#
#    See the file LICENSE.txt for your full rights.
#
"""Hermetic tests for weewx-airlink.  No network access: everything from the
fetch stack down is exercised with mocks, and the xtype SQL paths run
against an in-memory SQLite database."""

import copy
import datetime
import logging
import sqlite3
import threading
import time
import unittest

from typing import Any, Dict
from unittest import mock

import weeutil.logger
import weeutil.weeutil
import weewx
import weewx.units
import weewx.xtypes

import user.airlink

from user.airlink import AirLink, AQI, Concentrations, Configuration, Source

log = logging.getLogger(__name__)

# Set up logging using the defaults.
weeutil.logger.setup('test_config', {})

# A class whose *name* matches weewxd's shutdown exception.  weewxd raises
# Terminate from its SIGTERM handler; airlink.py recognizes it by name.
Terminate = type('Terminate', (Exception,), {})

# A type-6 /v1/current_conditions response as captured from a real AirLink.
VALID_RESPONSE: Dict[str, Any] = {
    'data': {
        'did': '001D0A100214',
        'name': 'paloaltoweather.com',
        'ts': 1600485172,
        'conditions': [{
            'lsid': 347825,
            'data_structure_type': 6,
            'temp': 70.8,
            'hum': 67.6,
            'dew_point': 59.5,
            'wet_bulb': 62.8,
            'heat_index': 70.9,
            'pm_1_last': 4,
            'pm_2p5_last': 9,
            'pm_10_last': 11,
            'pm_1': 5.43,
            'pm_2p5': 8.23,
            'pm_2p5_last_1_hour': 8.91,
            'pm_2p5_last_3_hours': 9.09,
            'pm_2p5_last_24_hours': 9.09,
            'pm_2p5_nowcast': 8.96,
            'pm_10': 11.90,
            'pm_10_last_1_hour': 12.54,
            'pm_10_last_3_hours': 13.11,
            'pm_10_last_24_hours': 13.11,
            'pm_10_nowcast': 12.84,
            'last_report_time': 1600485172,
            'pct_pm_data_last_1_hour': 100,
            'pct_pm_data_last_3_hours': 91,
            'pct_pm_data_nowcast': 22,
            'pct_pm_data_last_24_hours': 11,
        }],
    },
    'error': None,
}

def valid_response(last_report_time=None):
    """A deep copy of VALID_RESPONSE; by default the reading is fresh."""
    if last_report_time is None:
        last_report_time = int(time.time()) - 10
    j = copy.deepcopy(VALID_RESPONSE)
    j['data']['conditions'][0]['last_report_time'] = last_report_time
    return j

def conditions(j):
    return j['data']['conditions'][0]

def minimal_response():
    """A fresh AirLink (or one just rebooted) reports null for everything
    but the instantaneous fields; lsid can be null too."""
    j = valid_response()
    c = conditions(j)
    c['lsid'] = None
    for name in ['pm_1', 'pm_2p5', 'pm_2p5_last_1_hour', 'pm_2p5_last_3_hours',
                 'pm_2p5_last_24_hours', 'pm_2p5_nowcast', 'pm_10',
                 'pm_10_last_1_hour', 'pm_10_last_3_hours',
                 'pm_10_last_24_hours', 'pm_10_nowcast']:
        c[name] = None
    for name in ['pct_pm_data_last_1_hour', 'pct_pm_data_last_3_hours',
                 'pct_pm_data_nowcast', 'pct_pm_data_last_24_hours']:
        c[name] = None
    return j

def type_5_response(last_report_time=None):
    """A type-5 response (early firmware): pm_10p0* field names."""
    j = valid_response(last_report_time)
    c = conditions(j)
    c['data_structure_type'] = 5
    for suffix in ['', '_last_1_hour', '_last_3_hours', '_last_24_hours',
                   '_nowcast']:
        c['pm_10p0' + suffix] = c.pop('pm_10' + suffix)
    return j

class FakeResponse:
    """Just enough of requests.Response for collect_data."""
    def __init__(self, j, status_error=None):
        self._j = j
        self._status_error = status_error
    def raise_for_status(self):
        if self._status_error is not None:
            raise self._status_error
    def json(self):
        return self._j

class FakeDBManager:
    """Just enough of weewx.manager.Manager for the AQI xtype: a table name
    plus getSql/genSql running against a real SQLite connection."""
    def __init__(self, conn, table_name='archive'):
        self.conn = conn
        self.table_name = table_name
    def getSql(self, sql, sqlargs=()):
        return self.conn.execute(sql, sqlargs).fetchone()
    def genSql(self, sql, sqlargs=()):
        yield from self.conn.execute(sql, sqlargs)

def make_cfg(sources=None, archive_interval=300, concentrations=None):
    return Configuration(
        lock             = threading.Lock(),
        concentrations   = concentrations,
        stale_logged     = False,
        archive_interval = archive_interval,
        poll_interval    = 5,
        sources          = sources if sources is not None else [])

def make_source(name='Sensor1', enable=True, hostname='host', **kwargs):
    d = {'enable': enable, 'hostname': hostname}
    d.update(kwargs)
    return Source({name: d}, name)

def fresh_concentrations(**overrides):
    kwargs = dict(
        timestamp      = time.time(),
        pm_1_last      = 4.0,
        pm_2p5_last    = 9.0,
        pm_10_last     = 11.0,
        pm_1           = 5.43,
        pm_2p5         = 8.23,
        pm_10          = 11.9,
        pm_2p5_nowcast = 8.96,
        pm_10_nowcast  = 12.84,
        hum            = 67.6,
        temp           = 70.8)
    kwargs.update(overrides)
    return Concentrations(**kwargs)

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
# Above 325.4, AQI values continue past 500 on the same (Hazardous) slope;
# there is no upper cap.

class TestComputeAqi(unittest.TestCase):

    def test_good(self):
        self.assertEqual(AQI.compute_pm2_5_aqi(0.0), 0)
        self.assertEqual(AQI.compute_pm2_5_aqi(6.0), 33)
        self.assertEqual(AQI.compute_pm2_5_aqi(9.0), 50)
        # 9.099 is truncated to 9.0
        self.assertEqual(AQI.compute_pm2_5_aqi(9.099), 50)

    def test_moderate(self):
        self.assertEqual(AQI.compute_pm2_5_aqi(9.1), 51)
        self.assertEqual(AQI.compute_pm2_5_aqi(21.8), 75)
        self.assertEqual(AQI.compute_pm2_5_aqi(35.4), 100)
        self.assertEqual(AQI.compute_pm2_5_aqi(35.499), 100)

    def test_usg(self):
        self.assertEqual(AQI.compute_pm2_5_aqi(35.5), 101)
        self.assertEqual(AQI.compute_pm2_5_aqi(45.4), 125)
        self.assertEqual(AQI.compute_pm2_5_aqi(55.4), 150)

    def test_unhealthy(self):
        self.assertEqual(AQI.compute_pm2_5_aqi(55.5), 151)
        self.assertEqual(AQI.compute_pm2_5_aqi(90.5), 176)
        self.assertEqual(AQI.compute_pm2_5_aqi(125.4), 200)

    def test_very_unhealthy(self):
        self.assertEqual(AQI.compute_pm2_5_aqi(125.5), 201)
        self.assertEqual(AQI.compute_pm2_5_aqi(175.4), 250)
        self.assertEqual(AQI.compute_pm2_5_aqi(225.4), 300)

    def test_hazardous(self):
        # Per the May 2024 AirNow TAD (breakpoint-table footnote 4), the
        # concentration for AQI 500 is 325.4: slope 199 AQI per 99.9 ug/m^3.
        self.assertEqual(AQI.compute_pm2_5_aqi(225.5), 301)
        self.assertEqual(AQI.compute_pm2_5_aqi(275.4), 400)
        self.assertEqual(AQI.compute_pm2_5_aqi(325.4), 500)

    def test_above_500_extrapolates_hazardous_slope(self):
        # The TAD FAQ: values above 500 are "based on the same linear slope
        # as the AQI values between 301 and 500".  No upper cap.
        self.assertEqual(AQI.compute_pm2_5_aqi(375.0), 599)
        self.assertEqual(AQI.compute_pm2_5_aqi(425.0), 698)
        self.assertEqual(AQI.compute_pm2_5_aqi(1000.0), 1844)

    def test_negative_concentration_maps_to_zero(self):
        # A (bogus) negative concentration must not map below 0.
        self.assertEqual(AQI.compute_pm2_5_aqi(-5.0), 0)

class TestComputeAqiColor(unittest.TestCase):

    GREEN  = 228 << 8
    YELLOW = (255 << 16) + (255 << 8)
    ORANGE = (255 << 16) + (126 << 8)
    RED    = 255 << 16
    PURPLE = (143 << 16) + (63 << 8) + 151
    MAROON = (126 << 16) + 35

    def test_category_boundaries(self):
        for aqi, expected in [
                (  0, self.GREEN),  ( 25, self.GREEN),  ( 50, self.GREEN),
                ( 51, self.YELLOW), ( 75, self.YELLOW), (100, self.YELLOW),
                (101, self.ORANGE), (125, self.ORANGE), (150, self.ORANGE),
                (151, self.RED),    (175, self.RED),    (200, self.RED),
                (201, self.PURPLE), (250, self.PURPLE), (300, self.PURPLE),
                (301, self.MAROON), (400, self.MAROON), (500, self.MAROON),
                # Above 500 is still Hazardous/Maroon.
                (501, self.MAROON), (750, self.MAROON)]:
            self.assertEqual(AQI.compute_pm2_5_aqi_color(aqi), expected,
                             'wrong color for AQI %d' % aqi)

class TestIsType(unittest.TestCase):

    def test_matching_type(self):
        self.assertTrue(user.airlink.is_type({'a': 1}, int, 'a'))
        self.assertTrue(user.airlink.is_type({'a': 1.5}, float, 'a'))

    def test_wrong_type(self):
        self.assertFalse(user.airlink.is_type({'a': 'nan'}, int, 'a'))
        self.assertFalse(user.airlink.is_type({'a': 1}, float, 'a'))

    def test_none_ok(self):
        self.assertTrue(user.airlink.is_type({'a': None}, int, 'a', none_ok=True))
        self.assertFalse(user.airlink.is_type({'a': None}, int, 'a'))

    def test_missing_key(self):
        self.assertFalse(user.airlink.is_type({'a': 1}, int, 'zz'))

    def test_exception_swallowed(self):
        class Exploder:
            def __getitem__(self, key):
                raise RuntimeError('boom')
        self.assertFalse(user.airlink.is_type(Exploder(), int, 'a'))

class TestConvertType5To6(unittest.TestCase):

    def test_conversion(self):
        j = type_5_response(1601320120)
        user.airlink.convert_data_structure_type_5_to_6(j)
        c = conditions(j)
        self.assertEqual(c['data_structure_type'], 6)
        self.assertEqual(c['pm_10'], 11.90)
        self.assertEqual(c['pm_10_last_1_hour'], 12.54)
        self.assertEqual(c['pm_10_last_3_hours'], 13.11)
        self.assertEqual(c['pm_10_last_24_hours'], 13.11)
        self.assertEqual(c['pm_10_nowcast'], 12.84)
        # The type-5 names are nulled out.
        self.assertIsNone(c['pm_10p0'])
        self.assertIsNone(c['pm_10p0_nowcast'])
        sane, reason = user.airlink.is_sane(j)
        self.assertTrue(sane, reason)

    def test_missing_field_swallowed(self):
        # A malformed type-5 record must not raise; the sanity check
        # rejects it afterwards.
        j = type_5_response()
        del conditions(j)['pm_10p0']
        user.airlink.convert_data_structure_type_5_to_6(j)
        self.assertEqual(conditions(j)['data_structure_type'], 5)
        sane, _ = user.airlink.is_sane(j)
        self.assertFalse(sane)

class TestIsSane(unittest.TestCase):

    def test_valid_response(self):
        sane, reason = user.airlink.is_sane(valid_response())
        self.assertTrue(sane, reason)

    def test_minimal_response(self):
        # Nullable fields (lsid, averages, pct_pm_data_*) may all be null.
        sane, reason = user.airlink.is_sane(minimal_response())
        self.assertTrue(sane, reason)

    def test_error_response(self):
        j = valid_response()
        j['error'] = {'code': 409, 'message': 'Error'}
        sane, reason = user.airlink.is_sane(j)
        self.assertFalse(sane)
        self.assertIn('Error', reason)

    def test_missing_data(self):
        sane, reason = user.airlink.is_sane({'data': None, 'error': None})
        self.assertFalse(sane)
        self.assertIn('data', reason)

    def test_bad_name(self):
        j = valid_response()
        j['data']['name'] = None
        sane, reason = user.airlink.is_sane(j)
        self.assertFalse(sane)
        self.assertIn('name', reason)

    def test_bad_ts(self):
        j = valid_response()
        j['data']['ts'] = 'abc'
        sane, reason = user.airlink.is_sane(j)
        self.assertFalse(sane)
        self.assertIn('ts', reason)

    def test_bad_conditions(self):
        j = valid_response()
        j['data']['conditions'] = None
        sane, reason = user.airlink.is_sane(j)
        self.assertFalse(sane)
        self.assertIn('conditions', reason)

    def test_empty_conditions(self):
        j = valid_response()
        j['data']['conditions'] = []
        sane, reason = user.airlink.is_sane(j)
        self.assertFalse(sane)
        self.assertIn('one element', reason)

    def test_unconverted_type_5(self):
        # data_structure_type 5 must be converted before the sanity check.
        sane, reason = user.airlink.is_sane(type_5_response())
        self.assertFalse(sane)
        self.assertIn('data_structure_type', reason)

    def test_bad_data_structure_type(self):
        j = valid_response()
        conditions(j)['data_structure_type'] = 'abc'
        sane, reason = user.airlink.is_sane(j)
        self.assertFalse(sane)
        self.assertIn('data_structure_type', reason)

    def test_bad_lsid(self):
        j = valid_response()
        conditions(j)['lsid'] = 'abc'
        sane, reason = user.airlink.is_sane(j)
        self.assertFalse(sane)
        self.assertIn('lsid', reason)

    def test_missing_last_report_time(self):
        j = valid_response()
        del conditions(j)['last_report_time']
        sane, reason = user.airlink.is_sane(j)
        self.assertFalse(sane)
        self.assertIn('last_report_time', reason)

    def test_bad_pm_last(self):
        j = valid_response()
        conditions(j)['pm_2p5_last'] = 'nan'
        sane, reason = user.airlink.is_sane(j)
        self.assertFalse(sane)
        self.assertEqual(reason, 'Missing or malformed "pm_2p5_last" field')

    def test_null_pm_last_ok(self):
        j = valid_response()
        conditions(j)['pm_2p5_last'] = None
        sane, reason = user.airlink.is_sane(j)
        self.assertTrue(sane, reason)

    def test_bad_temp(self):
        j = valid_response()
        conditions(j)['temp'] = 'nan'
        sane, reason = user.airlink.is_sane(j)
        self.assertFalse(sane)
        self.assertIn('temp', reason)

    def test_null_hum_not_ok(self):
        j = valid_response()
        conditions(j)['hum'] = None
        sane, reason = user.airlink.is_sane(j)
        self.assertFalse(sane)
        self.assertIn('hum', reason)

    def test_bad_concentration(self):
        j = valid_response()
        conditions(j)['pm_1'] = 'abc'
        sane, reason = user.airlink.is_sane(j)
        self.assertFalse(sane)
        self.assertEqual(reason, 'Missing or malformed "pm_1" field')

class TestCollectData(unittest.TestCase):

    def collect(self, response, archive_interval=300):
        with mock.patch('user.airlink.requests.get', return_value=response):
            return user.airlink.collect_data('airlink.example', 80, 10,
                                             archive_interval)

    def test_successful_fetch(self):
        j = valid_response()
        record = self.collect(FakeResponse(j))
        self.assertIsNotNone(record)
        self.assertEqual(record['dateTime'], conditions(j)['last_report_time'])
        self.assertEqual(record['usUnits'], weewx.US)
        # The instantaneous fields land under WeeWX names...
        self.assertEqual(record['pm1_0'], 4)
        self.assertEqual(record['pm2_5'], 9)
        self.assertEqual(record['pm10_0'], 11)
        # ...and the whole Davis family is copied through.
        self.assertEqual(record['pm_2p5'], 8.23)
        self.assertEqual(record['pm_2p5_nowcast'], 8.96)
        self.assertEqual(record['pm_10_nowcast'], 12.84)
        self.assertEqual(record['temp'], 70.8)
        self.assertEqual(record['hum'], 67.6)

    def test_type_5_response_converted(self):
        record = self.collect(FakeResponse(type_5_response()))
        self.assertIsNotNone(record)
        self.assertEqual(record['pm_10'], 11.90)
        self.assertEqual(record['pm_10_nowcast'], 12.84)

    def test_device_error_returns_none(self):
        j = valid_response()
        j['error'] = {'code': 409, 'message': 'Error'}
        self.assertIsNone(self.collect(FakeResponse(j)))

    def test_insane_reading_returns_none(self):
        j = valid_response()
        conditions(j)['pm_1'] = 'abc'
        self.assertIsNone(self.collect(FakeResponse(j)))

    def test_old_reading_returns_none(self):
        j = valid_response(int(time.time()) - 301)
        self.assertIsNone(self.collect(FakeResponse(j)))

    def test_reboot_reading_returns_none(self):
        # After a reboot (before time sync), last_report_time is seconds
        # since boot and the averages are null.
        j = minimal_response()
        conditions(j)['last_report_time'] = 100
        self.assertIsNone(self.collect(FakeResponse(j)))

    def test_connection_error_returns_none(self):
        import requests
        with mock.patch('user.airlink.requests.get',
                        side_effect=requests.exceptions.ConnectionError('no route')):
            self.assertIsNone(
                user.airlink.collect_data('airlink.example', 80, 10, 300))

    def test_http_error_returns_none(self):
        import requests
        resp = FakeResponse(None, status_error=requests.exceptions.HTTPError('500'))
        self.assertIsNone(self.collect(resp))

    def test_malformed_json_returns_none(self):
        # A response without 'data' raises deep in collect_data; the broad
        # handler turns it into None.
        self.assertIsNone(self.collect(FakeResponse({'error': None})))

    def test_falsy_response_returns_none(self):
        class FalsyResponse(FakeResponse):
            def __bool__(self):
                return False
        self.assertIsNone(self.collect(FalsyResponse(valid_response())))

class TestPopulateRecord(unittest.TestCase):

    def test_all_fields(self):
        j = valid_response(1600485172)
        record = user.airlink.populate_record(1600485172, j)
        self.assertEqual(record['dateTime'], 1600485172)
        self.assertEqual(record['usUnits'], weewx.US)
        # The instantaneous readings land under the WeeWX column names.
        self.assertEqual(record['pm1_0'], 4)
        self.assertEqual(record['pm2_5'], 9)
        self.assertEqual(record['pm10_0'], 11)
        for name in ['last_report_time', 'temp', 'hum', 'dew_point',
                     'wet_bulb', 'heat_index', 'pct_pm_data_last_1_hour',
                     'pct_pm_data_last_3_hours', 'pct_pm_data_nowcast',
                     'pct_pm_data_last_24_hours', 'pm_1', 'pm_1_last',
                     'pm_2p5', 'pm_2p5_last', 'pm_2p5_last_1_hour',
                     'pm_2p5_last_3_hours', 'pm_2p5_last_24_hours',
                     'pm_2p5_nowcast', 'pm_10', 'pm_10_last',
                     'pm_10_last_1_hour', 'pm_10_last_3_hours',
                     'pm_10_last_24_hours', 'pm_10_nowcast']:
            self.assertEqual(record[name], conditions(j)[name], name)

    def test_missing_fields_are_none(self):
        j = valid_response()
        del conditions(j)['pm_2p5_nowcast']
        record = user.airlink.populate_record(1600485172, j)
        self.assertIsNone(record['pm_2p5_nowcast'])

class TestTerminatePassThrough(unittest.TestCase):
    """weewxd stops by raising Terminate from its SIGTERM handler inside
    whatever the main thread is executing.  The broad exception handlers on
    main-thread paths must hand it back."""

    def test_reraise_if_terminate(self):
        with self.assertRaises(Terminate):
            user.airlink.reraise_if_terminate(Terminate())
        # Any other exception is not re-raised.
        self.assertIsNone(user.airlink.reraise_if_terminate(ValueError()))

    def test_collect_data_passes_terminate_through(self):
        with mock.patch('user.airlink.requests.get', side_effect=Terminate()):
            with self.assertRaises(Terminate):
                user.airlink.collect_data('airlink.example', 80, 10, 300)

    def test_collect_data_swallows_other_exceptions(self):
        with mock.patch('user.airlink.requests.get', side_effect=RuntimeError('boom')):
            self.assertIsNone(
                user.airlink.collect_data('airlink.example', 80, 10, 300))

    def test_is_type_passes_terminate_through(self):
        class Exploder:
            def __getitem__(self, key):
                raise Terminate()
        with self.assertRaises(Terminate):
            user.airlink.is_type(Exploder(), int, 'x')

    def test_convert_passes_terminate_through(self):
        class Exploder:
            def __getitem__(self, key):
                raise Terminate()
        with self.assertRaises(Terminate):
            user.airlink.convert_data_structure_type_5_to_6({'data': Exploder()})

    def test_convert_swallows_other_exceptions(self):
        self.assertIsNone(
            user.airlink.convert_data_structure_type_5_to_6({'data': None}))

class TestConfigureSources(unittest.TestCase):

    def test_sensors_in_order(self):
        config = {
            'Sensor1': {'enable': True,  'hostname': 's1'},
            'Sensor2': {'enable': False, 'hostname': 's2'},
            'Sensor3': {'enable': True,  'hostname': 's3'},
        }
        sources = AirLink.configure_sources(config)
        self.assertEqual([s.hostname for s in sources], ['s1', 's2', 's3'])

    def test_numbering_must_be_consecutive(self):
        config = {
            'Sensor1': {'enable': True, 'hostname': 's1'},
            'Sensor3': {'enable': True, 'hostname': 's3'},
        }
        sources = AirLink.configure_sources(config)
        self.assertEqual([s.hostname for s in sources], ['s1'])

    def test_defaults(self):
        s = Source({'Sensor1': {'hostname': 'h'}}, 'Sensor1')
        self.assertEqual(s.port, 80)
        self.assertEqual(s.timeout, 10)
        # enable defaults to False, and parses strings.
        self.assertFalse(s.enable)
        s = Source({'Sensor1': {'hostname': 'h', 'enable': 'true'}}, 'Sensor1')
        self.assertTrue(s.enable)

class TestGetConcentrations(unittest.TestCase):

    @staticmethod
    def fresh_record(**overrides):
        record = {
            'dateTime': int(time.time()) - 10,
            'pm_1_last': 4,
            'pm_2p5_last': 9,
            'pm_10_last': 11,
            'pm_1': 5.43,
            'pm_2p5': 8.23,
            'pm_10': 11.9,
            'pm_2p5_nowcast': 8.96,
            'pm_10_nowcast': 12.84,
            'hum': 67.6,
            'temp': 70.8,
        }
        record.update(overrides)
        return record

    def test_single_sensor(self):
        cfg = make_cfg(sources=[make_source()])
        record = self.fresh_record()
        with mock.patch('user.airlink.collect_data', return_value=record):
            c = user.airlink.get_concentrations(cfg)
        self.assertIsNotNone(c)
        self.assertEqual(c.timestamp, record['dateTime'])
        self.assertEqual(c.pm_1_last, 4)
        self.assertEqual(c.pm_2p5_last, 9)
        self.assertEqual(c.pm_10_last, 11)
        self.assertEqual(c.pm_2p5, 8.23)
        self.assertEqual(c.pm_2p5_nowcast, 8.96)
        self.assertEqual(c.pm_10_nowcast, 12.84)
        self.assertEqual(c.temp, 70.8)
        self.assertEqual(c.hum, 67.6)

    def test_disabled_source_skipped(self):
        s1 = make_source('Sensor1', enable=False, hostname='s1')
        s2 = make_source('Sensor2', hostname='s2')
        cfg = make_cfg(sources=[s1, s2])
        with mock.patch('user.airlink.collect_data',
                        return_value=self.fresh_record()) as m:
            c = user.airlink.get_concentrations(cfg)
        self.assertIsNotNone(c)
        m.assert_called_once()
        self.assertEqual(m.call_args[0][0], 's2')

    def test_failing_source_falls_through_to_next(self):
        s1 = make_source('Sensor1', hostname='s1')
        s2 = make_source('Sensor2', hostname='s2')
        cfg = make_cfg(sources=[s1, s2])
        with mock.patch('user.airlink.collect_data',
                        side_effect=[None, self.fresh_record()]) as m:
            c = user.airlink.get_concentrations(cfg)
        self.assertIsNotNone(c)
        self.assertEqual(m.call_count, 2)

    def test_old_reading_ignored(self):
        cfg = make_cfg(sources=[make_source()])
        record = self.fresh_record(dateTime=int(time.time()) - 301)
        with mock.patch('user.airlink.collect_data', return_value=record):
            self.assertIsNone(user.airlink.get_concentrations(cfg))

    def test_no_sources_respond(self):
        cfg = make_cfg(sources=[make_source()])
        with mock.patch('user.airlink.collect_data', return_value=None):
            self.assertIsNone(user.airlink.get_concentrations(cfg))

class TestFillInPacket(unittest.TestCase):

    def test_all_fields_inserted(self):
        cfg = make_cfg(concentrations=fresh_concentrations())
        packet = {}
        AirLink.fill_in_packet(cfg, packet)
        self.assertEqual(packet['pm1_0'], 4.0)
        self.assertEqual(packet['pm2_5'], 9.0)
        self.assertEqual(packet['pm10_0'], 11.0)
        self.assertEqual(packet['pm2_5_aqi'], AQI.compute_pm2_5_aqi(9.0))
        self.assertEqual(packet['pm2_5_aqi_color'],
                         AQI.compute_pm2_5_aqi_color(packet['pm2_5_aqi']))
        # 1-minute averages.
        self.assertEqual(packet['pm1_0_1m'], 5.43)
        self.assertEqual(packet['pm2_5_1m'], 8.23)
        self.assertEqual(packet['pm10_0_1m'], 11.9)
        self.assertEqual(packet['pm2_5_1m_aqi'], AQI.compute_pm2_5_aqi(8.23))
        self.assertEqual(packet['pm2_5_1m_aqi_color'],
                         AQI.compute_pm2_5_aqi_color(packet['pm2_5_1m_aqi']))
        # NowCast.
        self.assertEqual(packet['pm2_5_nowcast'], 8.96)
        self.assertEqual(packet['pm2_5_nowcast_aqi'], AQI.compute_pm2_5_aqi(8.96))
        self.assertEqual(packet['pm2_5_nowcast_aqi_color'],
                         AQI.compute_pm2_5_aqi_color(packet['pm2_5_nowcast_aqi']))
        self.assertEqual(packet['pm10_0_nowcast'], 12.84)

    def test_one_minute_averages_fall_back_to_last(self):
        cfg = make_cfg(concentrations=fresh_concentrations(
            pm_1=None, pm_2p5=None, pm_10=None))
        packet = {}
        AirLink.fill_in_packet(cfg, packet)
        self.assertEqual(packet['pm1_0_1m'], 4.0)
        self.assertEqual(packet['pm2_5_1m'], 9.0)
        self.assertEqual(packet['pm10_0_1m'], 11.0)

    def test_missing_nowcast_not_substituted(self):
        cfg = make_cfg(concentrations=fresh_concentrations(
            pm_2p5_nowcast=None, pm_10_nowcast=None))
        packet = {}
        AirLink.fill_in_packet(cfg, packet)
        self.assertNotIn('pm2_5_nowcast', packet)
        self.assertNotIn('pm2_5_nowcast_aqi', packet)
        self.assertNotIn('pm2_5_nowcast_aqi_color', packet)
        self.assertNotIn('pm10_0_nowcast', packet)
        self.assertIn('pm2_5', packet)

    def test_missing_pm_2p5_last_skips_pm2_5(self):
        cfg = make_cfg(concentrations=fresh_concentrations(pm_2p5_last=None))
        packet = {}
        AirLink.fill_in_packet(cfg, packet)
        self.assertNotIn('pm2_5', packet)
        self.assertNotIn('pm2_5_aqi', packet)
        self.assertNotIn('pm2_5_aqi_color', packet)
        # The 1-minute average is unaffected.
        self.assertEqual(packet['pm2_5_1m'], 8.23)

    def test_all_readings_none_inserts_nothing(self):
        # A fresh timestamp but no readings at all (every pm field null):
        # nothing is inserted, and nothing crashes.
        cfg = make_cfg(concentrations=fresh_concentrations(
            pm_1_last=None, pm_2p5_last=None, pm_10_last=None,
            pm_1=None, pm_2p5=None, pm_10=None,
            pm_2p5_nowcast=None, pm_10_nowcast=None))
        packet = {}
        AirLink.fill_in_packet(cfg, packet)
        self.assertEqual(packet, {})

    def test_stale_concentrations_not_inserted(self):
        cfg = make_cfg(concentrations=fresh_concentrations(
            timestamp=time.time() - 301))
        packet = {}
        AirLink.fill_in_packet(cfg, packet)
        self.assertEqual(packet, {})

    def test_no_concentrations_not_inserted(self):
        cfg = make_cfg()
        packet = {}
        AirLink.fill_in_packet(cfg, packet)
        self.assertEqual(packet, {})

    def test_stale_logged_once_per_outage(self):
        cfg = make_cfg(concentrations=fresh_concentrations(
            timestamp=time.time() - 301))
        AirLink.fill_in_packet(cfg, {})
        self.assertTrue(cfg.stale_logged)
        AirLink.fill_in_packet(cfg, {})
        self.assertTrue(cfg.stale_logged)
        # Fresh data again: flag resets and fields are inserted.
        with cfg.lock:
            cfg.concentrations = fresh_concentrations()
        packet = {}
        AirLink.fill_in_packet(cfg, packet)
        self.assertFalse(cfg.stale_logged)
        self.assertIn('pm2_5', packet)

class TestAirLinkInit(unittest.TestCase):
    """Startup wiring: config parsing, xtype registration, poller launch.
    The engine is a mock and both the initial fetch and the poller thread
    are patched out, so nothing touches the network."""

    def test_startup_with_sources(self):
        engine = mock.Mock()
        config = {
            'AirLink': {
                'Sensor1': {'enable': True, 'hostname': 'airlink1'},
                'Sensor2': {'enable': False, 'hostname': 'airlink2'},
            },
            'StdArchive': {'archive_interval': 300},
        }
        conc = fresh_concentrations()
        n_xtypes = len(weewx.xtypes.xtypes)
        try:
            with mock.patch('user.airlink.get_concentrations', return_value=conc) as gc, \
                 mock.patch('user.airlink.threading.Thread') as thread_cls:
                a = AirLink(engine, config)
            # The synchronous startup fetch ran and its result is stored.
            gc.assert_called_once()
            self.assertIs(a.cfg.concentrations, conc)
            self.assertEqual(a.cfg.archive_interval, 300)
            self.assertEqual(a.cfg.poll_interval, 5)
            self.assertEqual(len(a.cfg.sources), 2)   # disabled sources still parsed
            # The AQI xtype is registered.
            self.assertEqual(len(weewx.xtypes.xtypes), n_xtypes + 1)
            self.assertIsInstance(weewx.xtypes.xtypes[-1], AQI)
            # The poller thread was created as a daemon and started.
            _, kwargs = thread_cls.call_args
            self.assertTrue(kwargs['daemon'])
            self.assertEqual(kwargs['name'], 'AirLink')
            thread_cls.return_value.start.assert_called_once()
            # Bound to NEW_LOOP_PACKET.
            engine.bind.assert_called_once_with(weewx.NEW_LOOP_PACKET, a.new_loop_packet)
        finally:
            # Unregister anything this test added to the global xtypes list.
            del weewx.xtypes.xtypes[n_xtypes:]

    def test_startup_without_sources_is_inoperable(self):
        engine = mock.Mock()
        config = {
            'AirLink': {'Sensor1': {'enable': False, 'hostname': 'airlink1'}},
            'StdArchive': {'archive_interval': 300},
        }
        n_xtypes = len(weewx.xtypes.xtypes)
        with mock.patch('user.airlink.get_concentrations') as gc, \
             mock.patch('user.airlink.threading.Thread') as thread_cls:
            AirLink(engine, config)
        # No fetch, no xtype, no poller, no binding -- but no crash either.
        gc.assert_not_called()
        thread_cls.assert_not_called()
        engine.bind.assert_not_called()
        self.assertEqual(len(weewx.xtypes.xtypes), n_xtypes)

    def test_new_loop_packet_delegates_to_fill_in_packet(self):
        a = AirLink.__new__(AirLink)
        a.cfg = make_cfg(concentrations=fresh_concentrations())
        class Event:
            packet: Dict[str, Any] = {}
        event = Event()
        a.new_loop_packet(event)
        self.assertIn('pm2_5', event.packet)

class TestGetScalar(unittest.TestCase):
    """Every registered AQI observation type resolves against the field it
    is computed from: pm2_5 (the database column) as well as the loop-only
    pm2_5_1m and pm2_5_nowcast."""

    def test_aqi_types_from_their_source_fields(self):
        record = {'dateTime': 1700000000, 'usUnits': weewx.US,
                  'pm2_5': 21.8, 'pm2_5_1m': 9.0, 'pm2_5_nowcast': 55.5}
        for obs_type, expected in [('pm2_5_aqi', 75),
                                   ('pm2_5_1m_aqi', 50),
                                   ('pm2_5_nowcast_aqi', 151)]:
            vt = AQI.get_scalar(obs_type, record)
            self.assertEqual(vt.value, expected, obs_type)
            self.assertEqual(vt.unit, 'aqi')
            self.assertEqual(vt.group, 'air_quality_index')

    def test_aqi_color_types_from_their_source_fields(self):
        record = {'dateTime': 1700000000, 'usUnits': weewx.US,
                  'pm2_5': 21.8, 'pm2_5_1m': 9.0, 'pm2_5_nowcast': 55.5}
        for obs_type, expected in [
                ('pm2_5_aqi_color', TestComputeAqiColor.YELLOW),
                ('pm2_5_1m_aqi_color', TestComputeAqiColor.GREEN),
                ('pm2_5_nowcast_aqi_color', TestComputeAqiColor.RED)]:
            vt = AQI.get_scalar(obs_type, record)
            self.assertEqual(vt.value, expected, obs_type)
            self.assertEqual(vt.unit, 'aqi_color')
            self.assertEqual(vt.group, 'air_quality_color')

    def test_unknown_type(self):
        with self.assertRaises(weewx.UnknownType):
            AQI.get_scalar('outTemp', {'pm2_5': 1.0})

    def test_no_record(self):
        with self.assertRaises(weewx.CannotCalculate):
            AQI.get_scalar('pm2_5_aqi', None)

    def test_record_without_source_field(self):
        # Archive records have no pm2_5_1m; the 1m/nowcast AQI types only
        # resolve against loop packets.
        record = {'dateTime': 1700000000, 'usUnits': weewx.US, 'pm2_5': 21.8}
        with self.assertRaises(weewx.UnknownType):
            AQI.get_scalar('pm2_5_1m_aqi', record)

    def test_record_with_null_pm2_5(self):
        # Catchup records inserted at startup have pm2_5 of None.
        with self.assertRaises(weewx.UnknownType):
            AQI.get_scalar('pm2_5_aqi',
                           {'dateTime': 1700000000, 'usUnits': weewx.US, 'pm2_5': None})

    def test_record_without_usunits(self):
        with self.assertRaises(weewx.CannotCalculate):
            AQI.get_scalar('pm2_5_aqi', {'dateTime': 1700000000, 'pm2_5': 21.8})

class TestGetSeries(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute(
            "CREATE TABLE archive (dateTime INTEGER PRIMARY KEY, usUnits INTEGER, "
            "`interval` INTEGER, pm2_5 REAL)")
        self.db_manager = FakeDBManager(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_unknown_type(self):
        # Loop-only AQI types cannot be charted from the database.
        for obs_type in ['outTemp', 'pm2_5_1m_aqi', 'pm2_5_nowcast_aqi']:
            with self.assertRaises(weewx.UnknownType):
                AQI.get_series(obs_type, weeutil.weeutil.TimeSpan(0, 4000),
                               self.db_manager)

    def test_series_without_aggregation(self):
        rows = [(3600, weewx.US, 5, 9.0), (3900, weewx.US, 5, 35.4)]
        self.conn.executemany("INSERT INTO archive VALUES (?, ?, ?, ?)", rows)
        start_vt, stop_vt, data_vt = AQI.get_series(
            'pm2_5_aqi', weeutil.weeutil.TimeSpan(0, 4000), self.db_manager)
        self.assertEqual(start_vt.value, [3300, 3600])
        self.assertEqual(stop_vt.value, [3600, 3900])
        self.assertEqual(data_vt.value, [50, 100])
        self.assertEqual(data_vt.unit, 'aqi')
        self.assertEqual(data_vt.group, 'air_quality_index')

    def test_series_of_colors(self):
        self.conn.execute("INSERT INTO archive VALUES (?, ?, ?, ?)",
                          (3600, weewx.US, 5, 55.5))
        _, _, data_vt = AQI.get_series(
            'pm2_5_aqi_color', weeutil.weeutil.TimeSpan(0, 4000), self.db_manager)
        self.assertEqual(data_vt.value, [TestComputeAqiColor.RED])

    def test_mixed_unit_systems_rejected(self):
        rows = [(3600, weewx.US, 5, 9.0), (3900, weewx.METRIC, 5, 35.4)]
        self.conn.executemany("INSERT INTO archive VALUES (?, ?, ?, ?)", rows)
        with self.assertRaises(weewx.UnsupportedFeature):
            AQI.get_series('pm2_5_aqi', weeutil.weeutil.TimeSpan(0, 4000), self.db_manager)

    def test_aggregation_delegates_to_archive_table(self):
        sentinel = object()
        with mock.patch.object(weewx.xtypes.ArchiveTable, 'get_series',
                               return_value=sentinel) as m:
            result = AQI.get_series('pm2_5_aqi', weeutil.weeutil.TimeSpan(0, 4000),
                                    self.db_manager, 'avg', 3600)
        self.assertIs(result, sentinel)
        m.assert_called_once()

class TestGetAggregate(unittest.TestCase):
    """Runs the xtype's aggregation SQL against a real (SQLite) database:
    an archive table and a pm2_5 daily summary table."""

    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute(
            "CREATE TABLE archive (dateTime INTEGER PRIMARY KEY, usUnits INTEGER, "
            "`interval` INTEGER, pm2_5 REAL)")
        self.conn.execute(
            "CREATE TABLE archive_day_pm2_5 (dateTime INTEGER PRIMARY KEY, "
            "min REAL, mintime INTEGER, max REAL, maxtime INTEGER, "
            "sum REAL, count INTEGER, wsum REAL, sumtime INTEGER)")
        self.db_manager = FakeDBManager(self.conn)

    def tearDown(self):
        self.conn.close()

    def insert_archive(self, rows):
        self.conn.executemany("INSERT INTO archive VALUES (?, ?, ?, ?)",
                              [(ts, weewx.US, 5, pm) for ts, pm in rows])

    def test_unknown_type(self):
        for obs_type in ['outTemp', 'pm2_5_1m_aqi', 'pm2_5_nowcast_aqi']:
            with self.assertRaises(weewx.UnknownType):
                AQI.get_aggregate(obs_type, weeutil.weeutil.TimeSpan(1000, 5000),
                                  'avg', self.db_manager)

    def test_unknown_aggregation(self):
        for agg in ['sum', 'not_a_thing']:
            with self.assertRaises(weewx.UnknownAggregation):
                AQI.get_aggregate('pm2_5_aqi', weeutil.weeutil.TimeSpan(1000, 5000),
                                  agg, self.db_manager)

    def test_archive_table_aggregates(self):
        # A span NOT on day boundaries: every aggregate must run against
        # the archive table.  (Before v2.0 the first/last SQL was
        # syntactically invalid; this test executes every statement.)
        self.insert_archive([(2000, 9.0), (3000, 35.4), (4000, 55.4)])
        span = weeutil.weeutil.TimeSpan(1000, 5000)
        expectations = {
            'first': 50,   # pm2_5 9.0
            'last': 150,   # pm2_5 55.4
            'min': 50,
            'max': 150,
            'avg': 96,     # pm2_5 (9.0 + 35.4 + 55.4) / 3 = 33.26
        }
        for agg, expected in expectations.items():
            vt = AQI.get_aggregate('pm2_5_aqi', span, agg, self.db_manager)
            self.assertEqual(vt.value, expected, 'aggregate %s' % agg)
            self.assertEqual(vt.unit, 'aqi')

    def test_count_is_not_aqi_transformed(self):
        # Regression: count used to be run through the AQI computation.
        self.insert_archive([(2000, 9.0), (3000, 35.4), (4000, 55.4)])
        vt = AQI.get_aggregate('pm2_5_aqi', weeutil.weeutil.TimeSpan(1000, 5000),
                               'count', self.db_manager)
        self.assertEqual(vt.value, 3)

    def test_color_aggregate(self):
        self.insert_archive([(2000, 9.0), (3000, 55.4)])
        vt = AQI.get_aggregate('pm2_5_aqi_color', weeutil.weeutil.TimeSpan(1000, 5000),
                               'max', self.db_manager)
        self.assertEqual(vt.value, TestComputeAqiColor.ORANGE)

    def test_empty_span(self):
        vt = AQI.get_aggregate('pm2_5_aqi', weeutil.weeutil.TimeSpan(6000, 7000),
                               'min', self.db_manager)
        self.assertIsNone(vt.value)

    @staticmethod
    def local_midnight(year, month, day):
        return int(time.mktime(
            datetime.datetime(year, month, day).timetuple()))

    def populate_day_summaries(self, with_archive=True):
        day1 = self.local_midnight(2026, 1, 5)
        day2 = self.local_midnight(2026, 1, 6)
        day3 = self.local_midnight(2026, 1, 7)
        # day1: avg 10, min 5, max 25.  day2: avg 30, min 15, max 35.
        self.conn.execute(
            "INSERT INTO archive_day_pm2_5 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (day1, 5.0, day1 + 60, 25.0, day1 + 120, 1000.0, 100, 1000.0, 100))
        self.conn.execute(
            "INSERT INTO archive_day_pm2_5 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (day2, 15.0, day2 + 60, 35.0, day2 + 120, 3000.0, 100, 3000.0, 100))
        if with_archive:
            # The day-boundary path reads usUnits from the archive table.
            self.insert_archive([(day1 + 300, 10.0)])
        return day1, day3

    def test_day_boundary_fast_path(self):
        day1, day3 = self.populate_day_summaries()
        span = weeutil.weeutil.TimeSpan(day1, day3)
        # Overall: avg (1000+3000)/(100+100) = 20, min 5, max 35.
        self.assertEqual(
            AQI.get_aggregate('pm2_5_aqi', span, 'avg', self.db_manager).value,
            AQI.compute_pm2_5_aqi(20.0))
        self.assertEqual(
            AQI.get_aggregate('pm2_5_aqi', span, 'min', self.db_manager).value,
            AQI.compute_pm2_5_aqi(5.0))
        self.assertEqual(
            AQI.get_aggregate('pm2_5_aqi', span, 'max', self.db_manager).value,
            AQI.compute_pm2_5_aqi(35.0))

    def test_day_boundary_with_empty_archive_table(self):
        # Day summaries but no archive rows: the usUnits lookup finds no
        # row.  The value still computes; the unit system is unknown.
        day1, day3 = self.populate_day_summaries(with_archive=False)
        vt = AQI.get_aggregate('pm2_5_aqi', weeutil.weeutil.TimeSpan(day1, day3),
                               'avg', self.db_manager)
        self.assertEqual(vt.value, AQI.compute_pm2_5_aqi(20.0))
        self.assertIsNone(vt.unit)

    def test_trailing_24h_window_uses_archive_table(self):
        # A span whose length is a multiple of 24 hours but which does NOT
        # start at midnight must not be routed to the daily summary table
        # (that would silently include data outside the span).
        day1, _ = self.populate_day_summaries()
        start = day1 + 3600
        stop = start + 24 * 3600
        self.insert_archive([(start + 300, 9.0), (start + 600, 35.4)])
        vt = AQI.get_aggregate('pm2_5_aqi', weeutil.weeutil.TimeSpan(start, stop),
                               'avg', self.db_manager)
        # Average of the archive rows within the span, (9.0 + 35.4) / 2 = 22.2;
        # the daily summaries (which would give 10.0) must not be consulted.
        self.assertEqual(vt.value, AQI.compute_pm2_5_aqi(22.2))

if __name__ == '__main__':
    unittest.main()
