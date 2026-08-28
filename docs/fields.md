---
title: Fields in reports
layout: default
nav_order: 4
description: Every field weewx-airlink provides, how to use it in a template, and how AQI is computed.
---

# Using weewx-airlink fields in reports

## Stored in the database

These three are inserted into every loop packet under the
[wview_extended](https://github.com/weewx/weewx/blob/master/src/schemas/wview_extended.py)
column names, so WeeWX accumulates and stores them like any other
observation, and history graphs work on their own.

| Field     | Contents                    |
|-----------|-----------------------------|
| `pm1_0`   | PM1.0 concentration (µg/m³) |
| `pm2_5`   | PM2.5 concentration (µg/m³) |
| `pm10_0`  | PM10 concentration (µg/m³)  |

## Loop-only: the AirLink's own averages

The AirLink computes smoother variants itself.  These reach loop packets but
are **not** database columns, so they are ideal for real-time displays — for
example with
[weewx-loopdata](https://github.com/chaunceygardiner/weewx-loopdata) — and
unavailable in history graphs.

| Field                      | Contents                                     |
|----------------------------|----------------------------------------------|
| `pm1_0_1m`                 | PM1.0, 1-minute average                      |
| `pm2_5_1m`                 | PM2.5, 1-minute average                      |
| `pm10_0_1m`                | PM10, 1-minute average                       |
| `pm2_5_1m_aqi`             | AQI computed from `pm2_5_1m`                 |
| `pm2_5_1m_aqi_color`       | RGB color of that AQI's category             |
| `pm2_5_nowcast`            | PM2.5 [NowCast](https://en.wikipedia.org/wiki/NowCast_(air_quality_index)) average |
| `pm2_5_nowcast_aqi`        | AQI computed from `pm2_5_nowcast`            |
| `pm2_5_nowcast_aqi_color`  | RGB color of that AQI's category             |
| `pm10_0_nowcast`           | PM10 NowCast average                         |

If a 1-minute average is unavailable the instantaneous reading is
substituted; if a NowCast average is unavailable nothing is substituted.

## Computed on demand: AQI and its color

| Field              | Contents                                                         |
|--------------------|------------------------------------------------------------------|
| `pm2_5_aqi`        | US EPA Air Quality Index computed from `pm2_5` (2024 definition) |
| `pm2_5_aqi_color`  | The RGB color of the AQI category, as a single integer           |

These are WeeWX
[XTypes](https://github.com/weewx/weewx/wiki/WeeWX-V4-user-defined-types).
They are never stored; they are computed from `pm2_5` whenever a report asks
for one, including for aggregates and series, so plots and history work
exactly as they do for a stored column.

## In a template

Current values:

```
$current.pm1_0
$current.pm2_5
$current.pm10_0
$current.pm2_5_aqi
$current.pm2_5_aqi_color
```

Aggregates over a period behave normally:

```
$day.pm2_5.avg
$day.pm2_5_aqi.max
$week.pm2_5_aqi.avg
```

A plot is declared like any other observation:

```
[[[dayaqi]]]
    [[[[pm2_5_aqi]]]]
```

## How AQI values are computed

`pm2_5_aqi` is the US EPA Air Quality Index for the PM2.5 concentration,
using the **2024** definition of the breakpoint table: AQI 500 is anchored
at 325.4 µg/m³, and values above 500 are not capped.  `pm2_5_aqi_color` is
the EPA's own RGB color for the category the AQI falls in, delivered as a
single integer.

Aggregates convert **after** aggregating: `$day.pm2_5_aqi.avg` is the AQI of
the day's average concentration, not the average of each record's AQI.  That
is the EPA-correct order of operations — AQI is a non-linear transform of
concentration, so the two are not the same number.

{: .important }
> Do not add a `pm2_5_aqi` column to your database schema.  It is both wrong
> and no faster: the conversion runs once per plotted point rather than once
> per row, whole-day spans are served from the `pm2_5` daily-summary table,
> and a stored column would hold values averaged in the wrong order.  If you
> already added one, the
> [README](https://github.com/chaunceygardiner/weewx-airlink#if-you-added-an-aqi-column-to-your-database)
> has the step-by-step removal procedure.

## AQI categories

| AQI     | Category                       |
|---------|--------------------------------|
| 0–50    | Good                           |
| 51–100  | Moderate                       |
| 101–150 | Unhealthy for Sensitive Groups |
| 151–200 | Unhealthy                      |
| 201–300 | Very Unhealthy                 |
| 301+    | Hazardous                      |
