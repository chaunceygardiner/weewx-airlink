# Copyright 2020 by John A Kline <john@johnkline.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

"""
WeeWX module that records AirLink air quality sensor readings.
"""

import json
import logging
import math
import requests
import sys
import threading
import time

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import weeutil.logger
import weeutil.weeutil
import weewx
import weewx.accum
import weewx.units
import weewx.xtypes

from weewx.units import ValueTuple
from weeutil.weeutil import timestamp_to_string
from weeutil.weeutil import to_bool
from weeutil.weeutil import to_float
from weeutil.weeutil import to_int
from weewx.engine import StdService

log = logging.getLogger(__name__)

WEEWX_AIRLINK_VERSION = "3.0"

if sys.version_info[0] < 3 or (sys.version_info[0] == 3 and sys.version_info[1] < 7):
    raise weewx.UnsupportedFeature(
        "weewx-airlink requires Python 3.7 or later, found %s.%s" % (sys.version_info[0], sys.version_info[1]))

if weewx.__version__ < "4":
    raise weewx.UnsupportedFeature(
        "weewx-airlink requires WeeWX 4, found %s" % weewx.__version__)

# Set up observation types not in weewx.units

weewx.units.USUnits['air_quality_index']       = 'aqi'
weewx.units.MetricUnits['air_quality_index']   = 'aqi'
weewx.units.MetricWXUnits['air_quality_index'] = 'aqi'

weewx.units.USUnits['air_quality_color']       = 'aqi_color'
weewx.units.MetricUnits['air_quality_color']   = 'aqi_color'
weewx.units.MetricWXUnits['air_quality_color'] = 'aqi_color'

weewx.units.default_unit_label_dict['aqi']  = ' AQI'
weewx.units.default_unit_label_dict['aqi_color'] = ' RGB'

weewx.units.default_unit_format_dict['aqi']  = '%d'
weewx.units.default_unit_format_dict['aqi_color'] = '%d'

weewx.units.obs_group_dict['pm1_0_1m'] = 'group_concentration'
weewx.units.obs_group_dict['pm10_0_1m'] = 'group_concentration'
weewx.units.obs_group_dict['pm2_5_aqi'] = 'air_quality_index'
weewx.units.obs_group_dict['pm2_5_aqi_color'] = 'air_quality_color'
weewx.units.obs_group_dict['pm2_5_1m'] = 'group_concentration'
weewx.units.obs_group_dict['pm2_5_1m_aqi'] = 'air_quality_index'
weewx.units.obs_group_dict['pm2_5_1m_aqi_color'] = 'air_quality_color'
weewx.units.obs_group_dict['pm2_5_nowcast'] = 'group_concentration'
weewx.units.obs_group_dict['pm2_5_nowcast_aqi'] = 'air_quality_index'
weewx.units.obs_group_dict['pm2_5_nowcast_aqi_color'] = 'air_quality_color'
weewx.units.obs_group_dict['pm10_0_nowcast'] = 'group_concentration'

# The observations this extension can fill into an archive record.  The
# 1m and nowcast variants are loop-only -- they are not database columns --
# so there is nothing to fill them into.
PM_OBS: List[str] = ['pm1_0', 'pm2_5', 'pm10_0']

# The span an airlink-proxy's /fetch-two-minute-record describes.
TWO_MINUTE_AVERAGE_SECS: int = 120

# The lowest airlink-proxy API command set catch-up can use.  Version 2 is
# where archive records became averages over their interval (version 1 filed
# the single poll that landed on the boundary, which does not describe the
# period), /fetch-two-minute-record arrived, and error bodies became
# parseable json.  Catch-up needs all three, and they shipped together.
# A LATER version is accepted: a command set only ever grows, and refusing
# one would silently disable catch-up for everyone until they restarted
# weewxd, since the refusal is remembered for the life of the process.
REQUIRED_PROXY_API_VERSION: int = 2

class Source:
    def __init__(self, config_dict, name, is_proxy):
        self.is_proxy = is_proxy
        # Raise KeyEror if name not in dictionary.
        source_dict = config_dict[name]
        self.enable = to_bool(source_dict.get('enable', False))
        self.hostname = source_dict.get('hostname', '')
        if is_proxy:
            self.port = to_int(source_dict.get('port', 8040))
            # An airlink-proxy answers out of its own database on the local
            # network.  One that has not answered in a second is down.
            self.timeout  = to_int(source_dict.get('timeout', 1))
        else:
            self.port = to_int(source_dict.get('port', 80))
            # The AirLink's own processor is slow and easily overwhelmed.
            self.timeout  = to_int(source_dict.get('timeout', 10))

@dataclass
class Concentrations:
    timestamp     : float
    pm_1_last     : float
    pm_2p5_last   : float
    pm_10_last    : float
    pm_1          : float
    pm_2p5        : float
    pm_10         : float
    pm_2p5_nowcast: float
    pm_10_nowcast : float
    hum           : float
    temp          : float

@dataclass
class Configuration:
    lock            : threading.Lock
    concentrations  : Optional[Concentrations] # Controlled by lock
    stale_logged    : bool                     # Controlled by lock
    archive_interval: int                      # Immutable
    poll_interval   : int                      # Immutable
    sources         : List[Source]             # Immutable
    # Archive periods this extension put pm data into, per observation.  An
    # archive record carries no proof of its own: under hardware record
    # generation the accumulator's values are grafted on AFTER this service's
    # handler runs, so a missing pm field says nothing about whether the
    # accumulator is empty.  What this extension injected does say so, and
    # only this extension knows it.  It lives here rather than on the service
    # because fill_in_packet, which records it, is a static method.
    injections      : Dict[str, List[float]] = field(  # Controlled by lock
        default_factory = lambda: {obs: [] for obs in PM_OBS})
    # How far back the injection tally is kept.  new_archive_record measures
    # a period with the interval WeeWX really archives on, which is NOT
    # archive_interval above when the console disagrees with weewx.conf, so
    # the tally has to reach at least as far back or a period WeeWX watched
    # can look unwatched.  The service sets it; None means fall back to
    # archive_interval, which is what the __main__ harness gets.
    injection_retention_secs: Optional[int] = None      # Immutable

def reraise_if_terminate(e: BaseException) -> None:
    """weewxd stops by raising Terminate from its SIGTERM signal handler --
    inside whatever the main thread is executing at that instant.  Every
    broad exception handler on a main-thread path must call this first and
    hand the exception back, or weewx cannot shut down.  weewxd runs as
    __main__, so its Terminate class cannot be imported here and is
    recognized by name."""
    if type(e).__name__ == 'Terminate':
        raise e

def get_concentrations(cfg: Configuration):
    for source in cfg.sources:
        if source.enable:
            record = collect_data(source.hostname,
                                  source.port,
                                  source.timeout,
                                  cfg.archive_interval)
            if record is not None:
                log.debug('get_concentrations: source: %s' % record)
                # is_sane permits a null last_report_time, which is what
                # dateTime is built from, so this can be None.  Nothing can
                # be said about the age of a reading that will not say when
                # it was taken; treat it as unusable rather than doing
                # arithmetic on None.  get_concentrations runs on the MAIN
                # thread at startup, where an escaping exception stops weewxd
                # from starting at all.
                reading_ts = to_int(record['dateTime'])
                if reading_ts is None:
                    log.info('Reading from %s:%d has no timestamp; ignoring it.'
                             % (source.hostname, source.port))
                    continue
                age_of_reading = time.time() - reading_ts
                if age_of_reading > cfg.archive_interval:
                    log.info('Reading from %s:%d is old: %d seconds.' % (
                        source.hostname, source.port, age_of_reading))
                    continue
                log.debug('get_concentrations: record: %s' % record)
                concentrations = Concentrations(
                    timestamp      = reading_ts,
                    pm_1_last      = record['pm_1_last'],
                    pm_2p5_last    = record['pm_2p5_last'],
                    pm_10_last     = record['pm_10_last'],
                    pm_1           = record['pm_1'],
                    pm_2p5         = record['pm_2p5'],
                    pm_10          = record['pm_10'],
                    pm_2p5_nowcast = record['pm_2p5_nowcast'],
                    pm_10_nowcast  = record['pm_10_nowcast'],
                    hum            = record['hum'],
                    temp           = record['temp'],
                )
                log.debug('get_concentrations: concentrations: %s' % concentrations)
                return concentrations
    log.error('Could not get concentrations from any source.')
    return None

def is_type(j: Dict[str, Any], t, name: str, none_ok: bool = False) -> bool:
    try:
        x = j[name]
        if x is None and none_ok:
            return True
        if not isinstance(x, t):
            log.debug('%s is not an instance of %s: %s' % (name, t, j[name]))
            return False
        return True
    except KeyError as e:
        log.debug('is_type: could not find key: %s' % e)
        return False
    except Exception as e:
        reraise_if_terminate(e)
        log.debug('is_type: exception: %s' % e)
        return False

def convert_data_structure_type_5_to_6(j: Dict[str, Any]) -> None:
    # Fix up these names and change data_structure_type to 6
    try:
        j['data']['conditions'][0]['pm_10'] = j['data']['conditions'][0]['pm_10p0']
        j['data']['conditions'][0]['pm_10p0'] = None
        j['data']['conditions'][0]['pm_10_last_1_hour'] = j['data']['conditions'][0]['pm_10p0_last_1_hour']
        j['data']['conditions'][0]['pm_10p0_last_1_hour'] = None
        j['data']['conditions'][0]['pm_10_last_3_hours'] = j['data']['conditions'][0]['pm_10p0_last_3_hours']
        j['data']['conditions'][0]['pm_10p0_last_3_hours'] = None
        j['data']['conditions'][0]['pm_10_last_24_hours'] = j['data']['conditions'][0]['pm_10p0_last_24_hours']
        j['data']['conditions'][0]['pm_10p0_last_24_hours'] = None
        j['data']['conditions'][0]['pm_10_nowcast'] = j['data']['conditions'][0]['pm_10p0_nowcast']
        j['data']['conditions'][0]['pm_10p0_nowcast'] = None

        j['data']['conditions'][0]['data_structure_type'] = 6
        log.debug('Converted type 5 record to type 6.')
    except Exception as e:
        reraise_if_terminate(e)
        log.info('convert_data_structure_type_5_to_6: exception: %s' % e)
        # Let sanity check handle the issue.

def is_sane(j: Dict[str, Any]) -> Tuple[bool, str]:
    if j['error'] is not None:
        return False, 'Error: %s' % j['error']

    if not is_type(j, dict, 'data'):
        return False, 'Missing or malformed "data" field'

    if not is_type(j['data'], str, 'name'):
        return False, 'Missing or malformed "name" field'

    if not is_type(j['data'], int, 'ts'):
        return False, 'Missing or malformed "ts" field'

    if not is_type(j['data'], list, 'conditions'):
        return False, 'Missing or malformed "conditions" field'

    if len(j['data']['conditions']) == 0:
        return False, 'Expected one element in conditions array.'

    if not is_type(j['data']['conditions'][0], int, 'data_structure_type'):
        return False, 'Missing or malformed "data_structure_type" field'

    if j['data']['conditions'][0]['data_structure_type'] != 6:
        return False, 'Expected data_structure_type of 6 (or type 5 auto converted to 6.'

    for name in ['pm_1_last', 'pm_2p5_last', 'pm_10_last', 'last_report_time',
            'pct_pm_data_last_1_hour', 'pct_pm_data_last_3_hours',
            'pct_pm_data_nowcast', 'pct_pm_data_last_24_hours']:
        if not is_type(j['data']['conditions'][0], int, name, True):
            return False, 'Missing or malformed "%s" field' % name

    if not is_type(j['data']['conditions'][0], int, 'lsid', True):
        return False, 'Missing or malformed "lsid" field'

    for name in ['temp', 'hum', 'dew_point', 'wet_bulb', 'heat_index']:
        if not is_type(j['data']['conditions'][0], float, name):
            return False, 'Missing or malformed "%s" field' % name

    for name in ['pm_1', 'pm_2p5', 'pm_2p5_last_1_hour',
             'pm_2p5_last_3_hours', 'pm_2p5_last_24_hours', 'pm_2p5_nowcast',
             'pm_10', 'pm_10_last_1_hour', 'pm_10_last_3_hours',
             'pm_10_last_24_hours', 'pm_10_nowcast']:
        if not is_type(j['data']['conditions'][0], float, name, True):
            return False, 'Missing or malformed "%s" field' % name

    return True, ''

def collect_data(hostname, port, timeout, archive_interval):

    j = None
    url = 'http://%s:%s/v1/current_conditions' % (hostname, port)

    try:
        # fetch data
        log.debug('collect_data: fetching from url: %s, timeout: %d' % (url, timeout))
        r = requests.get(url=url, timeout=timeout)
        r.raise_for_status()
        log.debug('collect_data: %s returned %r' % (hostname, r))
        if r:
            # convert to json
            j = r.json()
            log.debug('collect_data: json returned from %s is: %r' % (hostname, j))
            # Check for error
            if 'error' in j and j['error'] is not None:
                error = j['error']
                code = error['code']
                message = error['message']
                log.info('%s returned error(%d): %s' % (url, code, message))
                return None
            # If data structure type 5, convert it to 6.
            if j['data']['conditions'][0]['data_structure_type'] == 5:
                convert_data_structure_type_5_to_6(j)
            # Check for sanity
            sane, msg = is_sane(j)
            if not sane:
                log.warning('Reading not sane:  %s (%s)' % (msg, j))
                return None
            time_of_reading = j['data']['conditions'][0]['last_report_time']
            # The reading could be old.
            # Check that it's not older than now - arcint
            age_of_reading = time.time() - time_of_reading
            if age_of_reading > archive_interval:
                # Perhaps the AirLink has rebooted.  If so, the last_report_time
                # will be seconds from boot time (until the device syncs
                # time.  Check for this by checking if concentrations.pm_1
                # is None.
                if j['data']['conditions'][0]['pm_1'] is None:
                    log.info('last_report_time must be time since boot: %d seconds.  Record: %s'
                             % (time_of_reading, j))
                else:
                    # Not current.  (Note: Rarely, spurious timestamps (e.g., 2016 in 2020)
                    # have been observed.  Both the ts and last_report_time fields are incorrect.
                    # Example on Oct 10 21:11:38:
                    # {'data': {'did': '001D0A100214', 'name': 'airlink', 'ts': 1461926887,
                    # 'conditions': [{'lsid': 349506, 'data_structure_type': 6, 'temp': 67.7,
                    # 'hum': 72.2, 'dew_point': 58.4, 'wet_bulb': 61.2, 'heat_index': 68.1,
                    # 'pm_1_last': 0, 'pm_2p5_last': 0, 'pm_10_last': 0, 'pm_1': 0.0,
                    # 'pm_2p5': 0.0, 'pm_2p5_last_1_hour': 0.13, 'pm_2p5_last_3_hours': 0.27,
                    # 'pm_2p5_last_24_hours': 0.43, 'pm_2p5_nowcast': 0.23, 'pm_10': 1.09,
                    # 'pm_10_last_1_hour': 0.64, 'pm_10_last_3_hours': 0.89,
                    # 'pm_10_last_24_hours': 1.02, 'pm_10_nowcast': 0.84,
                    # 'last_report_time': 1461926886, 'pct_pm_data_last_1_hour': 100,
                    # 'pct_pm_data_last_3_hours': 100, 'pct_pm_data_nowcast': 100,
                    # 'pct_pm_data_last_24_hours': 100}]}, 'error': None}
                    log.info('Ignoring reading from %s--age: %d seconds.  Record: %s'
                             % (hostname, age_of_reading, j))
                j = None
    except Exception as e:
        reraise_if_terminate(e)
        log.info('collect_data: Attempt to fetch from: %s failed: %s.' % (hostname, e))
        j = None


    if j is None:
        return None

    # create a record
    log.debug('Successful read from %s.' % hostname)
    return populate_record(time_of_reading, j)

def populate_record(ts, j):
    record = dict()
    record['dateTime'] = ts
    record['usUnits'] = weewx.US

    # put items into record
    missed = []

    def get_and_update_missed(key):
        if key in j['data']['conditions'][0]:
            return j['data']['conditions'][0][key]
        else:
            missed.append(key)
            return None

    record['last_report_time'] = get_and_update_missed('last_report_time')
    record['temp'] = get_and_update_missed('temp')
    record['hum'] = get_and_update_missed('hum')
    record['dew_point'] = get_and_update_missed('dew_point')
    record['wet_bulb'] = get_and_update_missed('wet_bulb')
    record['heat_index'] = get_and_update_missed('heat_index')
    record['pct_pm_data_last_1_hour'] = get_and_update_missed('pct_pm_data_last_1_hour')
    record['pct_pm_data_last_3_hours'] = get_and_update_missed('pct_pm_data_last_3_hours')
    record['pct_pm_data_nowcast'] = get_and_update_missed('pct_pm_data_nowcast')
    record['pct_pm_data_last_24_hours'] = get_and_update_missed('pct_pm_data_last_24_hours')

    record['pm1_0'] = get_and_update_missed('pm_1_last')
    record['pm2_5'] = get_and_update_missed('pm_2p5_last')
    record['pm10_0'] = get_and_update_missed('pm_10_last')

    # Copy in all of the concentrations.
    record['pm_1'] = get_and_update_missed('pm_1')
    record['pm_1_last'] = get_and_update_missed('pm_1_last')
    for prefix in ['pm_2p5', 'pm_10']:
        key = prefix + '_last'
        record[key] = get_and_update_missed(key)
        key = prefix
        record[key] = get_and_update_missed(key)
        key = prefix + '_last_1_hour'
        record[key] = get_and_update_missed(key)
        key = prefix + '_last_3_hours'
        record[key] = get_and_update_missed(key)
        key = prefix + '_last_24_hours'
        record[key] = get_and_update_missed(key)
        key = prefix + '_nowcast'
        record[key] = get_and_update_missed(key)

    if missed:
        log.info("Sensor didn't report field(s): %s" % ','.join(missed))

    return record

@dataclass
class ProxyAnswer:
    """What came back from one airlink-proxy request.

    `reachable` is the distinction the catch-up path turns on, and it is not
    the same question as whether the request produced anything usable.  A
    proxy that ANSWERED -- with an error envelope, with a body that is not
    what was asked for, with an HTTP error -- is up, and holds the history;
    it must not be taken out of service.  airlink-proxy answers 200 with an
    error envelope when its own database is momentarily locked by the writer
    (server.py:60-61), which is likeliest exactly during a catchup burst.
    Only silence -- a refused connection, a timeout, a name that will not
    resolve -- means down.

    `value` is the usable result, or None if there wasn't one."""
    reachable: bool
    value    : Optional[Any] = None

def ask_proxy(source: Source, command: str) -> ProxyAnswer:
    """GET one airlink-proxy command.  A proxy's error is the AirLink's own
    envelope -- a null data member and an error object -- so the check is the
    one collect_data makes of the device.  (Before API version 2 the error
    body was not valid json at all; r.json() rejects it, which lands in the
    same place.)"""
    url = 'http://%s:%s%s' % (source.hostname, source.port, command)
    try:
        log.debug('ask_proxy: fetching from url: %s, timeout: %d' % (url, source.timeout))
        r = requests.get(url=url, timeout=source.timeout)
    except Exception as e:
        reraise_if_terminate(e)
        log.info('ask_proxy: Attempt to fetch from: %s failed: %s.' % (url, e))
        return ProxyAnswer(reachable=False)
    # Something answered.  Whatever it said, the proxy is up.
    try:
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        reraise_if_terminate(e)
        log.info('ask_proxy: %s answered unusably: %s.' % (url, e))
        return ProxyAnswer(reachable=True)
    if isinstance(j, dict) and j.get('error') is not None:
        log.info('%s returned error: %s' % (url, j['error']))
        return ProxyAnswer(reachable=True)
    return ProxyAnswer(reachable=True, value=j)

def record_from_proxy_reading(reading: Any) -> Optional[Dict[str, Any]]:
    """One airlink-proxy reading -> a record, or None if it is not sane.

    An airlink-proxy serves the AirLink's own shape, so this is the same
    parsing the sensor path uses -- minus collect_data's freshness check,
    which an archive record from a period WeeWX was down for is supposed to
    fail.  The caller runs this inside its own try: a reading that is not
    shaped like a reading at all raises rather than returning."""
    if reading.get('data') is None:
        return None
    if reading['data']['conditions'][0]['data_structure_type'] == 5:
        convert_data_structure_type_5_to_6(reading)
    sane, msg = is_sane(reading)
    if not sane:
        log.warning('airlink-proxy record not sane, %s: %s' % (msg, reading))
        return None
    return populate_record(reading['data']['conditions'][0]['last_report_time'], reading)

def fetch_proxy_archive_records(source: Source, since_ts: int,
                                max_ts: int) -> ProxyAnswer:
    """The proxy's archive records covering (since_ts, max_ts] -- since_ts is
    exclusive and max_ts inclusive, which is the proxy's own convention.  The
    answer's value is the sane records, possibly an empty list, or None if
    the request produced nothing usable.

    Note the query arguments are separated by a comma, not an ampersand."""
    answer = ask_proxy(source, '/fetch-archive-records?since_ts=%d,max_ts=%d' % (
        since_ts, max_ts))
    j = answer.value
    if j is None:
        return answer
    # Parsing stays inside a try: this runs on weewx's main thread, where
    # anything that escapes takes weewxd down with it.  A [[ProxyN]] port
    # pointed at some other service can return well-formed json that is
    # nothing like a list of readings.
    try:
        if not isinstance(j, list):
            log.info('fetch_proxy_archive_records: %s returned %r, expected a list of records.'
                     % (source.hostname, j))
            return ProxyAnswer(reachable=True)
        records: List[Dict[str, Any]] = []
        for reading in j:
            # Per reading: one unparseable row must not lose the good ones,
            # and must not be mistaken by the caller for an unreachable proxy
            # -- which would suppress a proxy that answered.
            try:
                record = record_from_proxy_reading(reading)
            except Exception as e:
                reraise_if_terminate(e)
                log.warning('airlink-proxy record from %s could not be read: %s: %s'
                            % (source.hostname, e, reading))
                continue
            if record is not None:
                records.append(record)
    except Exception as e:
        reraise_if_terminate(e)
        log.info('fetch_proxy_archive_records: could not parse the answer from %s: %s.'
                 % (source.hostname, e))
        return ProxyAnswer(reachable=True)
    return ProxyAnswer(reachable=True, value=records)

def fetch_proxy_two_minute_record(source: Source) -> ProxyAnswer:
    """The proxy's average over the last two minutes.  A proxy with no
    reading yet answers with an error envelope ('No two-minute average
    available.'), which ask_proxy has already turned into a value of None."""
    answer = ask_proxy(source, '/fetch-two-minute-record')
    j = answer.value
    if j is None:
        return answer
    try:
        if not isinstance(j, dict):
            log.info('fetch_proxy_two_minute_record: %s returned %r, expected a reading.'
                     % (source.hostname, j))
            return ProxyAnswer(reachable=True)
        return ProxyAnswer(reachable=True, value=record_from_proxy_reading(j))
    except Exception as e:
        reraise_if_terminate(e)
        log.info('fetch_proxy_two_minute_record: could not parse the answer from %s: %s.'
                 % (source.hostname, e))
        return ProxyAnswer(reachable=True)

def average_pm_values(records: List[Dict[str, Any]]) -> Dict[str, float]:
    """Average the pm values of the proxy records covering one WeeWX archive
    period -- what the accumulator would have arrived at from loop packets,
    which carry these same fields.  A field is averaged over however many
    records actually reported it; a field no record reported is absent."""
    sums  : Dict[str, float] = {}
    counts: Dict[str, int]   = {}
    for record in records:
        for obs in PM_OBS:
            value = record.get(obs)
            if value is not None:
                sums[obs]   = sums.get(obs, 0.0) + value
                counts[obs] = counts.get(obs, 0) + 1
    return {obs: sums[obs] / counts[obs] for obs in sums}

def proxy_supports_catchup(source: Source, archive_interval: int) -> ProxyAnswer:
    """Whether this proxy may be used for catch-up.  The answer's value is
    True or False when the proxy said enough to decide, and None when it did
    not -- which is a different thing from the proxy being unreachable, so
    the caller is told both.

    Two things have to hold.  The proxy must speak the API command set
    catch-up needs -- an older proxy files the single boundary poll rather
    than an average, which is not what the period contained.  And its archive
    interval must divide evenly into the one WeeWX archives on.  A SHORTER
    interval is harmless: several of the proxy's records cover one WeeWX
    period and are averaged together.  A longer one, or one that does not
    divide, cannot answer for a WeeWX period at all -- the period can fall
    entirely inside one of its records.  Rather than silently filling such a
    period badly, or leaving a hole that looks like the sensor's fault,
    catch-up is refused and says so."""
    answer = ask_proxy(source, '/get-version')
    j = answer.value
    if j is None:
        return ProxyAnswer(reachable=answer.reachable)
    version = j.get('version') if isinstance(j, dict) else None
    version_number: Optional[int] = None
    if version is not None:
        try:
            # Through float: a command set that ever answers '2.1' or '3.0'
            # must not be refused as older than 2, which int() alone would do
            # by raising -- and the refusal is remembered for the life of the
            # process, so it would not be reconsidered until a restart.
            version_number = int(float(version))
        except (TypeError, ValueError):
            version_number = None
    if version_number is None or version_number < REQUIRED_PROXY_API_VERSION:
        log.info('airlink-proxy %s:%s speaks API version %r, but catch-up requires '
                 'version %d or later.  No archive records will be filled in from it.'
                 % (source.hostname, source.port, version, REQUIRED_PROXY_API_VERSION))
        return ProxyAnswer(reachable=True, value=False)
    answer = ask_proxy(source, '/get-archive-interval-secs')
    j = answer.value
    if j is None:
        return ProxyAnswer(reachable=answer.reachable)
    interval = to_int(j.get('archive-interval-secs')) if isinstance(j, dict) else None
    if not interval or archive_interval % interval != 0:
        log.info('airlink-proxy %s:%s archives every %r seconds, which does not divide '
                 'into the %d seconds WeeWX archives on.  No archive records will be '
                 'filled in from it.'
                 % (source.hostname, source.port, interval, archive_interval))
        return ProxyAnswer(reachable=True, value=False)
    return ProxyAnswer(reachable=True, value=True)

class AirLink(StdService):
    """Collect AirLink air quality measurements."""

    def __init__(self, engine, config_dict):
        super(AirLink, self).__init__(engine, config_dict)
        log.info("Service version is %s." % WEEWX_AIRLINK_VERSION)

        self.engine = engine
        self.config_dict = config_dict.get('AirLink', {})

        self.cfg = Configuration(
            lock             = threading.Lock(),
            concentrations   = None,
            stale_logged     = False,
            archive_interval = int(config_dict['StdArchive']['archive_interval']),
            poll_interval    = 5,
            sources          = AirLink.configure_sources(self.config_dict))

        # The interval WeeWX actually archives on, decided the way the engine
        # decides it (engine.py:544, 566-580): under SOFTWARE record
        # generation weewx.conf's value is used and the console is ignored;
        # under HARDWARE the console's is used -- differing from weewx.conf
        # only earns a log message -- unless the driver cannot report one.
        # This is what an airlink-proxy's archive interval has to match.  It
        # is kept apart from cfg.archive_interval, which decides how long a
        # reading stays fresh and must keep answering exactly as it always
        # has.
        archive_dict = config_dict.get('StdArchive', {})
        configured_interval = to_int(archive_dict.get('archive_interval', 300))
        if archive_dict.get('record_generation', 'hardware').lower() == 'hardware':
            try:
                self.archive_interval = to_int(engine.console.archive_interval)
            except (AttributeError, NotImplementedError):
                self.archive_interval = configured_interval
            # A driver that answers None would otherwise stop weewx from
            # starting, with a traceback pointing at this extension.
            if not self.archive_interval:
                self.archive_interval = configured_interval
        else:
            self.archive_interval = configured_interval
        self.cfg.injection_retention_secs = 2 * self.archive_interval
        # A proxy that could not be reached is not asked again until this
        # time.  A startup catchup delivers its records back to back: without
        # this, an unreachable proxy would cost its whole timeout PER RECORD.
        # If it is down, it is down.
        self.proxy_retry_after: Dict[str, float] = {}
        # Whether each proxy may be used for catch-up at all, asked once and
        # remembered.  Absent means not yet asked.
        self.proxy_catchup_ok: Dict[str, bool] = {}
        # Latch for the nothing-to-fill message: a catchup burst dispatches
        # every record of an outage back to back, and a proxy that is refused
        # outright has already said why once.  Same convention as the
        # stale-reading log (Configuration.stale_logged).
        self.no_data_logged = False

        source_count = 0
        for source in self.cfg.sources:
            if source.enable:
                source_count += 1
                log.info(
                    'Source %d for AirLink readings: %s %s:%s, proxy: %s, timeout: %d' % (
                    source_count, 'airlink-proxy' if source.is_proxy else 'sensor',
                    source.hostname, source.port, source.is_proxy, source.timeout))
        if source_count == 0:
            log.error('No sources configured for airlink extension.  AirLink extension is inoperable.')
        else:
            weewx.xtypes.xtypes.append(AQI())
            AQI.register_accumulator_extractors()

            with self.cfg.lock:
                self.cfg.concentrations = get_concentrations(self.cfg)

            # Start a thread to query devices.
            dp: DevicePoller = DevicePoller(self.cfg)
            t: threading.Thread = threading.Thread(target=dp.poll_device, name='AirLink', daemon=True)
            t.start()

            self.bind(weewx.NEW_LOOP_PACKET, self.new_loop_packet)

            # Filling in an archive period WeeWX was down for means asking an
            # airlink-proxy for its archive history.  An AirLink queried
            # directly keeps none, so with no proxy configured there is
            # nothing to ask and the handler is not bound at all -- no
            # fetches, no log messages, nothing.
            if any(source.enable and source.is_proxy for source in self.cfg.sources):
                self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)

    def new_loop_packet(self, event):
        log.debug('new_loop_packet(%s)' % event)
        AirLink.fill_in_packet(self.cfg, event.packet)

    @staticmethod
    def fill_in_packet(cfg: Configuration, packet: Dict):
        with cfg.lock:
            log.debug('new_loop_packet: cfg.concentrations: %s' % cfg.concentrations)
            if cfg.concentrations is not None and \
                    cfg.concentrations.timestamp is not None and \
                    cfg.concentrations.timestamp + \
                    cfg.archive_interval >= time.time():
                if cfg.stale_logged:
                    log.info('Fresh concentrations available again.')
                    cfg.stale_logged = False
                log.debug('Time of reading being inserted: %s' % timestamp_to_string(cfg.concentrations.timestamp))
                # Insert pm1_0, pm2_5, pm10_0, aqi and aqic into loop packet.
                if cfg.concentrations.pm_1_last is not None:
                    packet['pm1_0'] = cfg.concentrations.pm_1_last
                    log.debug('Inserted packet[pm1_0]: %f into packet.' % cfg.concentrations.pm_1_last)
                if cfg.concentrations.pm_2p5_last is not None:
                    packet['pm2_5'] = cfg.concentrations.pm_2p5_last
                    log.debug('Inserted packet[pm2_5]: %f into packet.' % cfg.concentrations.pm_2p5_last)
                    # Put aqi and color in the packet.
                    packet['pm2_5_aqi'] = AQI.compute_pm2_5_aqi(packet['pm2_5'])
                    packet['pm2_5_aqi_color'] = AQI.compute_pm2_5_aqi_color(packet['pm2_5_aqi'])
                if cfg.concentrations.pm_10_last is not None:
                    packet['pm10_0'] = cfg.concentrations.pm_10_last
                    log.debug('Inserted packet[pm10_0]: %f into packet.' % cfg.concentrations.pm_10_last)

                # Also insert one minute averages as these averages are more useful for showing in realtime.
                # If 1m averages are not available, use last instead.
                if cfg.concentrations.pm_1 is not None:
                    packet['pm1_0_1m']       = cfg.concentrations.pm_1
                elif cfg.concentrations.pm_1_last is not None:
                    packet['pm1_0_1m']       = cfg.concentrations.pm_1_last
                if cfg.concentrations.pm_2p5 is not None:
                    packet['pm2_5_1m']       = cfg.concentrations.pm_2p5
                elif cfg.concentrations.pm_2p5_last is not None:
                    packet['pm2_5_1m']       = cfg.concentrations.pm_2p5_last
                if cfg.concentrations.pm_10 is not None:
                    packet['pm10_0_1m']      = cfg.concentrations.pm_10
                elif cfg.concentrations.pm_10_last is not None:
                    packet['pm10_0_1m']      = cfg.concentrations.pm_10_last

                # Add 1m aqi and color
                if 'pm2_5_1m' in packet:
                    packet['pm2_5_1m_aqi'] = AQI.compute_pm2_5_aqi(packet['pm2_5_1m'])
                    packet['pm2_5_1m_aqi_color'] = AQI.compute_pm2_5_aqi_color(packet['pm2_5_1m_aqi'])

                # And insert nowcast for pm 2.5 and 10 as some might want to report that.
                # If nowcast not available, don't substitute.
                if cfg.concentrations.pm_2p5_nowcast is not None:
                    packet['pm2_5_nowcast']  = cfg.concentrations.pm_2p5_nowcast
                    packet['pm2_5_nowcast_aqi'] = AQI.compute_pm2_5_aqi(packet['pm2_5_nowcast'])
                    packet['pm2_5_nowcast_aqi_color'] = AQI.compute_pm2_5_aqi_color(packet['pm2_5_nowcast_aqi'])
                if cfg.concentrations.pm_10_nowcast is not None:
                    packet['pm10_0_nowcast'] = cfg.concentrations.pm_10_nowcast

                AirLink.record_injections(cfg, packet)
            else:
                # Log at error level once per outage, not once per loop packet.
                if not cfg.stale_logged:
                    log.error('Found no fresh concentrations to insert.')
                    cfg.stale_logged = True
                else:
                    log.debug('Found no fresh concentrations to insert.')

    @staticmethod
    def record_injections(cfg: Configuration, packet: Dict[str, Any]) -> None:
        """Remember, per observation, that this extension put a value in a
        loop packet -- this is what new_archive_record consults to tell a
        period the accumulator has data for from one it has nothing for.
        Called with cfg.lock already held."""
        # `.get('dateTime')` defaults only cover an ABSENT key; a key
        # present and null still yields None, and the subtraction below would
        # then raise on the main thread, inside fill_in_packet, which
        # new_loop_packet calls with no guard around it.
        ts = to_float(packet.get('dateTime'))
        if ts is None:
            ts = time.time()
        # Two archive intervals is plenty to answer for the period that just
        # closed, and bounds the lists.
        retention = cfg.injection_retention_secs
        if not retention:
            retention = 2 * cfg.archive_interval
        cutoff = ts - retention
        for obs in PM_OBS:
            if obs in packet:
                cfg.injections[obs].append(ts)
            cfg.injections[obs] = [t for t in cfg.injections[obs] if t >= cutoff]

    def injected_in(self, obs: str, start_ts: float, end_ts: float) -> bool:
        """Did this extension put obs into a loop packet in
        (start_ts, end_ts]?  If it did, the accumulator holds that period's
        samples and nothing needs filling in."""
        with self.cfg.lock:
            return any(start_ts < ts <= end_ts for ts in self.cfg.injections[obs])

    def new_archive_record(self, event):
        """Fill in pm observations for an archive period this extension
        contributed nothing to -- the periods WeeWX was down for, handed over
        by the logger at startup catchup.  Bound only when a proxy source is
        configured.

        Runs on the main thread, in the data_services slot, so the record can
        still be altered: StdArchive stores it (and, for hardware records,
        grafts the accumulator's values onto the fields still missing) only
        after every data service has seen it.  Whatever is set here therefore
        survives -- which is also why a value is only ever set for a period
        this extension put nothing into."""
        record = event.record
        # Same class of hazard as the interval below, and ahead of the same
        # try: a record whose dateTime is null would raise where nothing
        # catches it.  A record that cannot say which period it covers cannot
        # be filled in.
        end_ts = to_int(record.get('dateTime'))
        if end_ts is None:
            log.info('Archive record has no timestamp; not filling it in.')
            return
        # The record's own interval, not the configured archive interval: on
        # a long catchup a logger's records need not fall on archive
        # boundaries.
        # `or 0`: a record read back with a NULL interval would otherwise
        # raise here, outside the try below, and take weewxd down.  to_float,
        # not to_int: under software record generation WeeWX sets interval by
        # true division (engine.py:758), so a 90 second interval arrives as
        # 1.5 and to_int would truncate it to a 60 second window.  round()
        # keeps the result an int and absorbs float error -- to_float(100/60)
        # * 60 is 99.999..., which should be 100.
        interval_secs = round(to_float(record.get('interval') or 0) * 60)
        if interval_secs <= 0:
            interval_secs = self.archive_interval
        start_ts = end_ts - interval_secs

        # Test for None, not just for absence.  Under software record
        # generation the accumulator has already had its say by the time this
        # runs, and it writes None for a type it holds with no usable values.
        needed = [obs for obs in PM_OBS if record.get(obs) is None
                  and not self.injected_in(obs, start_ts, end_ts)]
        if not needed:
            return

        # Main thread: an exception escaping here goes up through
        # dispatchEvent and stops weewxd.  Nothing about filling in an old
        # record is worth that.
        try:
            values = self.backfill_values(start_ts, end_ts)
        except Exception as e:
            reraise_if_terminate(e)
            log.error('Could not fill %s in archive record %s: %s' % (
                ', '.join(needed), timestamp_to_string(end_ts), e))
            return
        filled = [obs for obs in needed if obs in values]
        for obs in filled:
            record[obs] = values[obs]
        if filled:
            self.no_data_logged = False
            log.info('Filled in %s in archive record %s.' % (
                ', '.join(filled), timestamp_to_string(end_ts)))
        elif not self.no_data_logged:
            # Log once, not once per record of a catchup burst.
            self.no_data_logged = True
            log.info('No airlink-proxy data with which to fill %s in archive record %s.' % (
                ', '.join(needed), timestamp_to_string(end_ts)))
        else:
            log.debug('No airlink-proxy data with which to fill %s in archive record %s.' % (
                ', '.join(needed), timestamp_to_string(end_ts)))

    def usable_proxies(self) -> List[Source]:
        """The enabled proxies worth asking right now: reachable lately, and
        answering for the API version and archive interval catch-up needs.
        The question is asked once per proxy and remembered; a proxy that
        could not be reached is left unanswered and asked again later."""
        now = time.time()
        usable: List[Source] = []
        for source in self.cfg.sources:
            if not source.enable or not source.is_proxy:
                continue
            key = '%s:%s' % (source.hostname, source.port)
            if now < self.proxy_retry_after.get(key, 0.0):
                continue
            if key not in self.proxy_catchup_ok:
                answer = proxy_supports_catchup(source, self.archive_interval)
                if answer.value is None:
                    # Undecided.  Suppress it only if nothing answered at all;
                    # a proxy that answered badly is still up, and asking it
                    # again costs nothing it has not already paid.
                    if not answer.reachable:
                        self.proxy_retry_after[key] = now + self.archive_interval
                    continue
                self.proxy_catchup_ok[key] = answer.value
            if self.proxy_catchup_ok[key]:
                usable.append(source)
        return usable

    def backfill_values(self, start_ts: int, end_ts: int) -> Dict[str, float]:
        """The pm values for the period (start_ts, end_ts], from the first
        proxy that holds archive records covering it.

A proxy normally holds the period that just closed -- it writes the
        record on its first poll at or past the boundary, ahead of WeeWX,
        which archives at archive_delay.

        The two fallbacks below are governed by one rule: a reading may fill a
        period only if it OVERLAPS that period.  The proxy's two minute
        average describes the two minutes ending at its freshest sample, so it
        qualifies when that window intersects the period.  The reading in hand
        is a single instantaneous sample -- a point, not a span -- so it
        qualifies only when it was taken inside the period.  Neither is
        allowed to stand in for a period it says nothing about, which is what
        an unchecked sample taken after the period closed would be doing.

        Any period nothing overlaps keeps its empty pm columns.  That is the
        right answer, not a defeat."""
        now = time.time()
        # The proxies that answered.  One that did not is not asked a second
        # time for its two minute average: it has already cost a timeout on
        # weewx's main thread, and it is down.
        reachable: List[Source] = []
        for source in self.usable_proxies():
            answer = fetch_proxy_archive_records(source, start_ts, end_ts)
            if not answer.reachable:
                # Nothing answered.  Leave it alone until the next archive
                # period: every record in a catchup burst would otherwise wait
                # out the same timeout.  A proxy that DID answer, however
                # uselessly, is asked again for the next record -- a locked
                # database is momentary, and suppressing on it would abandon
                # the rest of the outage.
                self.proxy_retry_after['%s:%s' % (source.hostname, source.port)] = \
                    now + self.archive_interval
                continue
            reachable.append(source)
            if answer.value:
                # Records can be sane and still carry nothing usable -- a pm
                # sensor fault leaves temp and hum reporting.  Keep looking.
                values = average_pm_values(answer.value)
                if values:
                    return values
        # A two minute average ends at the freshest sample it covers, so one
        # taken now cannot reach a period that closed more than two minutes
        # ago.  Cheap check, before asking anyone anything -- and it guards
        # ONLY this loop.  The reading in hand below answers to a different
        # rule (taken inside the period), and a period that closed an hour
        # ago can still hold a sample that qualifies.
        two_minute_reachable = \
            reachable if time.time() - end_ts < TWO_MINUTE_AVERAGE_SECS else []
        for source in two_minute_reachable:
            record = fetch_proxy_two_minute_record(source).value
            if record is None:
                continue
            # The window this average describes, and whether it overlaps the
            # period.  This is also what keeps a FROZEN average out: a proxy
            # goes on serving its last two minute average after the AirLink
            # dies -- the row is replaced, never cleared -- and such a record
            # is dated before the period, so it cannot overlap it.
            reading_ts = record.get('dateTime')
            if reading_ts is None or \
                    reading_ts <= start_ts or \
                    reading_ts - TWO_MINUTE_AVERAGE_SECS >= end_ts:
                continue
            values = average_pm_values([record])
            if values:
                return values
        with self.cfg.lock:
            concentrations = self.cfg.concentrations
            # A spot reading has no duration, so overlapping the period means
            # being taken inside it.  A sample from after the period closed
            # describes a moment the period does not contain, however recent
            # it is.
            if concentrations is not None and concentrations.timestamp is not None \
                    and start_ts < concentrations.timestamp <= end_ts:
                return average_pm_values([{
                    'pm1_0' : concentrations.pm_1_last,
                    'pm2_5' : concentrations.pm_2p5_last,
                    'pm10_0': concentrations.pm_10_last}])
        return {}

    def configure_sources(config_dict):
        sources = []
        # Configure Proxies
        idx = 0
        while True:
            idx += 1
            try:
                source = Source(config_dict, 'Proxy%d' % idx, True)
                sources.append(source)
            except KeyError:
                break
        # Configure Sensors
        idx = 0
        while True:
            idx += 1
            try:
                source = Source(config_dict, 'Sensor%d' % idx, False)
                sources.append(source)
            except KeyError:
                break

        return sources

class DevicePoller:
    def __init__(self, cfg: Configuration):
        self.cfg = cfg

    def poll_device(self) -> None:
        log.debug('poll_device: start')
        while True:
            try:
                log.debug('poll_device: calling get_concentrations.')
                concentrations = get_concentrations(self.cfg)
            except Exception as e:
                log.error('poll_device exception: %s' % e)
                weeutil.logger.log_traceback(log.critical, "    ****  ")
                concentrations = None
            log.debug('poll_device: concentrations: %s' % concentrations)
            if concentrations is not None:
                with self.cfg.lock:
                    self.cfg.concentrations = concentrations
            log.debug('poll_device: Sleeping for %d seconds.' % self.cfg.poll_interval)
            time.sleep(self.cfg.poll_interval)

class AQI(weewx.xtypes.XType):
    """
    AQI XType which computes the AQI (air quality index) from
    the pm2_5 value.
    """

    def __init__(self):
        pass

    agg_sql_dict = {
        'avg': "SELECT AVG(pm2_5), MIN(usUnits) FROM %(table_name)s "
               "WHERE dateTime > %(start)s AND dateTime <= %(stop)s AND pm2_5 IS NOT NULL",
        'count': "SELECT COUNT(dateTime), MIN(usUnits) FROM %(table_name)s "
                 "WHERE dateTime > %(start)s AND dateTime <= %(stop)s AND pm2_5 IS NOT NULL",
        'first': "SELECT pm2_5, usUnits FROM %(table_name)s "
                 "WHERE dateTime = (SELECT MIN(dateTime) FROM %(table_name)s "
                 "WHERE dateTime > %(start)s AND dateTime <= %(stop)s AND pm2_5 IS NOT NULL)",
        'last': "SELECT pm2_5, usUnits FROM %(table_name)s "
                "WHERE dateTime = (SELECT MAX(dateTime) FROM %(table_name)s "
                "WHERE dateTime > %(start)s AND dateTime <= %(stop)s AND pm2_5 IS NOT NULL)",
        'min': "SELECT pm2_5, usUnits FROM %(table_name)s "
               "WHERE dateTime > %(start)s AND dateTime <= %(stop)s AND pm2_5 IS NOT NULL "
               "ORDER BY pm2_5 ASC LIMIT 1;",
        'max': "SELECT pm2_5, usUnits FROM %(table_name)s "
               "WHERE dateTime > %(start)s AND dateTime <= %(stop)s AND pm2_5 IS NOT NULL "
               "ORDER BY pm2_5 DESC LIMIT 1;",
    }

    day_boundary_avg_min_max_sql_dict = {
        'usUnits': "SELECT usUnits from %(table_name)s ORDER BY dateTime DESC LIMIT 1;",
        'avg'    : "SELECT sum(wsum) / sum(sumtime) FROM %(table_name)s%(pm2_5_summary_suffix)s "
                   "WHERE dateTime >= %(start)s AND dateTime < %(stop)s ",
        'min'    : "SELECT min FROM %(table_name)s%(pm2_5_summary_suffix)s "
                   "WHERE dateTime >= %(start)s AND dateTime < %(stop)s "
                   "ORDER BY min ASC LIMIT 1;",
        'max'    : "SELECT max FROM %(table_name)s%(pm2_5_summary_suffix)s "
                   "WHERE dateTime >= %(start)s AND dateTime < %(stop)s "
                   "ORDER BY max DESC LIMIT 1;",
    }

    # Maps each supported AQI observation type to the observation it is
    # computed from.  Only pm2_5 is a database column; the 1m and nowcast
    # observations exist only in loop packets, so their AQI types resolve
    # only against a record that carries them.
    aqi_source_field = {
        'pm2_5_aqi'              : 'pm2_5',
        'pm2_5_aqi_color'        : 'pm2_5',
        'pm2_5_1m_aqi'           : 'pm2_5_1m',
        'pm2_5_1m_aqi_color'     : 'pm2_5_1m',
        'pm2_5_nowcast_aqi'      : 'pm2_5_nowcast',
        'pm2_5_nowcast_aqi_color': 'pm2_5_nowcast',
    }

    @staticmethod
    def register_accumulator_extractors() -> None:
        """Tell the accumulator not to extract the loop-injected AQI fields
        into archive records.  fill_in_packet computes AQI per loop packet
        under the same names this xtype serves; without this, WeeWX's default
        avg extractor would fold a meaningless averaged AQI into the archive
        record, and $current would use it instead of the xtype during
        real-time report generation.  extractor = noop drops the fields so
        lookups fall through to the xtype -- the same pattern WeeWX's own
        defaults use for windSpeed.  A user's [Accumulator] section takes
        precedence over these entries."""
        weewx.accum.accum_dict.extend(
            {obs_type: {'extractor': 'noop'} for obs_type in AQI.aqi_source_field})

    @staticmethod
    def compute_pm2_5_aqi(pm2_5):
        #             U.S. EPA PM2.5 AQI (May 2024 AirNow TAD)
        #
        #  AQI Category  AQI Value  24-hr PM2.5
        # Good             0 -  50    0.0 -   9.0
        # Moderate        51 - 100    9.1 -  35.4
        # USG            101 - 150   35.5 -  55.4
        # Unhealthy      151 - 200   55.5 - 125.4
        # Very Unhealthy 201 - 300  125.5 - 225.4
        # Hazardous      301 - 500  225.5 - 325.4
        #
        # Concentrations above 325.4 map to AQI values above 500, continuing
        # on the Hazardous slope (TAD breakpoint-table footnote 4 and the
        # "AQI values above 500" FAQ).  There is no upper cap.

        # The EPA standard for AQI says to truncate PM2.5 to one decimal place.
        # See https://www3.epa.gov/airnow/aqi-technical-assistance-document-sept2018.pdf
        x = math.trunc(pm2_5 * 10) / 10

        if x <= 9.0: # Good
            aqi = round(x / 9.0 * 50)
        elif x <= 35.4: # Moderate
            aqi = round((x - 9.1) / 26.3 * 49.0 + 51.0)
        elif x <= 55.4: # Unhealthy for sensitive groups
            aqi = round((x - 35.5) / 19.9 * 49.0 + 101.0)
        elif x <= 125.4: # Unhealthy
            aqi = round((x - 55.5) / 69.9 * 49.0 + 151.0)
        elif x <= 225.4: # Very Unhealthy
            aqi = round((x - 125.5) / 99.9 * 99.0 + 201.0)
        else: # Hazardous
            aqi = round((x - 225.5) / 99.9 * 199.0 + 301.0)

        # A negative pm2_5 (only possible if a bogus value reached the
        # database by some other means) must not map below zero.
        return max(0, aqi)

    @staticmethod
    def compute_pm2_5_aqi_color(pm2_5_aqi):
        if pm2_5_aqi <= 50:
            return 228 << 8                      # Green
        elif pm2_5_aqi <= 100:
            return (255 << 16) + (255 << 8)      # Yellow
        elif pm2_5_aqi <=  150:
            return (255 << 16) + (126 << 8)      # Orange
        elif pm2_5_aqi <= 200:
            return 255 << 16                     # Red
        elif pm2_5_aqi <= 300:
            return (143 << 16) + (63 << 8) + 151 # Purple
        else:
            return (126 << 16) + 35              # Maroon

    @staticmethod
    def get_scalar(obs_type, record, db_manager=None):
        log.debug('get_scalar(%s)' % obs_type)
        if obs_type not in AQI.aqi_source_field:
            raise weewx.UnknownType(obs_type)
        if record is None:
            log.debug('get_scalar called where record is None.')
            raise weewx.CannotCalculate(obs_type)
        source_field = AQI.aqi_source_field[obs_type]
        if source_field not in record:
            # Returning CannotCalculate causes exception in ImageGenerator, return UnknownType instead.
            # ERROR weewx.reportengine: Caught unrecoverable exception in generator 'weewx.imagegenerator.ImageGenerator'
            log.debug('get_scalar called where record does not contain %s.' % source_field)
            raise weewx.UnknownType(obs_type)
        if record[source_field] is None:
            # Returning CannotCalculate causes exception in ImageGenerator, return UnknownType instead.
            # ERROR weewx.reportengine: Caught unrecoverable exception in generator 'weewx.imagegenerator.ImageGenerator'
            # Any archive catchup records will have None for pm2_5.
            log.debug('get_scalar called where record[%s] is None.  Probably a catchup record.' %
                source_field)
            raise weewx.UnknownType(obs_type)
        try:
            pm2_5 = record[source_field]
            if obs_type.endswith('_color'):
                value = AQI.compute_pm2_5_aqi_color(AQI.compute_pm2_5_aqi(pm2_5))
            else:
                value = AQI.compute_pm2_5_aqi(pm2_5)
            t, g = weewx.units.getStandardUnitType(record['usUnits'], obs_type)
            # Form the ValueTuple and return it:
            return weewx.units.ValueTuple(value, t, g)
        except KeyError:
            # Don't have everything we need. Raise an exception.
            raise weewx.CannotCalculate(obs_type)

    @staticmethod
    def get_series(obs_type, timespan, db_manager, aggregate_type=None, aggregate_interval=None):
        """Get a series, possibly with aggregation.
        """

        if obs_type not in [ 'pm2_5_aqi', 'pm2_5_aqi_color' ]:
            raise weewx.UnknownType(obs_type)

        log.debug('get_series(%s, %s, %s, aggregate:%s, aggregate_interval:%s)' % (
            obs_type, timestamp_to_string(timespan.start), timestamp_to_string(
            timespan.stop), aggregate_type, aggregate_interval))

        #  Prepare the lists that will hold the final results.
        start_vec = list()
        stop_vec = list()
        data_vec = list()

        # Is aggregation requested?
        if aggregate_type:
            # Yes. Just use the regular series function.
            return weewx.xtypes.ArchiveTable.get_series(obs_type, timespan, db_manager, aggregate_type,
                                           aggregate_interval)
        else:
            # No aggregation.
            sql_str = 'SELECT dateTime, usUnits, `interval`, pm2_5 FROM %s ' \
                      'WHERE dateTime >= ? AND dateTime <= ? AND pm2_5 IS NOT NULL' \
                      % db_manager.table_name
            std_unit_system = None

            for record in db_manager.genSql(sql_str, timespan):
                ts, unit_system, interval, pm2_5 = record
                if std_unit_system:
                    if std_unit_system != unit_system:
                        raise weewx.UnsupportedFeature(
                            "Unit type cannot change within a time interval.")
                else:
                    std_unit_system = unit_system

                if obs_type == 'pm2_5_aqi':
                    value = AQI.compute_pm2_5_aqi(pm2_5)
                else: # pm2_5_aqi_color
                    value = AQI.compute_pm2_5_aqi_color(AQI.compute_pm2_5_aqi(pm2_5))
                log.debug('get_series(%s): %s - %s - %s' % (obs_type,
                    timestamp_to_string(ts - interval * 60),
                    timestamp_to_string(ts), value))
                start_vec.append(ts - interval * 60)
                stop_vec.append(ts)
                data_vec.append(value)

            unit, unit_group = weewx.units.getStandardUnitType(std_unit_system, obs_type,
                                                               aggregate_type)

        return (ValueTuple(start_vec, 'unix_epoch', 'group_time'),
                ValueTuple(stop_vec, 'unix_epoch', 'group_time'),
                ValueTuple(data_vec, unit, unit_group))

    @staticmethod
    def get_aggregate(obs_type, timespan, aggregate_type, db_manager, **option_dict):
        """Returns an aggregation of pm2_5_aqi over a timespan by using the main archive
        table (or, for whole-day spans, the pm2_5 daily summary table).

        obs_type: Must be 'pm2_5_aqi' or 'pm2_5_aqi_color'.

        timespan: An instance of weeutil.Timespan with the time period over which aggregation is to
        be done.

        aggregate_type: The type of aggregation to be done. For this function, must be 'avg',
        'count', 'first', 'last', 'min', or 'max'. Anything else will cause
        weewx.UnknownAggregation to be raised.  ('sum' is deliberately not
        supported: the AQI of summed concentrations is not a meaningful
        quantity.)

        db_manager: An instance of weewx.manager.Manager or subclass.

        option_dict: Not used in this version.

        returns: A ValueTuple containing the result.
        """
        if obs_type not in [ 'pm2_5_aqi', 'pm2_5_aqi_color' ]:
            raise weewx.UnknownType(obs_type)

        log.debug('get_aggregate(%s, %s, %s, aggregate:%s)' % (
            obs_type, timestamp_to_string(timespan.start),
            timestamp_to_string(timespan.stop), aggregate_type))

        aggregate_type = aggregate_type.lower()

        # Raise exception if we don't know about this type of aggregation
        if aggregate_type not in list(AQI.agg_sql_dict.keys()):
            raise weewx.UnknownAggregation(aggregate_type)

        # Form the interpolation dictionary
        interpolation_dict = {
            'start': timespan.start,
            'stop': timespan.stop,
            'table_name': db_manager.table_name,
            'pm2_5_summary_suffix': '_day_pm2_5'
        }

        # The daily summary table can only be used if the timespan covers
        # whole archive days: both endpoints on local midnight.  A span
        # whose length merely happens to be a multiple of 24 hours (e.g.,
        # a trailing 24-hour window) must use the regular archive table.
        on_day_boundary = (timespan.start != timespan.stop
                           and weeutil.weeutil.isStartOfDay(timespan.start)
                           and weeutil.weeutil.isStartOfDay(timespan.stop))
        log.debug('day_boundary start: %r stop: %r on_day_boundary: %s' % (
            timespan.start, timespan.stop, on_day_boundary))
        if aggregate_type in list(AQI.day_boundary_avg_min_max_sql_dict.keys()) and on_day_boundary:
            select_stmt = AQI.day_boundary_avg_min_max_sql_dict[aggregate_type] % interpolation_dict
            select_usunits_stmt = AQI.day_boundary_avg_min_max_sql_dict['usUnits'] % interpolation_dict
            need_usUnits = True
        else:
            select_stmt = AQI.agg_sql_dict[aggregate_type] % interpolation_dict
            need_usUnits = False
        if need_usUnits:
            row = db_manager.getSql(select_usunits_stmt)
            if row:
                std_unit_system, = row
            else:
                std_unit_system = None
        row = db_manager.getSql(select_stmt)
        if row:
            if need_usUnits:
                value, = row
            else:
                value, std_unit_system = row
        else:
            value = None
            std_unit_system = None

        # A count is a count of records; every other aggregate is a pm2_5
        # concentration that must be converted to an AQI (or color).
        if value is not None and aggregate_type != 'count':
            if obs_type == 'pm2_5_aqi':
                value = AQI.compute_pm2_5_aqi(value)
            else: # pm2_5_aqi_color
                value = AQI.compute_pm2_5_aqi_color(AQI.compute_pm2_5_aqi(value))
        t, g = weewx.units.getStandardUnitType(std_unit_system, obs_type, aggregate_type)
        # Form the ValueTuple and return it:
        log.debug('get_aggregate(%s, %s, %s, aggregate:%s, select_stmt: %s, returning %s)' % (
            obs_type, timestamp_to_string(timespan.start), timestamp_to_string(timespan.stop),
            aggregate_type, select_stmt, value))
        return weewx.units.ValueTuple(value, t, g)

if __name__ == "__main__":
    usage = """%prog [options] [--help] [--debug]"""

    import weeutil.logger

    def main():
        import optparse
        parser = optparse.OptionParser(usage=usage)
        parser.add_option('--config', dest='cfgfn', type=str, metavar="FILE",
                          help="Use configuration file FILE. Default is /etc/weewx/weewx.conf or /home/weewx/weewx.conf")
        parser.add_option('--test-extension', dest='te', action='store_true',
                          help='test the data collector (requires a live AirLink sensor)')
        parser.add_option('--test-is-sane', dest='sane_test', action='store_true',
                          help='test the is_sane function (no sensor needed)')
        parser.add_option('--test-catchup', dest='catchup', action='store_true',
                          help='test the archive catch-up path (requires a running'
                               ' airlink-proxy)')
        parser.add_option('--hostname', dest='hostname', action='store',
                          help='hostname to use with --test-extension or --test-catchup')
        parser.add_option('--port', dest='port', action='store',
                          type=int, default=None,
                          help="port to use.  Default is '80' with --test-extension"
                               " and '8040' with --test-catchup")
        parser.add_option('--archive-interval', dest='archive_interval',
                          action='store', type=int, default=300,
                          help='the interval WeeWX archives on, in seconds, used by'
                               " --test-catchup to check the proxy against.  Default"
                               " is '300'")
        (options, args) = parser.parse_args()

        weeutil.logger.setup('airlink', {})

        if options.te:
            if not options.hostname:
                parser.error('--test-extension requires --hostname argument')
            test_extension(options.hostname, options.port or 80)
        if options.catchup:
            if not options.hostname:
                parser.error('--test-catchup requires --hostname argument')
            test_catchup(options.hostname, options.port or 8040,
                         options.archive_interval)
        if options.sane_test:
            test_is_sane()

    def test_extension(hostname, port):
        sources = [Source({'Sensor1': { 'enable': True, 'hostname': hostname, 'port': port, 'timeout': 2}}, 'Sensor1', False)]
        cfg = Configuration(
            lock             = threading.Lock(),
            concentrations   = None,
            stale_logged     = False,
            archive_interval = 300,
            poll_interval    = 5,
            sources          = sources)
        while True:
            with cfg.lock:
                cfg.concentrations = get_concentrations(cfg)
            print('%s:%d concentrations: %s' % (cfg.sources[0].hostname, cfg.sources[0].port, cfg.concentrations))
            packet: Dict[str, Any] = {}
            AirLink.fill_in_packet(cfg, packet)
            print('Fields to be inserted into packet: %s' % packet)
            time.sleep(cfg.poll_interval)

    def test_catchup(hostname, port, archive_interval):
        """Ask an airlink-proxy everything the catch-up path asks it, and show
        what would be filled into an archive record.  This is a diagnostic, so
        the timeout is generous where the extension's own default is one
        second: the question here is what the proxy says, not whether it says
        it fast enough to be worth waiting for on weewx's main thread."""
        source = Source({'Proxy1': {'enable': True, 'hostname': hostname,
                                    'port': port, 'timeout': 5}}, 'Proxy1', True)
        print('airlink-proxy %s:%d' % (source.hostname, source.port))
        print('WeeWX archive interval assumed to be %d seconds'
              ' (override with --archive-interval).' % archive_interval)
        print()

        print('What the proxy answers:')
        for command in ['/get-version', '/get-archive-interval-secs',
                        '/get-earliest-timestamp']:
            print('    %-28s %r' % (command, ask_proxy(source, command).value))
        print()

        answer = proxy_supports_catchup(source, archive_interval)
        if answer.value is None:
            print('The proxy %s, so nothing else can be checked.  Catch-up'
                  ' would leave the question open and ask again later.'
                  % ('answered, but not with a version and interval it could'
                     ' read' if answer.reachable else 'could not be reached'))
            return
        if not answer.value:
            print('This proxy will NOT be used to fill in archive records.'
                  '  The reason is logged above.')
        else:
            print('This proxy WILL be used to fill in archive records.')
        print()

        # The last archive period that closed -- the one catch-up would be
        # asked about first.
        end_ts = int(time.time() / archive_interval) * archive_interval
        start_ts = end_ts - archive_interval
        print('Last closed archive period, (%s, %s]:'
              % (timestamp_to_string(start_ts), timestamp_to_string(end_ts)))
        records = fetch_proxy_archive_records(source, start_ts, end_ts).value
        if records is None:
            print('    the proxy could not be asked')
        elif not records:
            print('    no archive records cover it, so this period would be'
                  ' filled from the two minute average below, if it closed'
                  ' within the last %d seconds -- and otherwise left empty'
                  % TWO_MINUTE_AVERAGE_SECS)
        else:
            print('    %d archive record(s) -> %s'
                  % (len(records), average_pm_values(records)))

        # A wider span, to show how much history the proxy actually holds.
        periods = 12
        span_ts = end_ts - periods * archive_interval
        records = fetch_proxy_archive_records(source, span_ts, end_ts).value
        if records is not None:
            print('Last %d periods (since %s): %d archive record(s).'
                  % (periods, timestamp_to_string(span_ts), len(records)))
        print()

        record = fetch_proxy_two_minute_record(source).value
        if record is None:
            print('/fetch-two-minute-record: no reading yet.')
        else:
            print('/fetch-two-minute-record: %s' % average_pm_values([record]))

    def test_is_sane():
        # A type-6 response as captured from a real AirLink.
        good_type_6 = ('{"data": {"did": "001D0A100214", "name": "airlink", "ts": 1602389498,'
            ' "conditions": [{"lsid": 349506, "data_structure_type": 6, "temp": 67.7,'
            ' "hum": 72.2, "dew_point": 58.4, "wet_bulb": 61.2, "heat_index": 68.1,'
            ' "pm_1_last": 0, "pm_2p5_last": 0, "pm_10_last": 0, "pm_1": 0.0,'
            ' "pm_2p5": 0.0, "pm_2p5_last_1_hour": 0.13, "pm_2p5_last_3_hours": 0.27,'
            ' "pm_2p5_last_24_hours": 0.43, "pm_2p5_nowcast": 0.23, "pm_10": 1.09,'
            ' "pm_10_last_1_hour": 0.64, "pm_10_last_3_hours": 0.89,'
            ' "pm_10_last_24_hours": 1.02, "pm_10_nowcast": 0.84,'
            ' "last_report_time": 1602389497, "pct_pm_data_last_1_hour": 100,'
            ' "pct_pm_data_last_3_hours": 100, "pct_pm_data_nowcast": 100,'
            ' "pct_pm_data_last_24_hours": 100}]}, "error": null}')
        # A type-5 response (early firmware): pm_10p0* field names.
        good_type_5 = ('{"data": {"did": "001D0A1000AF", "name": "LusherClose Sheringham",'
            ' "ts": 1601320120, "conditions": [{"lsid": 349639, "data_structure_type": 5,'
            ' "temp": 59.0, "hum": 69.3, "dew_point": 48.9, "wet_bulb": 52.6,'
            ' "heat_index": 58.1, "pm_1_last": 0, "pm_2p5_last": 0, "pm_10_last": 1,'
            ' "pm_1": 0.61, "pm_2p5": 0.61, "pm_2p5_last_1_hour": 1.07,'
            ' "pm_2p5_last_3_hours": 1.25, "pm_2p5_last_24_hours": 1.25,'
            ' "pm_2p5_nowcast": 1.2, "pm_10p0": 3.3, "pm_10p0_last_1_hour": 1.9,'
            ' "pm_10p0_last_3_hours": 2.52, "pm_10p0_last_24_hours": 2.52,'
            ' "pm_10p0_nowcast": 2.27, "last_report_time": 1601320120,'
            ' "pct_pm_data_last_1_hour": 100, "pct_pm_data_last_3_hours": 95,'
            ' "pct_pm_data_nowcast": 23, "pct_pm_data_last_24_hours": 11}]},'
            ' "error": null}')
        j = json.loads(good_type_6)
        sane, reason = is_sane(j)
        assert sane, reason
        j = json.loads(good_type_5)
        convert_data_structure_type_5_to_6(j)
        sane, reason = is_sane(j)
        assert sane, reason
        # A malformed reading (non-numeric pm_1) must not be sane.
        j = json.loads(good_type_6)
        j['data']['conditions'][0]['pm_1'] = 'abc'
        sane, _ = is_sane(j)
        assert not sane
        # An error response must not be sane.
        j = json.loads(good_type_6)
        j['error'] = {'code': 409, 'message': 'Error'}
        sane, _ = is_sane(j)
        assert not sane
        print('is_sane tests passed.')

    main()
