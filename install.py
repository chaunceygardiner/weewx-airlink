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

import sys
import weewx


from setup import ExtensionInstaller

def loader():
    if sys.version_info[0] < 3 or (sys.version_info[0] == 3 and sys.version_info[1] < 7):
        sys.exit("weewx-airlink requires Python 3.7 or later, found %s.%s" % (sys.version_info[0], sys.version_info[1]))

    if weewx.__version__ < "4":
        sys.exit("weewx-airlink requires WeeWX 4, found %s" % weewx.__version__)

    return AirLinkInstaller()

class AirLinkInstaller(ExtensionInstaller):
    def __init__(self):
        super(AirLinkInstaller, self).__init__(
            version="0.2",
            name='airlink',
            description='Record air quality as provided by a Davis AirLink sensor.',
            author="John A Kline",
            author_email="john@johnkline.com",
            data_services='user.airlink.AirLink',
            config = {
                'StdReport': {
                    'AirLinkReport': {
                        'HTML_ROOT':'airlink',
                        'enable': 'true',
                        'skin':'airlink',
                    },
                },
                'AirLink': {
                    'Sensor1'  : {
                        'enable'     : True,
                        'hostname'   : 'airlink',
                        'port'       : '80',
                        'timeout'    : '2',
                    },
                    'Sensor2': {
                        'enable'     : False,
                        'hostname'   : 'airlink2',
                        'port'       : '80',
                        'timeout'    : '2',
                    },
                },
            },
            files=[
                ('bin/user', ['bin/user/airlink.py']),
                ('skins/nws', [
                    'skins/airlink/index.html.tmpl',
                    'skins/airlink/skin.conf',
                ]),
            ]
        )
