---
title: Home
layout: default
nav_order: 1
permalink: /
description: A WeeWX extension that reads a Davis AirLink sensor (or airlink-proxy), inserts particulate concentrations into every loop packet, and serves AQI and its color on demand as XTypes.
---

# weewx-airlink

**Davis AirLink air quality for WeeWX** — particulate concentrations in
every loop packet; AQI and its color computed on demand, never stored; and
the archive periods WeeWX was down for filled in from a proxy's own history.

[View on GitHub](https://github.com/chaunceygardiner/weewx-airlink){: .btn .btn-primary }
[Download weewx-airlink.zip](https://github.com/chaunceygardiner/weewx-airlink/releases/latest/download/weewx-airlink.zip){: .btn }
[Report an issue](https://github.com/chaunceygardiner/weewx-airlink/issues){: .btn }

weewx-airlink reads a
[Davis AirLink](https://www.davisinstruments.com/airlink/) air quality
sensor on the local network (or an
[airlink-proxy](https://chaunceygardiner.github.io/airlink-proxy/) service)
and populates every WeeWX loop packet with:

| Field     | Contents                    |
|-----------|-----------------------------|
| `pm1_0`   | PM1.0 concentration (µg/m³) |
| `pm2_5`   | PM2.5 concentration (µg/m³) |
| `pm10_0`  | PM10 concentration (µg/m³)  |

These use the
[wview_extended](https://github.com/weewx/weewx/blob/master/src/schemas/wview_extended.py)
column names, so they land in the database and in history graphs on their
own.

Loop packets also carry the smoother variants the AirLink computes itself.
They are not database columns, but they are ideal for real-time displays
(for example with
[weewx-loopdata](https://github.com/chaunceygardiner/weewx-loopdata)):
`pm1_0_1m`, `pm2_5_1m`, `pm10_0_1m`, `pm2_5_nowcast` and `pm10_0_nowcast`,
each with its own AQI and color where one applies.  See
[Fields in reports](fields.md).

Two more observation types are available everywhere in reports and graphs —
without being stored in the database — via WeeWX
[XTypes](https://github.com/weewx/weewx/wiki/WeeWX-V4-user-defined-types):

| Field              | Contents                                                         |
|--------------------|------------------------------------------------------------------|
| `pm2_5_aqi`        | US EPA Air Quality Index computed from `pm2_5` (2024 definition) |
| `pm2_5_aqi_color`  | The RGB color of the AQI category, as a single integer           |

{: .note }
> AQI is computed on demand rather than stored.  Storing it is both wrong
> and no faster — see
> [How AQI is computed](fields.md#how-aqi-values-are-computed).

Readings are sanity checked: a reading is rejected if fields are missing or
non-numeric, if the device reports an error, or if the reading is stale.
Sources are polled every five seconds, proxies before sensors, and the first
one that yields a sane, fresh reading wins.

## Filling gaps after downtime

While WeeWX is stopped, nothing puts air quality data into loop packets, so
the archive records a logger hands over at startup have always arrived with
empty `pm1_0`, `pm2_5` and `pm10_0` columns — permanently, in the database
and in every graph drawn from it.

If an [airlink-proxy](https://chaunceygardiner.github.io/airlink-proxy/) is
configured, weewx-airlink fills those records in from the proxy's own
archive history, averaged over exactly the period being filled.  An AirLink
queried directly keeps no history, so with no proxy configured nothing is
asked and nothing is logged.  See
[Filling in archive records](configuration.md#filling-in-archive-records-after-downtime).

![AirLinkReport](https://raw.githubusercontent.com/chaunceygardiner/weewx-airlink/master/AirLinkReport.jpg)

A demo skin ships with the extension and is enabled by default; the page
above is what it renders.

## Where to go next

* [Installation](installation.md) — requirements and the install steps.
* [Configuration](configuration.md) — sensors, proxies and gap filling.
* [Fields in reports](fields.md) — every field, and how to use it in a template.
* [Troubleshooting](troubleshooting.md) — log messages and the offline harnesses.

## Licensing

weewx-airlink is Copyright © 2020–2026 John A Kline and is licensed under
the [GNU Public License v3](https://github.com/chaunceygardiner/weewx-airlink/blob/master/LICENSE).
