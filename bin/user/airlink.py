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

import logging
import math
import requests
import sys
import threading
import time

from dataclasses import dataclass
from typing import Any, Dict, List

import weeutil.weeutil
import weewx
import weewx.units
import weewx.xtypes

from weewx.units import ValueTuple
from weeutil.weeutil import timestamp_to_string
from weeutil.weeutil import to_bool
from weeutil.weeutil import to_int
from weewx.engine import StdService

log = logging.getLogger(__name__)

WEEWX_AIRLINK_VERSION = "0.1"

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

weewx.units.default_unit_label_dict['pm2_5_aqi']  = ' AQI'
weewx.units.default_unit_label_dict['pm2_5_aqi_color'] = ' RGB'
weewx.units.default_unit_label_dict['pm2_5_1m_aqi']  = ' AQI'
weewx.units.default_unit_label_dict['pm2_5_1m_aqi_color'] = ' RGB'
weewx.units.default_unit_label_dict['pm2_5_lrapa_aqi']  = ' AQI'
weewx.units.default_unit_label_dict['pm2_5_lrapa_aqi_color'] = ' RGB'
weewx.units.default_unit_label_dict['pm2_5_unbc_aqi']  = ' AQI'
weewx.units.default_unit_label_dict['pm2_5_unbc_aqi_color'] = ' RGB'

weewx.units.default_unit_format_dict['aqi']  = '%d'
weewx.units.default_unit_format_dict['aqi_color'] = '%d'

weewx.units.obs_group_dict['pm1_0_1m'] = 'group_concentration'
weewx.units.obs_group_dict['pm10_0_1m'] = 'group_concentration'
weewx.units.obs_group_dict['pm2_5_aqi'] = 'air_quality_index'
weewx.units.obs_group_dict['pm2_5_aqi_color'] = 'air_quality_color'
weewx.units.obs_group_dict['pm2_5_1m'] = 'group_concentration'
weewx.units.obs_group_dict['pm2_5_1m_aqi'] = 'air_quality_index'
weewx.units.obs_group_dict['pm2_5_1m_aqi_color'] = 'air_quality_color'
weewx.units.obs_group_dict['pm2_5_lrapa'] = 'group_concentration'
weewx.units.obs_group_dict['pm2_5_lrapa_aqi'] = 'air_quality_index'
weewx.units.obs_group_dict['pm2_5_lrapa_aqi_color'] = 'air_quality_color'
weewx.units.obs_group_dict['pm2_5_unbc'] = 'group_concentration'
weewx.units.obs_group_dict['pm2_5_unbc_aqi'] = 'air_quality_index'
weewx.units.obs_group_dict['pm2_5_unbc_aqi_color'] = 'air_quality_color'

class Source:
    def __init__(self, config_dict, name):
        # Raise KeyEror if name not in dictionary.
        source_dict = config_dict[name]
        self.enable = to_bool(source_dict.get('enable', False))
        self.hostname = source_dict.get('hostname', '')
        self.port = to_int(source_dict.get('port', 80))
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

@dataclass
class Configuration:
    lock            : threading.Lock
    concentrations  : Concentrations # Controlled by lock
    archive_interval: int            # Immutable
    archive_delay   : int            # Immutable
    poll_interval   : int            # Immutable
    sources         : List[Source]   # Immutable

def get_concentrations(cfg: Configuration):
    for source in cfg.sources:
        if source.enable:
            record = collect_data(source.hostname,
                                  source.port,
                                  source.timeout,
                                  cfg.archive_interval)
            if record is not None:
                log.debug('get_concentrations: source: %s' % record)
                reading_ts = to_int(record['dateTime'])
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
                )
                log.debug('get_concentrations: concentrations: %s' % concentrations)
                return concentrations
    log.error('Could not get concentrations from any source.')
    return None

def is_type(j: Dict[str, Any], t, names: List[str], none_ok: bool = False) -> bool:
    try:
        for name in names:
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
    except Exception as e:
        log.info('convert_data_structure_type_5_to_6: exception: %s' % e)
        # Let sanity check handle the issue.

def is_sane(j: Dict[str, Any]) -> bool:
    # { "data": { "did": "001D0A100214", "name": "airlink", "ts": 1601159588, "conditions": [{ "lsid": 349506, "data_structure_type": 6, "temp": 82.0, "hum": 42.8, "dew_point": 57.1, "wet_bulb": 62.8, "heat_index": 81.3, "pm_1_last": 1, "pm_2p5_last": 1, "pm_10_last": 2, "pm_1": 0.46, "pm_2p5": 0.55, "pm_2p5_last_1_hour": 1.65, "pm_2p5_last_3_hours": 0.58, "pm_2p5_last_24_hours": 0.77, "pm_2p5_nowcast": 0.90, "pm_10": 1.00, "pm_10_last_1_hour": 2.90, "pm_10_last_3_hours": 1.36, "pm_10_last_24_hours": 3.66, "pm_10_nowcast": 1.99, "last_report_time": 1601159588, "pct_pm_data_last_1_hour": 100, "pct_pm_data_last_3_hours": 100, "pct_pm_data_nowcast": 100, "pct_pm_data_last_24_hours": 100 }] }, "error": null }
    if j['error'] is not None:
        return False

    if not is_type(j, dict, ['data']):
        return False

    if not is_type(j['data'], str, ['name']):
        return False

    if not is_type(j['data'], int, ['ts']):
        return False

    if not is_type(j['data'], list, ['conditions']):
        return False

    if len(j['data']['conditions']) == 0:
        return False

    if not is_type(j['data']['conditions'][0], int, ['data_structure_type']):
        return False

    if j['data']['conditions'][0]['data_structure_type'] != 6:
        log.info('Expected data_structure_type of 6, found %s' % j['data']['conditions'][0]['data_structure_type'])
        return False

    if not is_type(j['data']['conditions'][0], int, ['pm_1_last',
            'pm_2p5_last', 'pm_10_last', 'last_report_time',
            'pct_pm_data_last_1_hour', 'pct_pm_data_last_3_hours',
            'pct_pm_data_nowcast', 'pct_pm_data_last_24_hours']):
        return False

    if not is_type(j['data']['conditions'][0], int, ['lsid'], True):
        return False

    if not is_type(j['data']['conditions'][0], float, ['temp', 'hum', 'dew_point', 'wet_bulb', 'heat_index']):
        return False

    if not is_type(j['data']['conditions'][0], float, ['pm_1', 'pm_2p5',
             'pm_2p5_last_1_hour', 'pm_2p5_last_3_hours', 'pm_2p5_last_24_hours',
             'pm_2p5_nowcast', 'pm_10', 'pm_10_last_1_hour', 'pm_10_last_3_hours',
             'pm_10_last_24_hours', 'pm_10_nowcast'], True):
        return False

    return True

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
            if not is_sane(j):
                log.info('airlink reading not sane: %s' % j)
                return None
            time_of_reading = j['data']['conditions'][0]['last_report_time']
            # The reading could be old.
            # Check that it's not older than now - arcint
            age_of_reading = time.time() - time_of_reading
            if age_of_reading > archive_interval:
                # Not current.
                log.info('Ignoring reading from %s--age: %d seconds.'
                         % (hostname, age_of_reading))
                j = None
    except Exception as e:
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

    record['airlink_last_report_time'] = get_and_update_missed('last_report_time')
    record['airlink_temp'] = get_and_update_missed('temp')
    record['airlink_hum'] = get_and_update_missed('hum')
    record['airlink_dew_point'] = get_and_update_missed('dew_point')
    record['airlink_wet_bulb'] = get_and_update_missed('wet_bulb')
    record['airlink_heat_index'] = get_and_update_missed('heat_index')
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

class AirLink(StdService):
    """Collect AirLink Air air quality measurements."""

    def __init__(self, engine, config_dict):
        super(AirLink, self).__init__(engine, config_dict)
        log.info("Service version is %s." % WEEWX_AIRLINK_VERSION)

        self.engine = engine
        self.config_dict = config_dict.get('AirLink', {})

        self.cfg = Configuration(
            lock             = threading.Lock(),
            concentrations   = None,
            archive_interval = int(config_dict['StdArchive']['archive_interval']),
            archive_delay    = to_int(config_dict['StdArchive'].get('archive_delay', 15)),
            poll_interval    = 5,
            sources          = AirLink.configure_sources(self.config_dict))
        with self.cfg.lock:
            self.cfg.concentrations = get_concentrations(self.cfg)

        source_count = 0
        for source in self.cfg.sources:
            if source.enable:
                source_count += 1
                log.info(
                    'Source %d for AirLink readings: %s:%s, timeout: %d' % (
                    source_count, source.hostname, source.port, source.timeout))
        if source_count == 0:
            log.error('No sources configured for airlink extension.  AirLink extension is inoperable.')
        else:
            weewx.xtypes.xtypes.append(AQI())

            # Start a thread to query devices.
            dp: DevicePoller = DevicePoller(self.cfg)
            t: threading.Thread = threading.Thread(target=dp.poll_device)
            t.setName('AirLink')
            t.setDaemon(True)
            t.start()

            self.bind(weewx.NEW_LOOP_PACKET, self.new_loop_packet)

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
                # Insert pm1_0, pm2_5, pm10_0, aqi and aqic into loop packet.
                packet['pm1_0'] = cfg.concentrations.pm_1_last
                packet['pm2_5'] = cfg.concentrations.pm_2p5_last
                packet['pm10_0'] = cfg.concentrations.pm_10_last
                log.debug('Time of reading being inserted: %s' % timestamp_to_string(cfg.concentrations.timestamp))
                log.debug('Inserted packet[pm1_0]: %f into packet.' % cfg.concentrations.pm_1_last)
                log.debug('Inserted packet[pm2_5]: %f into packet.' % cfg.concentrations.pm_2p5_last)
                log.debug('Inserted packet[pm10_0]: %f into packet.' % cfg.concentrations.pm_10_last)

                # Also insert one minute averages as these averages are more useful for showing in realtime.
                # If 1m averages are not available, use last instead.
                if cfg.concentrations.pm_1 is not None:
                    packet['pm1_0_1m']       = cfg.concentrations.pm_1
                else:
                    packet['pm1_0_1m']       = cfg.concentrations.pm_1_last
                if cfg.concentrations.pm_2p5 is not None:
                    packet['pm2_5_1m']       = cfg.concentrations.pm_2p5
                else:
                    packet['pm2_5_1m']       = cfg.concentrations.pm_2p5_last
                if cfg.concentrations.pm_10 is not None:
                    packet['pm10_0_1m']      = cfg.concentrations.pm_10
                else:
                    packet['pm10_0_1m']      = cfg.concentrations.pm_10_last

                # Add 1m aqi and color
                packet['pm2_5_1m_aqi'] = AQI.compute_pm2_5_aqi(packet['pm2_5_1m'])
                packet['pm2_5_1m_aqi_color'] = AQI.compute_pm2_5_aqi_color(packet['pm2_5_1m_aqi'])

                # Put aqi and color in the packet.
                if cfg.concentrations.pm_2p5 is not None:
                    packet['pm2_5_aqi'] = AQI.compute_pm2_5_aqi(cfg.concentrations.pm_2p5_last)
                    packet['pm2_5_aqi_color'] = AQI.compute_pm2_5_aqi_color(packet['pm2_5_aqi'])
                    packet['pm2_5_lrapa'] = AQI.compute_pm2_5_lrapa(cfg.concentrations.pm_2p5_last)
                    packet['pm2_5_lrapa_aqi'] = AQI.compute_pm2_5_aqi(packet['pm2_5_lrapa'])
                    packet['pm2_5_lrapa_aqi_color'] = AQI.compute_pm2_5_aqi_color(packet['pm2_5_lrapa_aqi'])
                    packet['pm2_5_unbc'] = AQI.compute_pm2_5_unbc(cfg.concentrations.pm_2p5_last)
                    packet['pm2_5_unbc_aqi'] = AQI.compute_pm2_5_aqi(packet['pm2_5_unbc'])
                    packet['pm2_5_unbc_aqi_color'] = AQI.compute_pm2_5_aqi_color(packet['pm2_5_unbc_aqi'])

                # And insert nowcast for pm 2.5 and 10 as some might want to report that.
                # If nowcast not available, don't substitute.
                if cfg.concentrations.pm_2p5_nowcast is not None:
                    packet['pm2_5_nowcast']  = cfg.concentrations.pm_2p5_nowcast
                    packet['pm2_5_nowcast_aqi'] = AQI.compute_pm2_5_aqi(cfg.concentrations.pm_2p5_nowcast)
                    packet['pm2_5_nowcast_aqi_color'] = AQI.compute_pm2_5_aqi_color(packet['pm2_5_nowcast_aqi'])
                if cfg.concentrations.pm_10_nowcast is not None:
                    packet['pm10_0_nowcast'] = cfg.concentrations.pm_10_nowcast
            else:
                log.error('Found no fresh concentrations to insert.')

    def configure_sources(config_dict):
        sources = []
        idx = 0
        while True:
            idx += 1
            try:
                source = Source(config_dict, 'Sensor%d' % idx)
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
        'avg': "SELECT AVG(pm2_5), usUnits FROM %(table_name)s "
               "WHERE dateTime > %(start)s AND dateTime <= %(stop)s AND pm2_5 IS NOT NULL",
        'count': "SELECT COUNT(dateTime), usUnits FROM %(table_name)s "
                 "WHERE dateTime > %(start)s AND dateTime <= %(stop)s AND pm2_5 IS NOT NULL",
        'first': "SELECT pm2_5, usUnits FROM %(table_name)s "
                 "WHERE dateTime = (SELECT MIN(dateTime) FROM %(table_name)s "
                 "WHERE dateTime > %(start)s AND dateTime <= %(stop)s AND pm2_5 IS NOT NULL",
        'last': "SELECT pm2_5, usUnits FROM %(table_name)s "
                "WHERE dateTime = (SELECT MAX(dateTime) FROM %(table_name)s "
                "WHERE dateTime > %(start)s AND dateTime <= %(stop)s AND pm2_5 IS NOT NULL",
        'min': "SELECT pm2_5, usUnits FROM %(table_name)s "
               "WHERE dateTime > %(start)s AND dateTime <= %(stop)s AND pm2_5 IS NOT NULL "
               "ORDER BY pm2_5 ASC LIMIT 1;",
        'max': "SELECT pm2_5, usUnits FROM %(table_name)s "
               "WHERE dateTime > %(start)s AND dateTime <= %(stop)s AND pm2_5 IS NOT NULL "
               "ORDER BY pm2_5 DESC LIMIT 1;",
        'sum': "SELECT SUM(pm2_5), usUnits FROM %(table_name)s "
               "WHERE dateTime > %(start)s AND dateTime <= %(stop)s AND pm2_5 IS NOT NULL)",
    }

    @staticmethod
    def compute_pm2_5_aqi(pm2_5):
        #             U.S. EPA PM2.5 AQI
        #
        #  AQI Category  AQI Value  24-hr PM2.5
        # Good             0 -  50    0.0 -  12.0
        # Moderate        51 - 100   12.1 -  35.4
        # USG            101 - 150   35.5 -  55.4
        # Unhealthy      151 - 200   55.5 - 150.4
        # Very Unhealthy 201 - 300  150.5 - 250.4
        # Hazardous      301 - 400  250.5 - 350.4
        # Hazardous      401 - 500  350.5 - 500.4

        if pm2_5 is None:
            return None

        # The EPA standard for AQI says to truncate PM2.5 to one decimal place.
        # See https://www3.epa.gov/airnow/aqi-technical-assistance-document-sept2018.pdf
        x = math.trunc(pm2_5 * 10) / 10

        if x <= 12.0: # Good
            return x / 12.0 * 50
        elif x <= 35.4: # Moderate
            return (x - 12.1) / 23.3 * 49.0 + 51.0
        elif x <= 55.4: # Unhealthy for senstive
            return (x - 35.5) / 19.9 * 49.0 + 101.0
        elif x <= 150.4: # Unhealthy
            return (x - 55.5) / 94.9 * 49.0 + 151.0
        elif x <= 250.4: # Very Unhealthy
            return (x - 150.5) / 99.9 * 99.0 + 201.0
        elif x <= 350.4: # Hazardous
            return (x - 250.5) / 99.9 * 99.0 + 301.0
        else: # Hazardous
            return (x - 350.5) / 149.9 * 99.0 + 401.0

    @staticmethod
    def compute_pm2_5_aqi_color(pm2_5_aqi):
        if pm2_5_aqi is None:
            return None

        if pm2_5_aqi <= 50:
            return 128 << 8                 # Green
        elif pm2_5_aqi <= 100:
            return (255 << 16) + (255 << 8) # Yellow
        elif pm2_5_aqi <=  150:
            return (255 << 16) + (140 << 8) # Orange
        elif pm2_5_aqi <= 200: 
            return 255 << 16                # Red
        elif pm2_5_aqi <= 300:
            return (128 << 16) + 128        # Purple
        else:
            return 128 << 16                # Maroon

    def compute_pm2_5_lrapa(pm2_5):
        if pm2_5 is None:
            return None

        # https://www.lrapa.org/DocumentCenter/View/4147/AirLink-Correction-Summary
        value = pm2_5 / 2 - 0.66
        if value < 0:
            value = 0
        return value

    def compute_pm2_5_unbc(pm2_5):
        if pm2_5 is None:
            return None

        # 0 μg m⁻³:
        # PM₂.₅ = PA
        #
        # 0+ - 20 μg m⁻³:
        # PM₂.₅ = 0.6 x PA + 3.4
        # 
        # 20+ - 200 μg m⁻³:
        # PM₂.₅ = 0.6 x PA + 2.5
        # 
        # 200+ μg m⁻³:
        # PM₂.₅ = 1.6 x PA - 194
        if pm2_5 == 0:
            value = pm2_5
        elif pm2_5 <= 20:
            value = 0.6 * pm2_5 + 3.4
        elif pm2_5 <= 200:
            value = 0.6 * pm2_5 + 2.5
        else:
            value = 1.6 * pm2_5 - 194
        return value

    @staticmethod
    def get_scalar(obs_type, record, db_manager=None):
        log.debug('get_scalar(%s)' % obs_type)
        if obs_type not in [ 'pm2_5_aqi', 'pm2_5_aqi_color', 'pm2_5_lrapa',
                             'pm2_5_lrapa_aqi', 'pm2_5_lrapa_aqi_color',
                             'pm2_5_unbc', 'pm2_5_unbc_aqi', 'pm2_5_unbc_aqi_color' ]:
            raise weewx.UnknownType(obs_type)
        log.debug('get_scalar(%s)' % obs_type)
        if record is None:
            log.debug('get_scalar called where record is None.')
            raise weewx.CannotCalculate(obs_type)
        if 'pm2_5' not in record:
            log.info('get_scalar called where record does not contain pm2_5.')
            raise weewx.CannotCalculate(obs_type)
        if record['pm2_5'] is None:
            log.info('get_scalar called where record[pm2_5] is None.')
            raise weewx.CannotCalculate(obs_type)
        try:
            pm2_5 = record['pm2_5']
            if obs_type == 'pm2_5_aqi':
                value = AQI.compute_pm2_5_aqi(pm2_5)
            if obs_type == 'pm2_5_aqi_color':
                value = AQI.compute_pm2_5_aqi_color(AQI.compute_pm2_5_aqi(pm2_5))
            elif obs_type == 'pm2_5_lrapa':
                value = AQI.compute_pm2_5_lrapa(pm2_5)
            elif obs_type == 'pm2_5_lrapa_aqi':
                pm2_5_lrapa = AQI.compute_pm2_5_lrapa(pm2_5)
                value = AQI.compute_pm2_5_aqi(pm2_5_lrapa)
            elif obs_type == 'pm2_5_lrapa_aqi_color':
                value = AQI.compute_pm2_5_aqi_color(AQI.compute_pm2_5_aqi(AQI.compute_pm2_5_lrapa(pm2_5)))
            elif obs_type == 'pm2_5_unbc':
                value = AQI.compute_pm2_5_unbc(pm2_5)
            elif obs_type == 'pm2_5_unbc_aqi':
                pm2_5_unbc = AQI.compute_pm2_5_unbc(pm2_5)
                value = AQI.compute_pm2_5_aqi(pm2_5_unbc)
            elif obs_type == 'pm2_5_unbc_aqi_color':
                value = AQI.compute_pm2_5_aqi_color(AQI.compute_pm2_5_aqi(AQI.compute_pm2_5_unbc(pm2_5)))
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

        if obs_type not in [ 'pm2_5_aqi', 'pm2_5_aqi_color', 'pm2_5_lrapa',
                             'pm2_5_lrapa_aqi', 'pm2_5_lrapa_aqi_color',
                             'pm2_5_unbc', 'pm2_5_unbc_aqi', 'pm2_5_unbc_aqi_color' ]:
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
            sql_str = 'SELECT dateTime, usUnits, interval, pm2_5 FROM %s ' \
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
                if obs_type == 'pm2_5_aqi_color':
                    value = AQI.compute_pm2_5_aqi_color(AQI.compute_pm2_5_aqi(pm2_5))
                elif obs_type == 'pm2_5_lrapa':
                    value = AQI.compute_pm2_5_lrapa(pm2_5)
                elif obs_type == 'pm2_5_lrapa_aqi':
                    value = AQI.compute_pm2_5_aqi(AQI.compute_pm2_5_lrapa(pm2_5))
                elif obs_type == 'pm2_5_lrapa_aqi_color':
                    value = AQI.compute_pm2_5_aqi_color(AQI.compute_pm2_5_aqi(AQI.compute_pm2_5_lrapa(pm2_5)))
                elif obs_type == 'pm2_5_unbc':
                    value = AQI.compute_pm2_5_unbc(pm2_5)
                elif obs_type == 'pm2_5_unbc_aqi':
                    value = AQI.compute_pm2_5_aqi(AQI.compute_pm2_5_unbc(pm2_5))
                elif obs_type == 'pm2_5_unbc_aqi_color':
                    value = AQI.compute_pm2_5_aqi_color(AQI.compute_pm2_5_aqi(AQI.compute_pm2_5_unbc(pm2_5)))
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
        table.

        obs_type

        timespan: An instance of weeutil.Timespan with the time period over which aggregation is to
        be done.

        aggregate_type: The type of aggregation to be done. For this function, must be 'avg',
        'sum', 'count', 'first', 'last', 'min', or 'max'. Anything else will cause
        weewx.UnknownAggregation to be raised.

        db_manager: An instance of weewx.manager.Manager or subclass.

        option_dict: Not used in this version.

        returns: A ValueTuple containing the result.
        """
        if obs_type not in [ 'pm2_5_aqi', 'pm2_5_aqi_color', 'pm2_5_lrapa',
                             'pm2_5_lrapa_aqi', 'pm2_5_lrapa_aqi_color',
                             'pm2_5_unbc', 'pm2_5_unbc_aqi', 'pm2_5_unbc_aqi_color' ]:
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
            'table_name': db_manager.table_name
        }

        select_stmt = AQI.agg_sql_dict[aggregate_type] % interpolation_dict
        row = db_manager.getSql(select_stmt)
        if row:
            value, std_unit_system = row
        else:
            value = None
            std_unit_system = None

        if value is not None:
            if obs_type == 'pm2_5_aqi':
                value = AQI.compute_pm2_5_aqi(value)
            if obs_type == 'pm2_5_aqi_color':
                value = AQI.compute_pm2_5_aqi_color(AQI.compute_pm2_5_aqi(value))
            elif obs_type == 'pm2_5_lrapa':
                value = AQI.compute_pm2_5_lrapa(value)
            elif obs_type == 'pm2_5_lrapa_aqi':
                value = AQI.compute_pm2_5_aqi(AQI.compute_pm2_5_lrapa(value))
            elif obs_type == 'pm2_5_lrapa_aqi_color':
                value = AQI.compute_pm2_5_aqi_color(AQI.compute_pm2_5_aqi(AQI.compute_pm2_5_lrapa(value)))
            elif obs_type == 'pm2_5_unbc':
                value = AQI.compute_pm2_5_unbc(value)
            elif obs_type == 'pm2_5_unbc_aqi':
                value = AQI.compute_pm2_5_aqi(AQI.compute_pm2_5_unbc(value))
            elif obs_type == 'pm2_5_unbc_aqi_color':
                value = AQI.compute_pm2_5_aqi_color(AQI.compute_pm2_5_aqi(AQI.compute_pm2_5_unbc(value)))
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
                          help='test the data collector')
        parser.add_option('--hostname', dest='hostname', action='store',
                          help='hostname to use with --test-collector')
        parser.add_option('--port', dest='port', action='store',
                          type=int, default=80,
                          help="port to use with --test-collector. Default is '80'")
        (options, args) = parser.parse_args()

        weeutil.logger.setup('airlink', {})

        if options.te:
            if not options.hostname:
                parser.error('--test-collector requires --hostname argument')
            test_extension(options.hostname, options.port)

    def test_extension(hostname, port):
        sources = [Source({'Sensor1': { 'enable': True, 'hostname': hostname, 'port': port, 'timeout': 2}}, 'Sensor1')]
        cfg = Configuration(
            lock             = threading.Lock(),
            concentrations   = None,
            archive_interval = 300,
            archive_delay    = 15,
            poll_interval    = 5,
            sources          = sources)
        while True:
            with cfg.lock:
                cfg.concentrations = get_concentrations(cfg)
            print('%s:%d concentrations: %s' % (cfg.sources[0].hostname, cfg.sources[0].port, cfg.concentrations))
            packet = {}
            AirLink.fill_in_packet(cfg, packet)
            print('Fields to be inserted into packet: %s' % packet)
            time.sleep(cfg.poll_interval)

    main()
