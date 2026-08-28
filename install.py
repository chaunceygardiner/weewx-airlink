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
from io import StringIO

import configobj

import weewx


from setup import ExtensionInstaller

# Written as weewx.conf text rather than a dict so that the stanza weectl
# merges into a fresh weewx.conf arrives with its comments: ConfigObj keeps
# them, a dict has nowhere to put them.
CONFIG = """
[StdReport]
    [[AirLinkReport]]
        # The "AirLinkReport" uses the "airlink" skin, which showcases the
        # extension.  Images and files are placed in a dedicated
        # subdirectory.
        HTML_ROOT = airlink
        enable = true
        skin = airlink

[AirLink]
    # This section is for configuring the extension weewx-airlink.
    # See the README.md for details.
    #
    # Sources are polled every 5 seconds, proxies first and then sensors,
    # low numbers to high.  The first one that yields a sane, fresh reading
    # wins and no further sources are tried.  The numbering of each kind
    # must start at 1 and be consecutive; a gap ends the scan.

    # Proxies are instances of airlink-proxy.  Only a proxy keeps a history
    # of readings, so only a proxy can fill in the archive records for
    # periods WeeWX was not running.
    [[Proxy1]]
        enable = False
        # Replace with the host name or IP address of the first proxy
        hostname = proxy1
        # Port is usually 8040 (purple-proxy uses 8000 and
        # airgradient-proxy 8080; airlink-proxy before 1.0 used 8000)
        port = 8040
        # http timeout (seconds).  A proxy answers out of its own database
        # on the local network; if it has not answered in a second, it is
        # down.
        timeout = 1
    [[Proxy2]]
        enable = False
        # Replace with the host name or IP address of the second proxy
        hostname = proxy2
        port = 8040
        timeout = 1

    # Sensors are AirLink devices, queried directly.
    [[Sensor1]]
        enable = True
        # Replace with the host name or IP address of the first sensor
        hostname = airlink
        # Port is usually 80
        port = 80
        # http timeout (seconds)
        timeout = 2
    [[Sensor2]]
        enable = False
        # Replace with the host name or IP address of the second sensor
        hostname = airlink2
        port = 80
        timeout = 2
"""

airlink_dict = configobj.ConfigObj(StringIO(CONFIG))

def loader():
    if sys.version_info[0] < 3 or (sys.version_info[0] == 3 and sys.version_info[1] < 7):
        sys.exit("weewx-airlink requires Python 3.7 or later, found %s.%s" % (sys.version_info[0], sys.version_info[1]))

    if weewx.__version__ < "4":
        sys.exit("weewx-airlink requires WeeWX 4, found %s" % weewx.__version__)

    return AirLinkInstaller()

class AirLinkInstaller(ExtensionInstaller):
    def __init__(self):
        super(AirLinkInstaller, self).__init__(
            version="3.0",
            name='airlink',
            description='Record air quality as provided by a Davis AirLink sensor.',
            author="John A Kline",
            author_email="john@johnkline.com",
            data_services='user.airlink.AirLink',
            config = airlink_dict,
            files=[
                ('bin/user', ['bin/user/airlink.py']),
                ('skins/airlink', [
                    'skins/airlink/index.html.tmpl',
                    'skins/airlink/skin.conf',
                ]),
            ]
        )
