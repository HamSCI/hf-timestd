# T3 admits itself: evidence at each station, offsets shared by the fleet

**Authors:** Michael Hauan (AC0G) + Claude
**Date:** 2026-11-12
**Status:** design, for Michael and Rob to review; Michael's answers of 2026-11-12 folded in.  Revises ruling B of 2026-10-30
(`2026-10-30-t3-restoration-design.md`, §8 step 7 and §9), and drops 10-30's GPSDO-only rule.

## Why

AC1SN, in Massachusetts, times itself on T2 alone.  It has no TS-1 (T6), no GPSDO pulse (T5) and
no time server on its LAN (T4).  Its host clock follows `time.google.com` across the internet, with
a sigma of 20 ms.  Its six metrology channels hear WWV and WWVH every minute, yet T3 reaches
nothing, because one setting holds it off at every station: `[timing.t3] admitted = false`.

That setting came from ruling B: T3 runs as a shadow until a B4 shadow run passes, then a person
flips the switch.  Michael expected something else, and his expectation describes the better
design: **a station uses T3 whenever T3 does better than T2 there, and stops when it doesn't.**
The station decides from evidence, all the time, without anyone flipping a switch.

The first B4 shadow run (11-01 to 11-02) shows why the decision has to keep happening.  T3 beat T2
by day: 94 % of minutes within 1 ms of T6, sd 0.56 ms against T2's 0.66.  By night it lost: sd
1.6 ms, sitting 0.7 to 1.1 ms late from 04 to 10Z, and ten of its eleven jumps over 5 ms came at
night.  A switch set once must either give up the days or accept the nights.  A decision made
minute by minute takes the days and leaves the nights to T2 until something improves them.

HFDL and CODAR exist in this fleet largely to improve them.  Their transmitters keep GPS time from
places WWV and WWVH don't cover.  But no station can learn when a CODAR radar starts its sweep, or
when an HFDL ground station keys its frame, from skywave alone.  A T6 station within ground-wave
reach can: its arrivals carry almost no path uncertainty, so it measures each transmitter's offset
directly.  sigmondb gathers those offsets.  **A T3 station can't measure them, but it can use them.**

## What success looks like

- AC1SN, and every station like it, labels its data with T3 through the day and falls back to T2
  through the night, with no one touching a setting, and says which it uses each minute.
- When HFDL and CODAR offsets arrive from the T6 stations, the same station holds T3 through more
  of the night, because fusion has more independent sources than WWV and WWVH.
- At B4, ND and KFS-OMNI, where T6 provides truth, the station checks T3 against T6 continuously
  and publishes whether T3's sigma tells the truth.  Every other station leans on that check.

## Two routes to admission

### Route 1: a station with a reference

A station with T4, T5 or T6 compares T3 and T2 against its best reference over a rolling window.
It admits T3 when T3's error beats T2's by median and by 95th percentile, the test of §9 criterion
6, and withdraws it when that stops holding.  This route needs no help from anyone.

It also produces the fleet's evidence: how often T3's stated sigma covers its actual error (§9
criterion 3, the rms of residual over sigma between 0.5 and 2).  Each reference station publishes
that figure, per hour of day, to sigmondb.

### Route 2: a station without one

A station with nothing better than T2 can't measure T3's error.  It can compare stated sigmas:
T3's, computed each minute from its sources' agreement, against T2's 20 ms.  The authority already
picks between tiers by sigma, so admission by sigma treats T3 like any other tier.

A stated sigma only means something if it tells the truth.  So route 2 admits T3 only while the
reference stations' published calibration says T3's sigma held honest, for the same hour of day,
over the last few days.  If the reference stations find T3's sigma too small at night, every route-2
station distrusts its night sigma until the model improves.  The reference stations keep the
others honest.

### Day and night need no special rule

At night T3's sigma rises (the B4 run shows why), and T2 wins the comparison on its own.  At dawn
T3's sigma falls and T3 wins again.  The station logs each change once, loudly, and counts them
(§9 criterion 5).

### The station's clock sets the windows

Michael, 2026-11-12: T3 serves every station, with or without a GPSDO.  So the windows can't be
fixed numbers.  How long a station may average, and how long it must wait before changing state,
depends on how well its sample clock holds between measurements.

- **With a GPSDO,** radiod's sample clock holds its rate for hours.  The station can average T3
  over long windows, which beats down the per-minute noise, and it needs little hysteresis.
- **Without one,** the RX888's crystal wanders.  The station measures that wander from its own
  data: the drift of its successive T3 and T2 offsets, as an Allan deviation against averaging
  time.  Averaging helps only up to the time where the clock's wander overtakes the measurement
  noise.  That crossing sets the window, and the hysteresis scales with it.

Each station computes its own crossing and reports it, so a station whose clock improves (a GPSDO
fitted, a warmer room) earns longer windows without anyone setting them.

### The offset judge follows the same clock

The judge already weighs each bench by its own sigma.  For T3 that sigma has to include the clock:
how far the sample clock can drift between T3's minutes.  With a GPSDO that term stays small and
the judge can hold a T3 verdict across a long window.  Without one it grows with time since the
last measurement, so the judge holds T3 only as long as the clock's measured stability allows.
The judge's T3 bench therefore follows the admission decision minute by minute, with the same
clock-dependent sigma, rather than ship as a one-time edit to its shadow-only list.

## Offsets from the T6 stations

The chirp design (`~/hamsci/tasks/chirp-registry/2026-11-12-codar-sigmondb-design.md`) already
builds the source side: T6 stations within ground-wave reach write CODAR and HFDL arrivals to
sigmondb, and a trust table names the stations whose measurements set each transmitter's offset.
This design adds the other side:

1. sigmondb publishes, per transmitter, its offset from UTC, the offset's uncertainty, when T6
   stations last confirmed it, and how many confirmed it.
2. Each station fetches that table on a schedule from the public copy (see "How stations read
   sigmondb") and keeps the last good copy with its age.
3. Fusion treats each CODAR or HFDL arrival as one more source for T3, with the published offset
   removed and its uncertainty added to the arrival's own.  A transmitter whose offset has gone
   stale, or that no trusted station confirms, contributes nothing.
4. Each source joins only after B4 shows it improves T3 there (ruling 9 of 10-30 stands).

### Each transmitter works like WWV

Michael, 2026-11-12: with its offset known, a CODAR radar or an HFDL ground station serves T3 the
way WWV and WWVH do now, and CHU did until Canada switched it off.  T3 already times WWV by
predicting each path: the station knows where the transmitter stands, the propagation model
predicts the mode and group delay, and the arrival minus that prediction names UTC.  A radar on
the Massachusetts coast or a ground station in Riverhead works the same way, given its position,
its published offset and a predicted path.

Three things make them worth more than another WWV channel:

- **Direction.**  WWV and WWVH reach B4 within 9 degrees of each other, so they share most of
  their ionospheric error.  Transmitters to the east, north and south cross other ionosphere, so
  their errors stay more nearly independent, and independent errors average down.
- **Number.**  Dozens of radars and ground stations transmit all day and night, many within a
  single hop.  Fusion can weigh several per minute, drop one that disagrees, and still hold.
- **Short paths.**  A transmitter a few hundred kilometres away puts a smaller and better-predicted
  group delay on its arrival than WWV's 1,000 to 3,000 km paths.  At ground-wave distances the
  path delay nearly vanishes.

Each arrival then enters fusion as WWV's do: one source per transmitter, carrying the propagation
model's predicted delay and its uncertainty plus the published offset's uncertainty.  The model
work of 10-30 stage 6 (mode choice, E/F ambiguity at night) applies to these paths too, and their
shorter, more varied geometry should help settle it.

## How stations read sigmondb

Michael, 2026-11-12: stations should query sigmondb directly, for the offsets and for details on
any station or transmitter they hear.  sigmondb lives only on wd30, in Michael's house.  wd30's
ClickHouse listens on localhost and its WireGuard address, with no public user, and should stay
that way.  The WsprDaemon servers already offer ClickHouse to the public, read-only, over HTTP and
ClickHouse's native protocol, through a `default` user whose password the project publishes.

So wd30 **publishes** the tables stations need to those servers, and stations query them there:

- **What it publishes**, as a database named `sigmondb_public`: each transmitter's offset, its
  uncertainty and its last confirmation; the trust table; the receiver and transmitter catalogs
  (position, kind, group, timing tier); and the route-2 calibration (how honest T3's sigma held, by
  station and hour of day).  Raw arrivals and captures stay on wd30.
- **How**, a job on wd30 builds each table fresh and replaces the public copy whole, so a station
  never reads half an update.  Every row carries the time wd30 built it.
- **How often**, hourly to start: offsets change over days, and calibration over hours.
- **What a station does**, one query per table on a schedule, through the public read-only user.
  It keeps the last good copy with its age, and fusion treats an old copy as an uncertain one.
- **What it costs wd30**: no new door.  The push leaves over WireGuard, the way rows already reach
  wd30 from the gateways.

Rob owns the WsprDaemon servers; the database, its size and the push credentials need his agreement.

## What stays from 10-30

- T3 never names the wrong second, and never jumps by more than 0.5 s.  A jump withdraws it at
  once, whatever the sigmas say (§9 criterion 4).
- T3 labels like T6 but never steers chrony (§7.1.1).
- Expose, never correct: T3 reports its offset and lets consumers decide.  Nothing adjusts the
  host clock from it.
- The shadow record keeps running, admitted or not, so the comparison never stops.

## The setting

`[timing.t3] admission = "auto" | "off" | "on"`, defaulting to `"auto"`.  `"off"` keeps today's
shadow-only behaviour for a station whose operator wants it; `"on"` forces admission, for testing
only.  The old `admitted = true` reads as `"on"` and `admitted = false` as `"auto"`, so every
station moves to automatic admission when this ships, without a config change.  The authority
names the route and the reason in `authority.json` each minute: "admitted by reference (T6)",
"admitted by sigma, fleet calibration holds for 14Z", "withheld: T2 sigma smaller", "withheld: no
fleet calibration for 04Z".

## Settled 2026-11-12 (Michael)

1. **Stations query sigmondb through the public WsprDaemon servers.**  wd30 publishes the tables
   stations need there (see "How stations read sigmondb"); wd30 itself opens no new door.
2. **No GPSDO requirement.**  T3 serves stations with or without one.  The 10-30 GPSDO-only ruling
   falls.
3. **Windows follow the station's clock** (see "The station's clock sets the windows").
4. **The judge follows the same clock** (see "The offset judge follows the same clock").

## Still open

- **Rob: the public servers.**  Which hosts and port serve the public read-only interface (8123
  on wsprdaemon.org and wd1-wd3 didn't answer from the devbox), and can they carry
  `sigmondb_public`?
- **A station with no T2 either.**  A station with no internet time has nothing for T3 to beat.
  Does route 2 then admit T3 on fleet calibration alone?

## Left to the builder

The window statistics, the calibration table's shape in sigmondb, the fetch schedule, the exact
words in `authority.json`, and the tests (each seen to fail first).
