---
title: Configuration
layout: default
nav_order: 3
description: The [AirLink] section of weewx.conf -- sensors, proxies, and filling in the archive records for periods WeeWX was down.
---

# Configuring weewx-airlink

Everything lives in the `[AirLink]` section of `weewx.conf`.  A fresh
install writes it with comments explaining each option.

```
[AirLink]
    [[Proxy1]]
        enable = false
        hostname = proxy1
        port = 8040
        timeout = 1
    [[Sensor1]]
        enable = true
        hostname = airlink
        port = 80
        timeout = 2
    [[Sensor2]]
        enable = false
        hostname = airlink2
        port = 80
        timeout = 2
```

| Option     | Default          | Meaning                              |
|------------|------------------|--------------------------------------|
| `enable`   | false            | Whether this source is polled        |
| `hostname` |                  | Hostname or IP address of the source |
| `port`     | 80 (proxy: 8040) | Port to connect on                   |
| `timeout`  | 10 (proxy: 1)    | HTTP timeout (seconds)               |

## Sources

Sources come in two kinds.  `[[Proxy1]]`, `[[Proxy2]]` and so on are
instances of
[airlink-proxy](https://chaunceygardiner.github.io/airlink-proxy/);
`[[Sensor1]]`, `[[Sensor2]]` and so on are AirLink devices queried directly.
There is no limit on the number of either, but each kind's numbering must
start at 1 and be consecutive — a gap ends the scan.

The two kinds differ only in the defaults they pick for `port` and
`timeout`: a proxy answers out of its own database and is expected to answer
quickly, while an AirLink's own processor is slow and easily overwhelmed.

On each polling round — every five seconds — sources are interrogated in
order, all proxies low numbers to high and then all sensors, and the first
one that yields a sane, fresh reading wins.  No further sources are tried.

A reading is considered fresh for one archive interval; stale readings are
never inserted into loop packets.

{: .important }
> `timeout` governs every request to that source — the five-second polling
> that feeds loop packets as well as the archive filling described below.
> One second suits a proxy answering out of its own database on the local
> network.  If you move a `[[SensorN]]` to a `[[ProxyN]]` and it is not on
> your local network, set `timeout` explicitly rather than taking the
> default.

{: .note }
> Earlier versions had no `[[ProxyN]]` sections, so an airlink-proxy was
> configured as a `[[SensorN]]` with its port set to 8000.  That still works
> and is still polled exactly as before — but the extension cannot tell such
> a section from a bare AirLink, and only a proxy can be asked for archive
> history, so gap filling does not happen until you move it.

## Filling in archive records after downtime

While WeeWX is stopped, nothing puts air quality data into loop packets.
The archive records a logger hands over when WeeWX starts again therefore
arrive with empty `pm1_0`, `pm2_5` and `pm10_0` columns, and the hole is
permanent — in the database and in every graph drawn from it.

If a `[[ProxyN]]` is configured, weewx-airlink fills those records in: for
each archive period it contributed nothing to, it asks the proxy for the
archive records covering exactly that period and averages them.  An AirLink
queried directly keeps no history, so with no proxy configured nothing is
asked and nothing is logged.

Values written this way are still subject to `[StdQC]` and
`[StdCalibrate]`.  Those services run in `process_services`, which WeeWX
loads after the `data_services` this extension installs into, so a
`[StdQC] [[MinMax]]` limit on `pm2_5` filters a filled-in value exactly as
it filters a live one.

Only `pm1_0`, `pm2_5` and `pm10_0` are filled in.  The 1m and nowcast
variants are loop-only — they are not database columns — and a value is
only ever written for a period this extension put nothing into, so a period
WeeWX was up for is never touched.

### What the proxy has to satisfy

Two conditions have to hold, and both are checked once per proxy and logged
when they fail:

* The proxy must speak **airlink-proxy API version 2 or later**, which means
  airlink-proxy 1.0 or later.  Earlier proxies file the single poll that
  landed on the archive boundary rather than an average over the interval,
  which is not what the period contained.
* The proxy's `archive-interval-secs` must **divide evenly into** WeeWX's
  `archive_interval`.  A shorter interval is fine — several of the proxy's
  records cover one WeeWX period and are averaged together, so a proxy on 60
  seconds serves a WeeWX archiving every 300.  A longer interval, or one
  that does not divide, cannot answer for a WeeWX period at all, since the
  period can fall entirely inside one of the proxy's records.  Rather than
  fill such a period badly, catch-up is refused and says so in the log.

{: .important }
> Both answers are remembered for as long as WeeWX runs.  After upgrading an
> airlink-proxy from API version 1 to 2, or changing its
> `archive-interval-secs`, **restart WeeWX** — until you do, the extension
> goes on acting on the answer it got at startup.

### When no archive record covers the period

For a period that closed recently and that no archive record covers yet, the
proxy's two-minute average is used instead, and failing that the reading
already in hand.  Both are subject to one rule: **a reading may fill a
period only if it overlaps that period.**

The two-minute average covers the two minutes ending at its freshest sample,
so it qualifies when that window reaches into the period.  The reading in
hand is a single instantaneous sample, so it qualifies only if it was taken
inside the period.  A sample taken after the period closed describes a
moment the period does not contain, however recent it is, and is not used.

That rule is also what keeps a proxy's last two-minute average out once the
AirLink stops answering: the proxy goes on serving it, but it no longer
reaches the periods being filled.

Any period nothing overlaps keeps its empty columns.  That is the honest
answer, not a failure.
