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


# weecfg.extension has been ExtensionInstaller's home since 2015 (WeeWX 3).
# WeeWX still aliases it as 'setup' for older installers, but that is a
# compatibility shim, not the canonical name.
from weecfg.extension import ExtensionInstaller

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

def weewx_version_at_least(minimum):
    """Is the running WeeWX at least `minimum` (e.g. (4, 6))?

    Compared as integers, not as text: WeeWX 4.10 sorts BELOW "4.6" as a
    string, so a plain comparison would reject the whole 4.10 series.
    weeutil's own version_compare cannot be used here -- it arrived after
    4.6, so it is missing from some of the versions this has to reject.
    """
    running = []
    for chunk in weewx.__version__.split('.')[:len(minimum)]:
        digits = ''
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            # A chunk we cannot parse.  If what we DID parse already settles
            # it -- "3.x" is below (4, 6) whatever the x -- say so.
            if tuple(running) < minimum[:len(running)]:
                return False
            # Otherwise accept: the cost of wrongly allowing an old WeeWX is
            # a demo page that renders its own $gettext placeholders, while
            # the cost of wrongly refusing a good one is weewxd dying at
            # import.
            return True
        running.append(int(digits))
    return tuple(running) >= minimum


airlink_dict = configobj.ConfigObj(StringIO(CONFIG))

def loader():
    if sys.version_info[0] < 3 or (sys.version_info[0] == 3 and sys.version_info[1] < 7):
        sys.exit("weewx-airlink requires Python 3.7 or later, found %s.%s" % (sys.version_info[0], sys.version_info[1]))

    # 4.6 is where WeeWX gained $lang and $gettext, which the demo skin uses.
    if not weewx_version_at_least((4, 6)):
        sys.exit("weewx-airlink requires WeeWX 4.6 or later, found %s" % weewx.__version__)

    return AirLinkInstaller()

class AirLinkInstaller(ExtensionInstaller):
    def __init__(self):
        super(AirLinkInstaller, self).__init__(
            version="4.0",
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
                ('skins/airlink/font', [
                    'skins/airlink/font/OpenSans-Regular.ttf',
                    'skins/airlink/font/OpenSans-Bold.ttf',
                    'skins/airlink/font/license.txt',
                ]),
                ('skins/airlink/lang', [
                    'skins/airlink/lang/en.conf',
                    'skins/airlink/lang/de.conf',
                    'skins/airlink/lang/fr.conf',
                    'skins/airlink/lang/nl.conf',
                    'skins/airlink/lang/es.conf',
                ]),
            ]
        )
