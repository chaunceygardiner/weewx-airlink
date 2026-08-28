#
#    See the file LICENSE.txt for your full rights.
#
"""Hermetic tests for weewx-airlink.  No network access: everything from the
fetch stack down is exercised with mocks, and the xtype SQL paths run
against an in-memory SQLite database."""

import copy
import datetime
import importlib
import importlib.util
import logging
import os
import sqlite3
import threading
import time
import unittest

from typing import Any, Dict
from unittest import mock

import configobj

import weeutil.logger
import weeutil.weeutil
import weewx
import weewx.accum
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

def answer(value=None, reachable=True):
    """A ProxyAnswer for mocking the fetch layer."""
    return user.airlink.ProxyAnswer(reachable=reachable, value=value)

def make_source(name='Sensor1', enable=True, hostname='host', is_proxy=False, **kwargs):
    d = {'enable': enable, 'hostname': hostname}
    d.update(kwargs)
    return Source({name: d}, name, is_proxy)

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
        s = Source({'Sensor1': {'hostname': 'h'}}, 'Sensor1', False)
        self.assertFalse(s.is_proxy)
        self.assertEqual(s.port, 80)
        self.assertEqual(s.timeout, 10)
        # enable defaults to False, and parses strings.
        self.assertFalse(s.enable)
        s = Source({'Sensor1': {'hostname': 'h', 'enable': 'true'}}, 'Sensor1', False)
        self.assertTrue(s.enable)

    def test_proxy_defaults(self):
        # A proxy answers on 8040 and is expected to answer fast, since it
        # reads from its own database rather than from the sensor.
        s = Source({'Proxy1': {'hostname': 'h'}}, 'Proxy1', True)
        self.assertTrue(s.is_proxy)
        self.assertEqual(s.port, 8040)
        self.assertEqual(s.timeout, 1)

    def test_explicit_values_override_both_kinds(self):
        s = Source({'Proxy1': {'hostname': 'h', 'port': '9000', 'timeout': '7'}}, 'Proxy1', True)
        self.assertEqual(s.port, 9000)
        self.assertEqual(s.timeout, 7)
        s = Source({'Sensor1': {'hostname': 'h', 'port': '8080', 'timeout': '3'}}, 'Sensor1', False)
        self.assertEqual(s.port, 8080)
        self.assertEqual(s.timeout, 3)

    def test_proxies_come_before_sensors(self):
        config = {
            'Sensor1': {'enable': True, 'hostname': 's1'},
            'Proxy1' : {'enable': True, 'hostname': 'p1'},
            'Proxy2' : {'enable': True, 'hostname': 'p2'},
        }
        sources = AirLink.configure_sources(config)
        self.assertEqual([s.hostname for s in sources], ['p1', 'p2', 's1'])
        self.assertEqual([s.is_proxy for s in sources], [True, True, False])

    def test_proxy_numbering_must_be_consecutive(self):
        config = {
            'Proxy1' : {'enable': True, 'hostname': 'p1'},
            'Proxy3' : {'enable': True, 'hostname': 'p3'},
            'Sensor1': {'enable': True, 'hostname': 's1'},
        }
        sources = AirLink.configure_sources(config)
        self.assertEqual([s.hostname for s in sources], ['p1', 's1'])

    def test_sensors_only_is_unchanged(self):
        # The existing [[SensorN]]-only configuration keeps working untouched.
        config = {'Sensor1': {'enable': True, 'hostname': 's1'}}
        sources = AirLink.configure_sources(config)
        self.assertEqual([s.hostname for s in sources], ['s1'])
        self.assertEqual([s.is_proxy for s in sources], [False])

class TestConcentrationsTimestamp(unittest.TestCase):
    """is_sane permits a null last_report_time, which is what dateTime is
    built from.  get_concentrations runs on the MAIN thread at startup, where
    an escaping exception stops weewxd from starting at all."""

    def test_a_reading_with_no_timestamp_is_ignored(self):
        cfg = make_cfg(sources=[make_source('Sensor1', hostname='s1')])
        record = user.airlink.populate_record(None, valid_response())
        with mock.patch('user.airlink.collect_data', return_value=record):
            self.assertIsNone(user.airlink.get_concentrations(cfg))

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
        engine.console.archive_interval = 300
        config = {
            'AirLink': {
                'Sensor1': {'enable': True, 'hostname': 'airlink1'},
                'Sensor2': {'enable': False, 'hostname': 'airlink2'},
            },
            'StdArchive': {'archive_interval': 300},
        }
        conc = fresh_concentrations()
        n_xtypes = len(weewx.xtypes.xtypes)
        orig_accum_maps = list(weewx.accum.accum_dict.maps)
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
            # The noop accumulator extractors are registered, so the archive
            # record can't shadow the xtype.
            self.assertEqual(
                weewx.accum.accum_dict['pm2_5_aqi'], {'extractor': 'noop'})
            # The poller thread was created as a daemon and started.
            _, kwargs = thread_cls.call_args
            self.assertTrue(kwargs['daemon'])
            self.assertEqual(kwargs['name'], 'AirLink')
            thread_cls.return_value.start.assert_called_once()
            # Bound to NEW_LOOP_PACKET.
            engine.bind.assert_called_once_with(weewx.NEW_LOOP_PACKET, a.new_loop_packet)
        finally:
            # Unregister anything this test added to the global xtypes list
            # and the global accumulator config.
            del weewx.xtypes.xtypes[n_xtypes:]
            weewx.accum.accum_dict.maps[:] = orig_accum_maps

    def test_startup_without_sources_is_inoperable(self):
        engine = mock.Mock()
        engine.console.archive_interval = 300
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

class TestAccumulatorExtractors(unittest.TestCase):
    """The accumulator must not fold the loop-injected AQI fields into
    archive records: WeeWX's default avg extractor would average the
    already-rounded AQI integers (a meaningless quantity), and during
    real-time report generation $current uses the archive record directly,
    shadowing the AQI xtype.  extractor = noop drops the fields so lookups
    fall through to the xtype."""

    def setUp(self):
        self.orig_accum_maps = list(weewx.accum.accum_dict.maps)

    def tearDown(self):
        weewx.accum.accum_dict.maps[:] = self.orig_accum_maps

    def test_noop_extractor_registered_for_full_aqi_family(self):
        AQI.register_accumulator_extractors()
        for obs_type in AQI.aqi_source_field:
            self.assertEqual(
                weewx.accum.accum_dict[obs_type]['extractor'], 'noop')

    def test_aqi_fields_dropped_from_extracted_record(self):
        AQI.register_accumulator_extractors()
        accum = weewx.accum.Accum(
            weeutil.weeutil.TimeSpan(1700000000, 1700000300))
        # Loop packets whose AQI toggles between 0 and 1: the default avg
        # extractor would put a bogus fractional AQI in the record.
        for i, (pm, aqi) in enumerate([(0.05, 0), (0.155, 1), (0.155, 1)]):
            accum.addRecord({
                'dateTime': 1700000100 + 5 * i,
                'usUnits': weewx.US,
                'pm2_5': pm,
                'pm2_5_aqi': aqi,
                'pm2_5_aqi_color': 128 * i,
            })
        record = accum.getRecord()
        # The concentration is extracted (averaged) as before...
        self.assertAlmostEqual(record['pm2_5'], (0.05 + 0.155 + 0.155) / 3)
        # ...but the AQI fields are dropped, leaving $current to the xtype.
        self.assertNotIn('pm2_5_aqi', record)
        self.assertNotIn('pm2_5_aqi_color', record)

    def test_user_accumulator_config_takes_precedence(self):
        AQI.register_accumulator_extractors()
        # weewx.accum.initialize() loads the user's [Accumulator] section in
        # front of everything else; a user override must win over ours.
        weewx.accum.initialize(
            {'Accumulator': {'pm2_5_aqi': {'extractor': 'avg'}}})
        self.assertEqual(
            weewx.accum.accum_dict['pm2_5_aqi']['extractor'], 'avg')
        # Types the user didn't override still get ours.
        self.assertEqual(
            weewx.accum.accum_dict['pm2_5_1m_aqi']['extractor'], 'noop')

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

def archive_reading(pm_1_last=4, pm_2p5_last=9, pm_10_last=11, ts=None):
    """One airlink-proxy archive record: the AirLink's own shape, holding
    averages rather than a spot reading."""
    j = valid_response(ts)
    c = conditions(j)
    c['pm_1_last']   = pm_1_last
    c['pm_2p5_last'] = pm_2p5_last
    c['pm_10_last']  = pm_10_last
    return j

class TestAskProxy(unittest.TestCase):
    """The reachable/usable distinction: a proxy that answered is up, however
    little use its answer was.  Only silence takes it out of service."""

    def ask(self, response=None, side_effect=None, command='/get-version'):
        with mock.patch('user.airlink.requests.get',
                        return_value=response, side_effect=side_effect) as get:
            return user.airlink.ask_proxy(
                make_source('Proxy1', is_proxy=True), command), get

    def test_returns_parsed_json(self):
        a, get = self.ask(FakeResponse({'version': '2'}))
        self.assertEqual(a.value, {'version': '2'})
        self.assertTrue(a.reachable)
        self.assertEqual(get.call_args[1]['url'], 'http://host:8040/get-version')

    def test_an_error_envelope_is_no_value_but_still_reachable(self):
        # airlink-proxy answers 200 with this when its database is locked by
        # its own writer -- likeliest during a catchup burst.  Reading it as
        # "down" is what abandons the rest of the outage.
        body = {'data': None, 'error': {'code': 400, 'message': 'Unknown command: /bogus.'}}
        a, _ = self.ask(FakeResponse(body), command='/bogus')
        self.assertIsNone(a.value)
        self.assertTrue(a.reachable)

    def test_an_unparseable_body_is_still_reachable(self):
        a, _ = self.ask(FakeResponse(None, status_error=Exception('500')))
        self.assertIsNone(a.value)
        self.assertTrue(a.reachable)

    def test_null_error_member_is_a_result(self):
        a, _ = self.ask(FakeResponse(valid_response()),
                        command='/fetch-two-minute-record')
        self.assertIsNotNone(a.value)

    def test_silence_is_unreachable(self):
        a, _ = self.ask(side_effect=Exception('boom'))
        self.assertIsNone(a.value)
        self.assertFalse(a.reachable)

    def test_terminate_is_not_swallowed(self):
        with self.assertRaises(Terminate):
            self.ask(side_effect=Terminate('bye'))

class TestFetchProxyArchiveRecords(unittest.TestCase):

    def fetch(self, payload, since_ts=1000, max_ts=1300):
        with mock.patch('user.airlink.requests.get',
                        return_value=FakeResponse(payload)) as get:
            a = user.airlink.fetch_proxy_archive_records(
                make_source('Proxy1', is_proxy=True), since_ts, max_ts)
        return a.value, get

    def test_query_args_are_comma_separated(self):
        # The proxy splits on ',' -- an '&' would be parsed into since_ts.
        _, get = self.fetch([])
        self.assertEqual(
            get.call_args[1]['url'],
            'http://host:8040/fetch-archive-records?since_ts=1000,max_ts=1300')

    def test_records_are_parsed(self):
        records, _ = self.fetch([archive_reading(), archive_reading(pm_2p5_last=15)])
        self.assertEqual([r['pm2_5'] for r in records], [9, 15])

    def test_insane_records_are_skipped_not_fatal(self):
        bad = archive_reading()
        conditions(bad)['pm_2p5_last'] = 'nine'
        records, _ = self.fetch([bad, archive_reading()])
        self.assertEqual([r['pm2_5'] for r in records], [9])

    def test_type_5_records_are_converted(self):
        records, _ = self.fetch([type_5_response()])
        self.assertEqual(len(records), 1)
        self.assertIsNotNone(records[0]['pm_10_nowcast'])

    def test_not_a_list_returns_none(self):
        records, _ = self.fetch({'version': '2'})
        self.assertIsNone(records)

    def test_a_garbage_element_is_skipped_not_fatal(self):
        # Returning None here would be read by backfill_values as "proxy
        # unreachable", suppressing a proxy that answered.
        records, _ = self.fetch(['not a reading', archive_reading()])
        self.assertEqual([r['pm2_5'] for r in records], [9])

    def test_a_batch_of_only_garbage_is_empty_not_none(self):
        records, _ = self.fetch(['not a reading'])
        self.assertEqual(records, [])

    def test_unreachable_is_reported_as_such(self):
        with mock.patch('user.airlink.requests.get', side_effect=Exception('boom')):
            a = user.airlink.fetch_proxy_archive_records(
                make_source('Proxy1', is_proxy=True), 1000, 1300)
        self.assertIsNone(a.value)
        self.assertFalse(a.reachable)

    def test_a_useless_answer_is_not_reported_as_unreachable(self):
        with mock.patch('user.airlink.requests.get',
                        return_value=FakeResponse({'version': '2'})):
            a = user.airlink.fetch_proxy_archive_records(
                make_source('Proxy1', is_proxy=True), 1000, 1300)
        self.assertIsNone(a.value)
        self.assertTrue(a.reachable)

    def test_a_batch_that_cannot_be_walked_at_all_returns_none(self):
        # The per-reading handler catches a bad row; this is the backstop
        # around everything else, on a path where an escaping exception would
        # take weewxd down.
        class Unwalkable(list):
            def __iter__(self):
                raise ValueError('boom')
        records, _ = self.fetch(Unwalkable())
        self.assertIsNone(records)

    def test_empty_list_is_not_none(self):
        records, _ = self.fetch([])
        self.assertEqual(records, [])

class TestFetchProxyTwoMinuteRecord(unittest.TestCase):

    def fetch(self, payload):
        with mock.patch('user.airlink.requests.get', return_value=FakeResponse(payload)):
            return user.airlink.fetch_proxy_two_minute_record(
                make_source('Proxy1', is_proxy=True)).value

    def test_reading_is_parsed(self):
        self.assertEqual(self.fetch(archive_reading())['pm2_5'], 9)

    def test_no_reading_yet_is_an_error_envelope(self):
        # The proxy answers 'No two-minute average available.', not {}.
        self.assertIsNone(self.fetch(
            {'data': None, 'error': {'code': 400,
                                     'message': 'No two-minute average available.'}}))

    def test_not_an_object_returns_none(self):
        self.assertIsNone(self.fetch([]))

    def test_garbage_returns_none(self):
        self.assertIsNone(self.fetch({'data': 'nonsense'}))

    def test_null_data_returns_none(self):
        self.assertIsNone(self.fetch({'data': None, 'error': None}))

    def test_unreachable_returns_none(self):
        with mock.patch('user.airlink.requests.get', side_effect=Exception('boom')):
            a = user.airlink.fetch_proxy_two_minute_record(
                make_source('Proxy1', is_proxy=True))
        self.assertIsNone(a.value)
        self.assertFalse(a.reachable)

class TestAveragePmValues(unittest.TestCase):

    def test_averages_each_observation(self):
        values = user.airlink.average_pm_values(
            [{'pm1_0': 2, 'pm2_5': 10, 'pm10_0': 20},
             {'pm1_0': 4, 'pm2_5': 20, 'pm10_0': 30}])
        self.assertEqual(values, {'pm1_0': 3.0, 'pm2_5': 15.0, 'pm10_0': 25.0})

    def test_a_field_is_averaged_over_the_records_that_reported_it(self):
        values = user.airlink.average_pm_values(
            [{'pm1_0': 2, 'pm2_5': 10, 'pm10_0': None},
             {'pm1_0': 4, 'pm2_5': None, 'pm10_0': None}])
        self.assertEqual(values, {'pm1_0': 3.0, 'pm2_5': 10.0})

    def test_no_records_is_no_values(self):
        self.assertEqual(user.airlink.average_pm_values([]), {})

class TestProxySupportsCatchup(unittest.TestCase):

    def ask(self, answers, archive_interval=300):
        """answers: a dict of command suffix -> payload (or an Exception)."""
        def fake_get(url, timeout):
            for suffix, payload in answers.items():
                if url.endswith(suffix):
                    if isinstance(payload, Exception):
                        raise payload
                    return FakeResponse(payload)
            raise AssertionError('unexpected url: %s' % url)
        with mock.patch('user.airlink.requests.get',
                        side_effect=lambda url, timeout: fake_get(url, timeout)):
            return user.airlink.proxy_supports_catchup(
                make_source('Proxy1', is_proxy=True), archive_interval).value

    def test_matching_version_and_interval(self):
        self.assertTrue(self.ask({'/get-version': {'version': '2'},
                                  '/get-archive-interval-secs': {'archive-interval-secs': 300}}))

    def test_old_api_version_is_refused(self):
        # Version 1 files the single boundary poll, not an average.
        self.assertFalse(self.ask({'/get-version': {'version': '1'}}))

    def test_a_later_api_version_is_accepted(self):
        # A command set only grows.  Refusing a later one would silently
        # disable catch-up until every user restarted weewxd, since the
        # refusal is remembered for the life of the process.
        self.assertTrue(self.ask({'/get-version': {'version': '3'},
                                  '/get-archive-interval-secs': {'archive-interval-secs': 300}}))

    def test_a_version_that_is_not_a_number_is_refused(self):
        self.assertFalse(self.ask({'/get-version': {'version': 'two'}}))

    def test_a_dotted_version_is_not_mistaken_for_an_old_one(self):
        # int('2.1') raises, which would land in the refused branch and claim
        # the proxy speaks something older than 2 -- remembered for the life
        # of the process.
        for version in ('2.1', '3.0'):
            self.assertTrue(
                self.ask({'/get-version': {'version': version},
                          '/get-archive-interval-secs': {'archive-interval-secs': 300}}),
                version)
        self.assertFalse(self.ask({'/get-version': {'version': '1.9'}}))

    def test_missing_version_is_refused(self):
        self.assertFalse(self.ask({'/get-version': {}}))

    def test_a_shorter_interval_that_divides_evenly_is_accepted(self):
        # Five of the proxy's 60 second records cover one 300 second WeeWX
        # period, and are averaged together.
        self.assertTrue(self.ask({'/get-version': {'version': '2'},
                                  '/get-archive-interval-secs': {'archive-interval-secs': 60}}))

    def test_an_interval_that_does_not_divide_is_refused(self):
        self.assertFalse(self.ask({'/get-version': {'version': '2'},
                                   '/get-archive-interval-secs': {'archive-interval-secs': 400}}))

    def test_a_longer_interval_is_refused(self):
        # A WeeWX period can fall entirely inside one of the proxy's records.
        self.assertFalse(self.ask({'/get-version': {'version': '2'},
                                   '/get-archive-interval-secs': {'archive-interval-secs': 600}}))

    def test_a_zero_interval_is_refused(self):
        self.assertFalse(self.ask({'/get-version': {'version': '2'},
                                   '/get-archive-interval-secs': {'archive-interval-secs': 0}}))

    def test_missing_interval_is_refused(self):
        self.assertFalse(self.ask({'/get-version': {'version': '2'},
                                   '/get-archive-interval-secs': {}}))

    def test_interval_as_a_string_still_matches(self):
        self.assertTrue(self.ask({'/get-version': {'version': '2'},
                                  '/get-archive-interval-secs': {'archive-interval-secs': '300'}}))

    def test_unreachable_leaves_the_question_open(self):
        self.assertIsNone(self.ask({'/get-version': Exception('down')}))

    def test_unreachable_on_the_interval_leaves_it_open(self):
        self.assertIsNone(self.ask({'/get-version': {'version': '2'},
                                    '/get-archive-interval-secs': Exception('down')}))

class TestRecordInjections(unittest.TestCase):

    def test_only_what_was_inserted_is_tallied(self):
        cfg = make_cfg()
        AirLink.record_injections(cfg, {'dateTime': 1000, 'pm2_5': 9})
        self.assertEqual(cfg.injections['pm2_5'], [1000.0])
        self.assertEqual(cfg.injections['pm1_0'], [])

    def test_retention_follows_the_interval_catchup_measures_with(self):
        # Under hardware record generation the console's interval can exceed
        # weewx.conf's.  Pruning to the shorter one would make a period WeeWX
        # watched look unwatched, and its real average would be overwritten.
        cfg = make_cfg(archive_interval=300)
        cfg.injection_retention_secs = 2 * 1800
        AirLink.record_injections(cfg, {'dateTime': 1000, 'pm2_5': 9})
        AirLink.record_injections(cfg, {'dateTime': 1000 + 601, 'pm2_5': 9})
        # Still inside 3600s of retention, where 2 * 300 would have dropped it.
        self.assertEqual(cfg.injections['pm2_5'], [1000.0, 1601.0])

    def test_retention_falls_back_to_the_archive_interval(self):
        # What the __main__ harness gets: nothing sets the field.
        cfg = make_cfg(archive_interval=300)
        self.assertIsNone(cfg.injection_retention_secs)
        AirLink.record_injections(cfg, {'dateTime': 1000, 'pm2_5': 9})
        AirLink.record_injections(cfg, {'dateTime': 1000 + 601, 'pm2_5': 9})
        self.assertEqual(cfg.injections['pm2_5'], [1601.0])

    def test_old_entries_are_pruned(self):
        cfg = make_cfg(archive_interval=300)
        AirLink.record_injections(cfg, {'dateTime': 1000, 'pm2_5': 9})
        # Two archive intervals later, the first entry is gone.
        AirLink.record_injections(cfg, {'dateTime': 1000 + 601, 'pm2_5': 9})
        self.assertEqual(cfg.injections['pm2_5'], [1601.0])

class AirLinkServiceTestCase(unittest.TestCase):
    """Builds real AirLink services, restoring the global xtype and
    accumulator registries afterwards."""

    def setUp(self):
        self._n_xtypes = len(weewx.xtypes.xtypes)
        self._accum_maps = list(weewx.accum.accum_dict.maps)

    def tearDown(self):
        del weewx.xtypes.xtypes[self._n_xtypes:]
        weewx.accum.accum_dict.maps[:] = self._accum_maps

    def make_airlink(self, proxy=True, archive_interval=300,
                     record_generation='software', concentrations=None,
                     console_interval=300):
        engine = mock.Mock()
        engine.console.archive_interval = console_interval
        airlink_dict = {}
        if proxy:
            airlink_dict['Proxy1'] = {'enable': True, 'hostname': 'p1'}
        airlink_dict['Sensor1'] = {'enable': True, 'hostname': 's1'}
        config = {
            'AirLink': airlink_dict,
            'StdArchive': {'archive_interval': archive_interval,
                           'record_generation': record_generation},
        }
        with mock.patch('user.airlink.get_concentrations', return_value=concentrations), \
             mock.patch('user.airlink.threading.Thread'):
            return AirLink(engine, config)

class TestArchiveRecordBinding(AirLinkServiceTestCase):

    def test_bound_when_a_proxy_is_configured(self):
        a = self.make_airlink(proxy=True)
        self.assertIn(mock.call(weewx.NEW_ARCHIVE_RECORD, a.new_archive_record),
                      a.engine.bind.call_args_list)

    def test_not_bound_without_a_proxy(self):
        # An AirLink queried directly keeps no history, so there is nothing to
        # ask and the handler is never bound.
        a = self.make_airlink(proxy=False)
        self.assertEqual([c[0][0] for c in a.engine.bind.call_args_list],
                         [weewx.NEW_LOOP_PACKET])

    def test_software_generation_uses_the_configured_interval(self):
        a = self.make_airlink(archive_interval=300, record_generation='software',
                              console_interval=60)
        self.assertEqual(a.archive_interval, 300)

    def test_hardware_generation_uses_the_console_interval(self):
        a = self.make_airlink(archive_interval=300, record_generation='hardware',
                              console_interval=60)
        self.assertEqual(a.archive_interval, 60)

    def test_a_console_that_answers_none_falls_back(self):
        a = self.make_airlink(archive_interval=300, record_generation='hardware',
                              console_interval=None)
        self.assertEqual(a.archive_interval, 300)

    def test_a_console_that_cannot_answer_falls_back(self):
        engine = mock.Mock()
        type(engine.console).archive_interval = mock.PropertyMock(
            side_effect=NotImplementedError)
        config = {
            'AirLink': {'Proxy1': {'enable': True, 'hostname': 'p1'}},
            'StdArchive': {'archive_interval': 300, 'record_generation': 'hardware'},
        }
        with mock.patch('user.airlink.get_concentrations', return_value=None), \
             mock.patch('user.airlink.threading.Thread'):
            a = AirLink(engine, config)
        self.assertEqual(a.archive_interval, 300)

    def test_injection_retention_follows_the_catchup_interval(self):
        # The tally must reach back at least as far as the period
        # new_archive_record measures, which under hardware generation is the
        # console's interval, not weewx.conf's.
        a = self.make_airlink(archive_interval=300, record_generation='hardware',
                              console_interval=1800)
        self.assertEqual(a.archive_interval, 1800)
        self.assertEqual(a.cfg.injection_retention_secs, 3600)

    def test_freshness_interval_is_left_alone(self):
        # cfg.archive_interval decides how long a reading stays fresh and must
        # keep answering from weewx.conf exactly as it always has.
        a = self.make_airlink(archive_interval=300, record_generation='hardware',
                              console_interval=60)
        self.assertEqual(a.cfg.archive_interval, 300)

class TestNewArchiveRecord(AirLinkServiceTestCase):

    @staticmethod
    def event(record):
        return type('Event', (), {'record': record})()

    def test_fills_a_period_the_extension_missed(self):
        a = self.make_airlink()
        record = {'dateTime': 2000, 'interval': 5}
        with mock.patch.object(a, 'backfill_values',
                               return_value={'pm1_0': 1.0, 'pm2_5': 9.0, 'pm10_0': 11.0}) as bf:
            a.new_archive_record(self.event(record))
        self.assertEqual(record['pm2_5'], 9.0)
        # The span is the record's own interval, in seconds.
        bf.assert_called_once_with(1700, 2000)

    def test_a_fractional_interval_is_not_truncated(self):
        # Under software record generation WeeWX sets interval by true
        # division, so a 90 second archive interval arrives as 1.5.
        # Truncating it would ask the proxy for a 60 second window and put
        # start_ts 30 seconds inside the period.
        a = self.make_airlink(archive_interval=90)
        with mock.patch.object(a, 'backfill_values', return_value={}) as bf:
            a.new_archive_record(self.event({'dateTime': 2000, 'interval': 1.5}))
        bf.assert_called_once_with(1910, 2000)

    def test_a_record_with_no_timestamp_is_left_alone(self):
        # Arithmetic on a null dateTime would raise ahead of the guarded
        # call, on the main thread.  A record that cannot say which period it
        # covers cannot be filled in either.
        a = self.make_airlink()
        record = {'dateTime': None, 'interval': 5}
        with mock.patch.object(a, 'backfill_values') as bf:
            a.new_archive_record(self.event(record))
        bf.assert_not_called()
        self.assertNotIn('pm2_5', record)

    def test_a_null_interval_does_not_take_weewxd_down(self):
        # A record read back with a NULL interval used to raise TypeError
        # outside the guarded call.
        a = self.make_airlink(archive_interval=300)
        with mock.patch.object(a, 'backfill_values', return_value={}) as bf:
            a.new_archive_record(self.event({'dateTime': 2000, 'interval': None}))
        bf.assert_called_once_with(1700, 2000)

    def test_the_configured_interval_is_used_when_the_record_has_none(self):
        a = self.make_airlink(archive_interval=300)
        with mock.patch.object(a, 'backfill_values', return_value={}) as bf:
            a.new_archive_record(self.event({'dateTime': 2000}))
        bf.assert_called_once_with(1700, 2000)

    def test_a_record_that_already_has_values_is_left_alone(self):
        a = self.make_airlink()
        record = {'dateTime': 2000, 'interval': 5,
                  'pm1_0': 1.0, 'pm2_5': 9.0, 'pm10_0': 11.0}
        with mock.patch.object(a, 'backfill_values') as bf:
            a.new_archive_record(self.event(record))
        bf.assert_not_called()

    def test_a_period_the_extension_injected_into_is_left_alone(self):
        a = self.make_airlink()
        # A loop packet inside the period got pm data, so the accumulator has
        # the period's samples even though the record does not show them yet.
        with a.cfg.lock:
            AirLink.record_injections(a.cfg, {'dateTime': 1900, 'pm1_0': 1,
                                              'pm2_5': 9, 'pm10_0': 11})
        with mock.patch.object(a, 'backfill_values') as bf:
            a.new_archive_record(self.event({'dateTime': 2000, 'interval': 5}))
        bf.assert_not_called()

    def test_an_injection_outside_the_period_does_not_count(self):
        a = self.make_airlink()
        with a.cfg.lock:
            AirLink.record_injections(a.cfg, {'dateTime': 1700, 'pm2_5': 9})
        # start_ts is exclusive: 1700 is the previous period's.
        with mock.patch.object(a, 'backfill_values', return_value={}) as bf:
            a.new_archive_record(self.event({'dateTime': 2000, 'interval': 5}))
        bf.assert_called_once()

    def test_a_none_value_is_treated_as_missing(self):
        # Under software record generation the accumulator writes None for a
        # type it holds with no usable values.
        a = self.make_airlink()
        record = {'dateTime': 2000, 'interval': 5, 'pm2_5': None}
        with mock.patch.object(a, 'backfill_values', return_value={'pm2_5': 9.0}):
            a.new_archive_record(self.event(record))
        self.assertEqual(record['pm2_5'], 9.0)

    def test_the_nothing_to_fill_message_is_logged_once(self):
        # A catchup burst dispatches every record of an outage back to back.
        a = self.make_airlink()
        with mock.patch.object(a, 'backfill_values', return_value={}):
            with self.assertLogs('user.airlink', level='INFO') as first:
                a.new_archive_record(self.event({'dateTime': 2000, 'interval': 5}))
            self.assertTrue(any('No airlink-proxy data' in m for m in first.output))
            self.assertTrue(a.no_data_logged)
            # The next record in the burst says it at debug, not info.
            with self.assertLogs('user.airlink', level='DEBUG') as again:
                a.new_archive_record(self.event({'dateTime': 2300, 'interval': 5}))
            self.assertFalse(any(m.startswith('INFO') and 'No airlink-proxy data' in m
                                 for m in again.output))

    def test_a_successful_fill_rearms_the_message(self):
        a = self.make_airlink()
        with mock.patch.object(a, 'backfill_values', return_value={}):
            a.new_archive_record(self.event({'dateTime': 2000, 'interval': 5}))
        self.assertTrue(a.no_data_logged)
        with mock.patch.object(a, 'backfill_values', return_value={'pm2_5': 9.0}):
            a.new_archive_record(self.event({'dateTime': 2300, 'interval': 5}))
        self.assertFalse(a.no_data_logged)

    def test_nothing_to_fill_with_leaves_the_record_untouched(self):
        a = self.make_airlink()
        record = {'dateTime': 2000, 'interval': 5}
        with mock.patch.object(a, 'backfill_values', return_value={}):
            a.new_archive_record(self.event(record))
        self.assertNotIn('pm2_5', record)

    def test_an_exception_does_not_reach_weewx(self):
        a = self.make_airlink()
        record = {'dateTime': 2000, 'interval': 5}
        with mock.patch.object(a, 'backfill_values', side_effect=Exception('boom')):
            a.new_archive_record(self.event(record))
        self.assertNotIn('pm2_5', record)

    def test_terminate_is_not_swallowed(self):
        a = self.make_airlink()
        with mock.patch.object(a, 'backfill_values', side_effect=Terminate('bye')):
            with self.assertRaises(Terminate):
                a.new_archive_record(self.event({'dateTime': 2000, 'interval': 5}))

class TestBackfillValues(AirLinkServiceTestCase):

    def test_archive_records_are_averaged(self):
        a = self.make_airlink()
        with mock.patch.object(a, 'usable_proxies', return_value=a.cfg.sources[:1]), \
             mock.patch('user.airlink.fetch_proxy_archive_records',
                        return_value=answer([{'pm2_5': 10}, {'pm2_5': 20}])):
            values = a.backfill_values(1700, 2000)
        self.assertEqual(values, {'pm2_5': 15.0})

    def test_an_unreachable_proxy_is_not_asked_again_this_interval(self):
        a = self.make_airlink()
        with mock.patch.object(a, 'usable_proxies', return_value=a.cfg.sources[:1]), \
             mock.patch('user.airlink.fetch_proxy_archive_records',
                        return_value=answer(reachable=False)):
            a.backfill_values(1700, int(time.time()) - 3600)
        self.assertTrue(a.proxy_retry_after['p1:8040'] > time.time())

    def test_a_proxy_that_answered_badly_is_not_taken_out_of_service(self):
        # airlink-proxy answers 200 with an error envelope when its own
        # writer holds the database lock -- likeliest during a catchup burst,
        # when we fire a query per record.  Suppressing on that would skip
        # every remaining period of the outage, permanently, from a proxy
        # that is up and holds the data.
        a = self.make_airlink()
        with mock.patch.object(a, 'usable_proxies', return_value=a.cfg.sources[:1]), \
             mock.patch('user.airlink.fetch_proxy_archive_records',
                        return_value=answer(reachable=True)), \
             mock.patch('user.airlink.fetch_proxy_two_minute_record',
                        return_value=answer()):
            a.backfill_values(1700, int(time.time()) - 3600)
        self.assertNotIn('p1:8040', a.proxy_retry_after)

    def test_a_proxy_that_answered_badly_is_still_asked_for_two_minutes(self):
        a = self.make_airlink()
        end_ts = int(time.time()) - 10
        with mock.patch.object(a, 'usable_proxies', return_value=a.cfg.sources[:1]), \
             mock.patch('user.airlink.fetch_proxy_archive_records',
                        return_value=answer(reachable=True)), \
             mock.patch('user.airlink.fetch_proxy_two_minute_record',
                        return_value=answer({'dateTime': time.time() - 5,
                                             'pm2_5': 12})) as two_min:
            values = a.backfill_values(end_ts - 300, end_ts)
        two_min.assert_called_once()
        self.assertEqual(values, {'pm2_5': 12.0})

    def test_an_old_period_with_no_records_stays_empty(self):
        # Nothing in hand describes a period that closed hours ago.  An empty
        # column is the right answer.
        a = self.make_airlink(concentrations=fresh_concentrations())
        with mock.patch.object(a, 'usable_proxies', return_value=a.cfg.sources[:1]), \
             mock.patch('user.airlink.fetch_proxy_archive_records', return_value=answer([])):
            values = a.backfill_values(1700, int(time.time()) - 3600)
        self.assertEqual(values, {})

    def test_the_two_minute_average_fills_the_period_just_closed(self):
        a = self.make_airlink()
        end_ts = int(time.time()) - 10
        with mock.patch.object(a, 'usable_proxies', return_value=a.cfg.sources[:1]), \
             mock.patch('user.airlink.fetch_proxy_archive_records', return_value=answer([])), \
             mock.patch('user.airlink.fetch_proxy_two_minute_record',
                        return_value=answer({'dateTime': time.time() - 5, 'pm2_5': 12})):
            values = a.backfill_values(end_ts - 300, end_ts)
        self.assertEqual(values, {'pm2_5': 12.0})

    def test_a_two_minute_average_whose_window_misses_the_period_is_refused(self):
        # Its window is [reading_ts - 120, reading_ts]; dated before the
        # period's start, it cannot reach into the period.
        a = self.make_airlink()
        end_ts = int(time.time()) - 10
        start_ts = end_ts - 300
        with mock.patch.object(a, 'usable_proxies', return_value=a.cfg.sources[:1]), \
             mock.patch('user.airlink.fetch_proxy_archive_records', return_value=answer([])), \
             mock.patch('user.airlink.fetch_proxy_two_minute_record',
                        return_value=answer({'dateTime': start_ts, 'pm2_5': 12})):
            values = a.backfill_values(start_ts, end_ts)
        self.assertEqual(values, {})

    def test_a_stale_two_minute_average_is_refused(self):
        # A proxy keeps serving its last two minute average after the AirLink
        # dies -- the row is replaced, never cleared.  Using it would freeze
        # that value into every archive record from then on.
        a = self.make_airlink()
        end_ts = int(time.time()) - 10
        with mock.patch.object(a, 'usable_proxies', return_value=a.cfg.sources[:1]), \
             mock.patch('user.airlink.fetch_proxy_archive_records', return_value=answer([])), \
             mock.patch('user.airlink.fetch_proxy_two_minute_record',
                        return_value=answer({'dateTime': time.time() - 3600, 'pm2_5': 12})):
            values = a.backfill_values(end_ts - 300, end_ts)
        self.assertEqual(values, {})

    def test_a_two_minute_average_of_unknown_age_is_refused(self):
        # last_report_time is allowed to be null, so dateTime can be None.
        a = self.make_airlink()
        end_ts = int(time.time()) - 10
        with mock.patch.object(a, 'usable_proxies', return_value=a.cfg.sources[:1]), \
             mock.patch('user.airlink.fetch_proxy_archive_records', return_value=answer([])), \
             mock.patch('user.airlink.fetch_proxy_two_minute_record',
                        return_value=answer({'dateTime': None, 'pm2_5': 12})):
            values = a.backfill_values(end_ts - 300, end_ts)
        self.assertEqual(values, {})

    def test_archive_records_with_no_usable_values_fall_through(self):
        # Sane records, but the pm sensor reported nothing: keep looking
        # rather than treating {} as the answer.
        a = self.make_airlink()
        end_ts = int(time.time()) - 10
        with mock.patch.object(a, 'usable_proxies', return_value=a.cfg.sources[:1]), \
             mock.patch('user.airlink.fetch_proxy_archive_records',
                        return_value=answer([{'pm1_0': None, 'pm2_5': None, 'pm10_0': None}])), \
             mock.patch('user.airlink.fetch_proxy_two_minute_record',
                        return_value=answer({'dateTime': time.time() - 5, 'pm2_5': 12})):
            values = a.backfill_values(end_ts - 300, end_ts)
        self.assertEqual(values, {'pm2_5': 12.0})

    def test_an_unreachable_proxy_is_not_asked_for_its_two_minute_average(self):
        # It has already cost one timeout on the main thread.
        a = self.make_airlink()
        end_ts = int(time.time()) - 10
        with mock.patch.object(a, 'usable_proxies', return_value=a.cfg.sources[:1]), \
             mock.patch('user.airlink.fetch_proxy_archive_records',
                        return_value=answer(reachable=False)), \
             mock.patch('user.airlink.fetch_proxy_two_minute_record') as two_min:
            a.backfill_values(end_ts - 300, end_ts)
        two_min.assert_not_called()

    def in_hand(self, timestamp):
        """backfill_values for a period ending 10s ago, with the reading in
        hand taken at `timestamp` and both proxy paths empty."""
        conc = fresh_concentrations(timestamp=timestamp)
        a = self.make_airlink(concentrations=conc)
        with a.cfg.lock:
            a.cfg.concentrations = conc
        end_ts = int(time.time()) - 10
        with mock.patch.object(a, 'usable_proxies', return_value=a.cfg.sources[:1]), \
             mock.patch('user.airlink.fetch_proxy_archive_records', return_value=answer([])), \
             mock.patch('user.airlink.fetch_proxy_two_minute_record', return_value=answer()):
            return a.backfill_values(end_ts - 300, end_ts), conc, end_ts

    def test_a_reading_taken_inside_the_period_is_the_last_resort(self):
        # A spot reading has no duration, so it overlaps the period only by
        # being taken inside it.
        values, conc, _ = self.in_hand(int(time.time()) - 100)
        self.assertEqual(values['pm2_5'], conc.pm_2p5_last)

    def test_an_old_period_can_still_use_a_reading_taken_inside_it(self):
        # The two minute window governs the proxy's two minute average, not
        # the reading in hand -- whose rule is simply that it was taken
        # inside the period.  A period that closed an hour ago can still hold
        # a qualifying sample.
        conc = fresh_concentrations(timestamp=time.time() - 3700)
        a = self.make_airlink(concentrations=conc)
        with a.cfg.lock:
            a.cfg.concentrations = conc
        end_ts = int(time.time()) - 3600
        with mock.patch.object(a, 'usable_proxies', return_value=a.cfg.sources[:1]), \
             mock.patch('user.airlink.fetch_proxy_archive_records', return_value=answer([])), \
             mock.patch('user.airlink.fetch_proxy_two_minute_record') as two_min:
            values = a.backfill_values(end_ts - 300, end_ts)
        # ...and the two minute average is not even asked for, since its own
        # window cannot reach that far back.
        two_min.assert_not_called()
        self.assertEqual(values['pm2_5'], conc.pm_2p5_last)

    def test_a_reading_taken_after_the_period_is_refused(self):
        # The realistic case at startup: the first fetch runs after the last
        # catch-up period has already closed.  However recent it is, it
        # describes a moment the period does not contain.
        values, _, _ = self.in_hand(time.time())
        self.assertEqual(values, {})

    def test_a_reading_taken_before_the_period_is_refused(self):
        # Older than the period's start: it predates the window entirely.
        values, _, _ = self.in_hand(int(time.time()) - 410)
        self.assertEqual(values, {})

    def test_a_stale_reading_in_hand_is_not_used(self):
        conc = fresh_concentrations(timestamp=time.time() - 3600)
        a = self.make_airlink(concentrations=conc)
        with a.cfg.lock:
            a.cfg.concentrations = conc
        end_ts = int(time.time()) - 10
        with mock.patch.object(a, 'usable_proxies', return_value=a.cfg.sources[:1]), \
             mock.patch('user.airlink.fetch_proxy_archive_records', return_value=answer([])), \
             mock.patch('user.airlink.fetch_proxy_two_minute_record', return_value=answer()):
            values = a.backfill_values(end_ts - 300, end_ts)
        self.assertEqual(values, {})

    def test_a_two_minute_record_with_no_pm_values_is_passed_over(self):
        a = self.make_airlink()
        end_ts = int(time.time()) - 10
        with mock.patch.object(a, 'usable_proxies', return_value=a.cfg.sources[:1]), \
             mock.patch('user.airlink.fetch_proxy_archive_records', return_value=answer([])), \
             mock.patch('user.airlink.fetch_proxy_two_minute_record',
                        return_value=answer({'dateTime': time.time() - 5, 'pm1_0': None,
                                             'pm2_5': None, 'pm10_0': None})):
            values = a.backfill_values(end_ts - 300, end_ts)
        self.assertEqual(values, {})

    def test_no_reading_at_all_is_no_values(self):
        a = self.make_airlink()
        end_ts = int(time.time()) - 10
        with mock.patch.object(a, 'usable_proxies', return_value=a.cfg.sources[:1]), \
             mock.patch('user.airlink.fetch_proxy_archive_records', return_value=answer([])), \
             mock.patch('user.airlink.fetch_proxy_two_minute_record', return_value=answer()):
            values = a.backfill_values(end_ts - 300, end_ts)
        self.assertEqual(values, {})

class TestUsableProxies(AirLinkServiceTestCase):

    def test_the_answer_is_asked_once_and_remembered(self):
        a = self.make_airlink()
        with mock.patch('user.airlink.proxy_supports_catchup', return_value=answer(True)) as ask:
            self.assertEqual(len(a.usable_proxies()), 1)
            self.assertEqual(len(a.usable_proxies()), 1)
        ask.assert_called_once()

    def test_a_refused_proxy_is_never_used(self):
        a = self.make_airlink()
        with mock.patch('user.airlink.proxy_supports_catchup', return_value=answer(False)) as ask:
            self.assertEqual(a.usable_proxies(), [])
            self.assertEqual(a.usable_proxies(), [])
        ask.assert_called_once()

    def test_an_unreachable_proxy_is_asked_again_later(self):
        a = self.make_airlink()
        with mock.patch('user.airlink.proxy_supports_catchup',
                        return_value=answer(reachable=False)):
            self.assertEqual(a.usable_proxies(), [])
        self.assertTrue(a.proxy_retry_after['p1:8040'] > time.time())
        # Within the backoff window it is not even asked.
        with mock.patch('user.airlink.proxy_supports_catchup') as ask:
            self.assertEqual(a.usable_proxies(), [])
        ask.assert_not_called()

    def test_an_undecided_but_reachable_proxy_is_not_suppressed(self):
        # It answered; it just did not say enough to decide.  Asking again
        # next period costs nothing it has not already paid.
        a = self.make_airlink()
        with mock.patch('user.airlink.proxy_supports_catchup',
                        return_value=answer(reachable=True)) as ask:
            self.assertEqual(a.usable_proxies(), [])
            self.assertNotIn('p1:8040', a.proxy_retry_after)
            self.assertEqual(a.usable_proxies(), [])
        self.assertEqual(ask.call_count, 2)

    def test_sensors_are_never_asked(self):
        a = self.make_airlink(proxy=False)
        with mock.patch('user.airlink.proxy_supports_catchup') as ask:
            self.assertEqual(a.usable_proxies(), [])
        ask.assert_not_called()

class TestInstallerConfig(unittest.TestCase):
    """install.py's [StdReport] and [AirLink] defaults.  These are only ever
    read on a fresh `weectl extension install`, so a wrong value ships
    silently: weecfg merges the stanza with conditional_merge, which fills in
    absent keys only and never rewrites an existing weewx.conf."""

    REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    @classmethod
    def installer_config(cls):
        """install.py's config stanza, whichever form it is written in.
        Loading it needs weecfg.extension imported first: that module aliases
        itself as 'setup' in sys.modules for installers written against the
        pre-5.0 name, which is what install.py's own import resolves
        through."""
        importlib.import_module('weecfg.extension')  # registers the alias
        spec = importlib.util.spec_from_file_location(
            'airlink_install', os.path.join(cls.REPO_DIR, 'install.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.AirLinkInstaller()['config']

    def test_html_root_is_a_bare_subdirectory(self):
        """HTML_ROOT must NOT carry a public_html prefix.  weecfg prepends the
        installation's own StdReport HTML_ROOT at install time
        (ExtensionEngine.install_config -> prepend_path), so 'airlink' becomes
        public_html/airlink -- or whatever that installation uses.  Writing
        'public_html/airlink' here would land the report in
        public_html/public_html/airlink."""
        report = self.installer_config()['StdReport']['AirLinkReport']
        self.assertEqual(report['HTML_ROOT'], 'airlink')
        self.assertEqual(report['skin'], 'airlink')

    def test_demo_report_is_enabled_by_default(self):
        # The demo page is meant to render without the user turning it on.
        report = self.installer_config()['StdReport']['AirLinkReport']
        self.assertTrue(weeutil.weeutil.to_bool(report['enable']))

    def test_source_defaults(self):
        """One sensor enabled, both proxies and the second sensor off.  Values
        are compared through to_bool/to_int because a ConfigObj stanza yields
        strings where a plain dict yields bools and ints -- the installed
        weewx.conf is text either way, and airlink.py coerces on read."""
        airlink = self.installer_config()['AirLink']
        for name in ['Proxy1', 'Proxy2']:
            source = airlink[name]
            self.assertFalse(weeutil.weeutil.to_bool(source['enable']), name)
            self.assertEqual(weeutil.weeutil.to_int(source['port']), 8040, name)
            # A proxy answers from its own database on the LAN; if it has not
            # answered in a second it is down.
            self.assertEqual(weeutil.weeutil.to_int(source['timeout']), 1, name)
        self.assertEqual(airlink['Proxy1']['hostname'], 'proxy1')
        self.assertEqual(airlink['Proxy2']['hostname'], 'proxy2')
        self.assertTrue(weeutil.weeutil.to_bool(airlink['Sensor1']['enable']))
        self.assertFalse(weeutil.weeutil.to_bool(airlink['Sensor2']['enable']))
        for name in ['Sensor1', 'Sensor2']:
            source = airlink[name]
            self.assertEqual(weeutil.weeutil.to_int(source['port']), 80, name)
            self.assertEqual(weeutil.weeutil.to_int(source['timeout']), 2, name)
        self.assertEqual(airlink['Sensor1']['hostname'], 'airlink')
        self.assertEqual(airlink['Sensor2']['hostname'], 'airlink2')

    def test_the_stanza_is_commented(self):
        """The point of building the stanza from weewx.conf text rather than a
        dict: a fresh weewx.conf gets lines saying what each option is and
        which values are placeholders."""
        config = self.installer_config()
        self.assertTrue(config['AirLink'].comments['Proxy1'],
                        'the Proxy1 section ships with no comment')
        self.assertTrue(config['AirLink']['Sensor1'].comments['hostname'],
                        'the hostname placeholder ships with no comment')

    def test_the_version_is_in_lockstep(self):
        """install.py, the module and the skin all carry the version."""
        importlib.import_module('weecfg.extension')
        spec = importlib.util.spec_from_file_location(
            'airlink_install_v', os.path.join(self.REPO_DIR, 'install.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        version = module.AirLinkInstaller()['version']
        self.assertEqual(version, user.airlink.WEEWX_AIRLINK_VERSION)
        skin_conf = os.path.join(self.REPO_DIR, 'skins', 'airlink', 'skin.conf')
        skin = configobj.ConfigObj(skin_conf)
        self.assertEqual(skin['Extras']['version'], version)

if __name__ == '__main__':
    unittest.main()
