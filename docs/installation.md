---
title: Installation
layout: default
nav_order: 2
description: Requirements and step-by-step installation of the weewx-airlink extension.
---

# Installing weewx-airlink

## Requirements

* Python 3.7 or later
* WeeWX 4.6 or later
* The [wview_extended](https://github.com/weewx/weewx/blob/master/src/schemas/wview_extended.py)
  schema, which supplies the `pm1_0`, `pm2_5` and `pm10_0` columns
* The `requests` Python package
* Recommended: an
  [airlink-proxy](https://chaunceygardiner.github.io/airlink-proxy/) 1.0 or
  later polling your sensor.  Filling in the archive records for periods
  WeeWX was down requires one; everything else works without it.

## 1. Find your sensor and give it a stable address

Find the AirLink's IP address — in your router's DHCP client list, or in the
WeatherLink app — then browse to `http://<sensor-ip>/v1/current_conditions`.
You should see a page of JSON sensor data; that is exactly the endpoint this
extension polls.

{: .important }
> Give the sensor a DHCP reservation in your router, or a hostname in local
> DNS, so its address does not change under you.

## 2. Install the prerequisite Python package

For a WeeWX pip install, activate WeeWX's virtual environment first, then:

```
pip install requests
```

For a Debian package install of WeeWX:

```
apt install python3-requests
```

## 3. Download the extension

Download the latest `weewx-airlink.zip` from the
[releases page](https://github.com/chaunceygardiner/weewx-airlink/releases/latest).

## 4. Install it

WeeWX 5:

```
weectl extension install weewx-airlink.zip
```

WeeWX 4 (adjust the path if WeeWX is not installed in `/home/weewx`):

```
sudo /home/weewx/bin/wee_extension --install weewx-airlink.zip
```

## 5. Point it at your sensor

The install writes a commented `[AirLink]` section into `weewx.conf`.  Edit
it so a source points at your sensor or proxy, then restart WeeWX.  See
[Configuration](configuration.md).

## 6. Check it

Wait for a reporting cycle, then browse to your WeeWX site with `/airlink`
appended to the URL — for example `http://weewx-machine/weewx/airlink`.  The
PM2.5 and AQI graphs fill in over time.

If nothing appears, see [Troubleshooting](troubleshooting.md).

{: .note }
> Upgrading replaces the bundled skin in `skins/airlink/`.  If you have
> customized it, save a copy first.
