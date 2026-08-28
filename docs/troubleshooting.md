---
title: Troubleshooting
layout: default
nav_order: 5
description: What weewx-airlink's log messages mean, and the harnesses for diagnosing a sensor or a proxy.
---

# Troubleshooting

## Log messages

`AirLink extension is inoperable`
: No source has `enable = true` in `[AirLink]`.  Nothing is polled.

`Found no fresh concentrations to insert.`
: The sensor has stopped answering, or is answering with stale or insane
readings.  Logged once per outage, not once per loop packet;
`Fresh concentrations available again.` is logged on recovery.

`Reading not sane: ...`
: A reading failed the sanity check.  The reason and the offending reading
are both in the message.

`Filled in pm1_0, pm2_5, pm10_0 in archive record <time>`
: An archive period WeeWX was down for was filled from a proxy's history.
One per record.

`No airlink-proxy data with which to fill ... in archive record <time>`
: Nothing overlapping that period could be found.  Logged once per outage
rather than once per record, so a long catch-up does not fill your log with
it; it is logged again after the next successful fill.

`airlink-proxy ... speaks API version ..., but catch-up requires version 2 or later`
: The proxy is too old to fill archive records.  Upgrade it to
airlink-proxy 1.0 or later, then **restart WeeWX** — the answer is
remembered for as long as WeeWX runs.

`airlink-proxy ... archives every N seconds, which does not divide into the M seconds WeeWX archives on`
: Set the proxy's `archive-interval-secs` so it divides evenly into WeeWX's
`archive_interval`, then restart WeeWX.

## Harnesses

Smoke test the sanity checker, with no sensor needed:

```
PYTHONPATH=<weewx-bin-dir> python bin/user/airlink.py --test-is-sane
```

Watch what the collector sees, against a live sensor:

```
PYTHONPATH=<weewx-bin-dir> python bin/user/airlink.py --test-extension --hostname <sensor> [--port <port>]
```

If archive records are not being filled in, ask the proxy the same questions
the extension asks it:

```
PYTHONPATH=<weewx-bin-dir> python bin/user/airlink.py --test-catchup --hostname <proxy> [--port <port>] [--archive-interval <secs>]
```

It prints the proxy's API version, its archive interval, the earliest record
it holds, whether catch-up would use it *and why not* if it would not, the
records covering the last archive period with the values that would be
filled in, and the two-minute average.

`--port` defaults to 8040 and `--archive-interval` to 300.  Pass your own
`archive_interval` if WeeWX does not archive every five minutes, since that
is what the proxy's interval is checked against.
