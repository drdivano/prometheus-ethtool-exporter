#!/usr/bin/env python
"""Collect ethtool metrics,publish them via http or save them to a file."""
import argparse
import logging
import os
import re
import subprocess
import sys
import time

import prometheus_client


class EthtoolCollector(object):
    """Collect ethtool metrics,publish them via http or save them to a file."""

    def __init__(self, args=None):
        """Construct the object and parse the arguments."""
        self.args = None
        if not args:
            args = sys.argv[1:]
        self._parse_args(args)

    def _parse_args(self, args):
        """Parse CLI args and set them to self.args."""
        parser = argparse.ArgumentParser()
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            '-f',
            '--textfile-name',
            dest='textfile_name',
            help=('Full file path where to store data for node '
                  'collector to pick up')
        )
        group.add_argument(
            '-l',
            '--listen',
            dest='listen',
            help='Listen host:port, i.e. 0.0.0.0:9417'
        )
        parser.add_argument(
            '-i',
            '--interval',
            dest='interval',
            type=int,
            help=('Number of seconds between updates of the textfile. '
                  'Default is 5 seconds')
        )
        parser.add_argument(
            '-I',
            '--interface-regex',
            dest='interface_regex',
            default='.*',
            help='Only scrape interfaces whose name matches this regex'
        )
        parser.add_argument(
            '-1',
            '--oneshot',
            dest='oneshot',
            action='store_true',
            default=False,
            help='Run only once and exit. Useful for running in a cronjob'
        )
        wblistgroup = parser.add_mutually_exclusive_group()
        wblistgroup.add_argument(
            '-w',
            '--whitelist-regex',
            dest='whitelist_regex',
            help=('Only include values whose name matches this regex. '
                  '-w and -b are mutually exclusive')
        )
        wblistgroup.add_argument(
            '-b',
            '--blacklist-regex',
            dest='blacklist_regex',
            help=('Exclude values whose name matches this regex. '
                  '-w and -b are mutually exclusive')
        )
        arguments = parser.parse_args(args)
        if arguments.oneshot and not arguments.textfile_name:
            logging.error('Oneshot has to be used with textfile mode')
            parser.print_help()
            sys.exit(1)
        if arguments.interval and not arguments.textfile_name:
            logging.error('Interval has to be used with textfile mode')
            parser.print_help()
            sys.exit(1)
        if not arguments.interval:
            arguments.interval = 5
        self.args = vars(arguments)

    def whitelist_blacklist_check(self, stat_name):
        """Check whether stat_name matches whitelist or blacklist."""
        if self.args['whitelist_regex']:
            if re.match(self.args['whitelist_regex'], stat_name):
                return True
            else:
                return False
        if self.args['blacklist_regex']:
            if re.match(self.args['blacklist_regex'], stat_name):
                return False
            else:
                return True
        return True

    def update_ethtool_stats(self, iface, gauge):
        """Update gauge with statistics from ethtool for interface iface."""
        command = ['/sbin/ethtool', '-S', iface]
        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE)
        except FileNotFoundError:
            logging.critical('/sbin/ethtool not found. Giving up')
            sys.exit(1)
        except PermissionError as e:
            logging.critical('Permission error trying to '
                             'run /sbin/ethtool: {}'.format(e))
            sys.exit(1)
        data = proc.communicate()[0]
        if proc.returncode != 0:
            logging.critical('Ethtool returned non-zero return '
                            'code for interface {}'.format(iface))
            return
        data = data.decode('utf-8').split('\n')
        key_set = set()
        for line in data:
            # drop empty lines and the header
            if not line or line == 'NIC statistics:':
                continue
            line = line.strip()
            try:
                key, value = line.split(': ')
                key = key.strip()
                value = value.strip()
                value = float(value)
            except ValueError:
                logging.warning('Failed parsing "{}"'.format(line))
                continue
            if not self.whitelist_blacklist_check(key):
                continue
            labels = [iface, key]
            if key not in key_set:
                gauge.add_metric(labels, value)
                key_set.add(key)
            else:
                logging.warning('Item {} already seen, check the source '
                                'data for interface {}'.format(key, iface))

    def collect(self):
        """
        Collect the metrics.

        Collect the metrics and yield them. Prometheus client library
        uses this method to respond to http queries or save them to disk.
        """
        gauge = prometheus_client.core.GaugeMetricFamily(
            'node_net_ethtool', 'Ethtool data', labels=['device', 'type'])
        for iface in self.find_physical_interfaces():
            self.update_ethtool_stats(iface, gauge)
        yield gauge

    def find_physical_interfaces(self):
        """Find physical interfaces and optionally filter them."""
        # https://serverfault.com/a/833577/393474
        root = '/sys/class/net'
        for file in os.listdir(root):
            path = os.path.join(root, file)
            if os.path.islink(path) and 'virtual' not in os.readlink(path):
                if re.match(self.args['interface_regex'], file):
                    yield file


if __name__ == '__main__':
    collector = EthtoolCollector()
    registry = prometheus_client.CollectorRegistry()
    registry.register(collector)
    args = collector.args
    if args['listen']:
        (ip, port) = args['listen'].split(':')
        prometheus_client.start_http_server(port=int(port),
                                            addr=ip, registry=registry)
        while True:
            time.sleep(3600)
    if args['textfile_name']:
        while True:
            collector.collect()
            prometheus_client.write_to_textfile(args['textfile_name'],
                                                registry)
            if collector.args['oneshot']:
                sys.exit(0)
            time.sleep(args['interval'])
