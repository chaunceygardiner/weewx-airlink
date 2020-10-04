
#
#    See the file LICENSE.txt for your full rights.
#
"""Test processing packets."""

import json
import logging
import unittest

import weeutil.logger

import user.airlink

log = logging.getLogger(__name__)

# Set up logging using the defaults.
weeutil.logger.setup('test_config', {})

class AirLinkTests(unittest.TestCase):
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

    def test_compute_pm2_5_aqi(self):

        # Good
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi( 0.0), 0)
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi( 6.0), 25)
        aqi = user.airlink.AQI.compute_pm2_5_aqi(11.26)
        self.assertTrue(aqi > 46.666 and aqi < 46.667)
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi(12.0), 50)
        # 12.099 is truncated to 12
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi(12.099), 50)

        # Moderate
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi(12.1),  51)
        aqi = user.airlink.AQI.compute_pm2_5_aqi(23.7)
        self.assertTrue(aqi > 75.3948 and aqi < 75.3949)
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi(35.499), 100)

        # USG
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi(35.5), 101)
        aqi = user.airlink.AQI.compute_pm2_5_aqi(45.4)
        self.assertTrue(aqi > 125.3768 and aqi < 125.3769)
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi(55.4), 150)

        # Unhealthy
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi( 55.5), 151)
        aqi = user.airlink.AQI.compute_pm2_5_aqi(102.9)
        self.assertTrue(aqi > 175.4741 and aqi < 175.4742)
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi(150.4), 200)

        # Very Unhealthy
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi(150.5), 201)
        aqi = user.airlink.AQI.compute_pm2_5_aqi(200.4)
        self.assertTrue(aqi > 250.4504 and aqi < 250.4505)
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi(250.4), 300)

        # Harzadous
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi(250.5), 301)
        aqi = user.airlink.AQI.compute_pm2_5_aqi(300.4)
        self.assertTrue(aqi > 350.4504 and aqi < 350.4505)
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi(350.4), 400)

        # Harzadous
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi(350.5), 401)
        aqi = user.airlink.AQI.compute_pm2_5_aqi(425.45)
        self.assertTrue(aqi > 450.4 and aqi < 450.6)
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi(500.4), 500)

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

    def test_compute_pm2_5_aqi_color(self):

        # Good
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color( 0), 128 << 8)
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color(25), 128 << 8)
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color(46), 128 << 8)
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color(50), 128 << 8)

        # Moderate
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color( 51), (255 << 16) + (255 << 8))
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color( 75), (255 << 16) + (255 << 8))
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color(100), (255 << 16) + (255 << 8))

        # USG
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color(101), (255 << 16) + (140 << 8))
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color(125), (255 << 16) + (140 << 8))
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color(150), (255 << 16) + (140 << 8))

        # Unhealthy
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color(151), (255 << 16))
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color(175), (255 << 16))
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color(200), (255 << 16))

        # Very Unhealthy
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color(201), (128 << 16) + 128)
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color(250), (128 << 16) + 128)
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color(300), (128 << 16) + 128)

        # Harzadous
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color(301), 128 << 16)
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color(350), 128 << 16)
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color(400), 128 << 16)

        # Harzadous
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color(401), 128 << 16)
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color(450), 128 << 16)
        self.assertEqual(user.airlink.AQI.compute_pm2_5_aqi_color(500), 128 << 16)

    def test_compute_pm_2p5(self):
        # PM2.5=0.541*PA_cf1(avgAB)-0.0618*RH +0.00534*T +3.634
        self.assertEqual(user.airlink.AQI.compute_pm_2p5_us_epa_correction(0.0, 0.0, 0.0), 3.634)
        val = user.airlink.AQI.compute_pm_2p5_us_epa_correction(0.0, 60.0, 80.0)
        self.assertTrue(val > 0.353199 and val < 0.3532)
        val = user.airlink.AQI.compute_pm_2p5_us_epa_correction(0.0, 100.0, 100.0)
        self.assertEqual(val, 0.0)

    def test_is_sane(self):
        minimal= ('{ \
                  "data": { \
                           "did": null, \
                           "name": "paloaltoweather.com", \
                           "ts": 1600485172, \
                           "conditions": [ \
                                          { \
                                           "lsid": null, \
                                           "data_structure_type": 6, \
                                           "temp": 70.8, \
                                           "hum": 67.6, \
                                           "dew_point": 59.5, \
                                           "wet_bulb": 62.8, \
                                           "heat_index": 70.9, \
                                           "pm_1_last": 4, \
                                           "pm_2p5_last": 9, \
                                           "pm_10_last": 11, \
                                           "pm_1": null, \
                                           "pm_2p5": null, \
                                           "pm_2p5_last_1_hour": null, \
                                           "pm_2p5_last_3_hours": null, \
                                           "pm_2p5_last_24_hours": null, \
                                           "pm_2p5_nowcast": null, \
                                           "pm_10": null, \
                                           "pm_10_last_1_hour": null, \
                                           "pm_10_last_3_hours": null, \
                                           "pm_10_last_24_hours": null, \
                                           "pm_10_nowcast": 12.84, \
                                           "last_report_time": 100, \
                                           "pct_pm_data_last_1_hour": 0, \
                                           "pct_pm_data_last_3_hours": 0, \
                                           "pct_pm_data_nowcast": 0, \
                                           "pct_pm_data_last_24_hours": 0 \
                                          } \
                                         ] \
                          }, \
                  "error": null \
                 }')
        observed_packet_with_pct_fields_of_none = ('{"data": {"did": "001D0A100214", \
            "name": "airlink", "ts": 1601491799, "conditions": [{"lsid": 349506, \
            "data_structure_type": 6, "temp": 71.9, "hum": 70.1, "dew_point": 61.6, \
            "wet_bulb": 64.5, "heat_index": 72.5, "pm_1_last": 15, "pm_2p5_last": 24, \
            "pm_10_last": 27, "pm_1": null, "pm_2p5": null, "pm_2p5_last_1_hour": null, \
            "pm_2p5_last_3_hours": null, "pm_2p5_last_24_hours": null, \
            "pm_2p5_nowcast": null, "pm_10": null, "pm_10_last_1_hour": null, \
            "pm_10_last_3_hours": null, "pm_10_last_24_hours": null, \
            "pm_10_nowcast": null, "last_report_time": 1601491799, \
            "pct_pm_data_last_1_hour": null, "pct_pm_data_last_3_hours": null, \
            "pct_pm_data_nowcast": null, "pct_pm_data_last_24_hours": null \
            }]}, "error": null}')
        good = ('{ \
                  "data": { \
                           "did": "001D0A100214", \
                           "name": "paloaltoweather.com", \
                           "ts": 1600485172, \
                           "conditions": [ \
                                          { \
                                           "lsid": 347825, \
                                           "data_structure_type": 6, \
                                           "temp": 70.8, \
                                           "hum": 67.6, \
                                           "dew_point": 59.5, \
                                           "wet_bulb": 62.8, \
                                           "heat_index": 70.9, \
                                           "pm_1_last": 4, \
                                           "pm_2p5_last": 9, \
                                           "pm_10_last": 11, \
                                           "pm_1": 5.43, \
                                           "pm_2p5": 8.23, \
                                           "pm_2p5_last_1_hour": 8.91, \
                                           "pm_2p5_last_3_hours": 9.09, \
                                           "pm_2p5_last_24_hours": 9.09, \
                                           "pm_2p5_nowcast": 8.96, \
                                           "pm_10": 11.90, \
                                           "pm_10_last_1_hour": 12.54, \
                                           "pm_10_last_3_hours": 13.11, \
                                           "pm_10_last_24_hours": 13.11, \
                                           "pm_10_nowcast": 12.84, \
                                           "last_report_time": 1600485172, \
                                           "pct_pm_data_last_1_hour": 100, \
                                           "pct_pm_data_last_3_hours": 91, \
                                           "pct_pm_data_nowcast": 22, \
                                           "pct_pm_data_last_24_hours": 11 \
                                          } \
                                         ] \
                          }, \
                  "error": null \
                 }')
        data_structure_5_response = ('{"data": {"did": "001D0A1000AF", "name": "LusherClose Sheringham", "ts": 1601320120, "conditions": [{"lsid": 349639, "data_structure_type": 5, "temp": 59.0, "hum": 69.3, "dew_point": 48.9, "wet_bulb": 52.6, "heat_index": 58.1, "pm_1_last": 0, "pm_2p5_last": 0, "pm_10_last": 1, "pm_1": 0.61, "pm_2p5": 0.61, "pm_2p5_last_1_hour": 1.07, "pm_2p5_last_3_hours": 1.25, "pm_2p5_last_24_hours": 1.25, "pm_2p5_nowcast": 1.2, "pm_10p0": 3.3, "pm_10p0_last_1_hour": 1.9, "pm_10p0_last_3_hours": 2.52, "pm_10p0_last_24_hours": 2.52, "pm_10p0_nowcast": 2.27, "last_report_time": 1601320120, "pct_pm_data_last_1_hour": 100, "pct_pm_data_last_3_hours": 95, "pct_pm_data_nowcast": 23, "pct_pm_data_last_24_hours": 11}]}, "error": null}')
        # Bad pm_1 field.
        bad = ('{ \
                  "data": { \
                           "did": "001D0A100214", \
                           "name": "paloaltoweather.com", \
                           "ts": 1600485172, \
                           "conditions": [ \
                                          { \
                                           "lsid": 347825, \
                                           "data_structure_type": 6, \
                                           "temp": 70.8, \
                                           "hum": 67.6, \
                                           "dew_point": 59.5, \
                                           "wet_bulb": 62.8, \
                                           "heat_index": 70.9, \
                                           "pm_1_last": 4, \
                                           "pm_2p5_last": 9, \
                                           "pm_10_last": 11, \
                                           "pm_1": "abc", \
                                           "pm_2p5": 8.23, \
                                           "pm_2p5_last_1_hour": 8.91, \
                                           "pm_2p5_last_3_hours": 9.09, \
                                           "pm_2p5_last_24_hours": 9.09, \
                                           "pm_2p5_nowcast": 8.96, \
                                           "pm_10": 11.90, \
                                           "pm_10_last_1_hour": 12.54, \
                                           "pm_10_last_3_hours": 13.11, \
                                           "pm_10_last_24_hours": 13.11, \
                                           "pm_10_nowcast": 12.84, \
                                           "last_report_time": 1600485172, \
                                           "pct_pm_data_last_1_hour": 100, \
                                           "pct_pm_data_last_3_hours": 91, \
                                           "pct_pm_data_nowcast": 22, \
                                           "pct_pm_data_last_24_hours": 11 \
                                          } \
                                         ] \
                          }, \
                  "error": null \
                 }')
        j = json.loads(minimal)
        sane, _ = user.airlink.is_sane(j)
        assert(sane)
        j = json.loads(observed_packet_with_pct_fields_of_none)
        sane, _ = user.airlink.is_sane(j)
        assert(sane)
        j = json.loads(good)
        sane, _ = user.airlink.is_sane(j)
        assert(sane)
        j = json.loads(data_structure_5_response)
        user.airlink.convert_data_structure_type_5_to_6(j)
        sane, _ = user.airlink.is_sane(j)
        assert(sane)
        j = json.loads(bad)
        sane, msg = user.airlink.is_sane(j)
        assert(not sane)
        assert(msg == 'Missing or malformed "pm_1" field')

if __name__ == '__main__':
    unittest.main()
