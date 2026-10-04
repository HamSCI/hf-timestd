# A LAN GNSS time server that also feeds VTEC

One small box on the station's LAN can do two jobs at once. It keeps time for the
whole network as a stratum-1 NTP server, disciplined by its GNSS receiver's PPS.
It also streams that receiver's raw measurements, from which hf-timestd's
`timestd-vtec` service computes the vertical total electron content (VTEC) of
the ionosphere above the station.

Neither job is required. A station keeps time from the internet without the
box, and hf-timestd measures HF TEC from WWV/WWVH timing without any GPS
([PHYSICS.md](PHYSICS.md)). The box improves both: the host clock settles to
within microseconds instead of milliseconds, and GNSS VTEC gives an independent
ionosphere to compare the HF measurements against.

This guide replaces three older documents: `ZED_F9P_TEC_CONFIGURATION.md`,
`GPS_TEC_OPTIONAL.md` and §4 of `STATION_SETUP_GUIDE.md`. They described only
u-blox receivers and only the VTEC half; the first two now live in `archive/`.

---

## 1. Two boxes that run today

| | AC0G home | AC0G-ND, Fargo |
|---|---|---|
| Box | `ScreenPI4`, Raspberry Pi 4, Debian 12 | `AC0G-ND-TIME`, Raspberry Pi 4, Debian 13 |
| LAN address | 192.168.1.80 | 192.168.8.144 |
| Receiver | u-blox ZED-F9P | Quectel LG290P |
| What it streams | **UBX**: `RXM-RAWX` + `NAV-SAT` | **RTCM 3.3**: MSM7 + 1005 + 1033 |
| Relay to the LAN | `str2str` (RTKLIB), TCP port 9000 | `str2str` (RTKLIB), TCP port 9000 |
| chrony on PPS | ±224 ns | ±4.5 µs |
| Station it serves | AC0G-B4 | AC0G-ND |

hf-timestd tells the two apart by itself (§6), so a station needs only the
box's address and port.

The two receivers differ in one way that matters. A u-blox receiver reports
each satellite's elevation itself (`NAV-SAT`). An RTCM base station does not:
it sends observations and its own antenna position, but no satellite orbits.
For RTCM, hf-timestd therefore downloads the day's broadcast orbits from the
internet and computes the elevations (§6.3).

---

## 2. Hardware

- **A small Linux computer** with a GPIO header. A Raspberry Pi 4 does it.
- **A dual-frequency GNSS receiver.** VTEC needs two carrier frequencies on the
  same satellite (GPS L1 and L2). The ZED-F9P (L1/L2) and the LG290P
  (L1/L2/L5/E6) both qualify; a single-frequency receiver does not.
- **The receiver's PPS output wired to a GPIO pin** (GPIO 18 below) and ground.
  The serial or USB link carries the data; the PPS wire carries the time.
- **A GNSS antenna with a clear sky view.**

---

## 3. The box's operating system and PPS

Install Raspberry Pi OS or Debian, then enable the kernel PPS driver on the
GPIO pin. In `/boot/firmware/config.txt`:

```
dtoverlay=pps-gpio,gpiopin=18
```

⚠ Spell the parameter `gpiopin`. `AC0G-ND-TIME` carried `gpiopi=18`, which the
overlay ignores; it worked only because 18 happens to be the default pin. On
any other pin the PPS would quietly vanish.

Reboot, then check that the kernel sees one pulse a second:

```bash
ls -l /dev/pps0
sudo apt install pps-tools
sudo ppstest /dev/pps0          # one "assert" line per second; Ctrl-C to stop
```

---

## 4. The receiver needs a name that survives a reset

A USB receiver appears as `/dev/ttyACM0` — until it resets, when Linux may hand
it `/dev/ttyACM1` instead. Anything configured for `ttyACM0` then fails.
`AC0G-ND-TIME` lost its stream that way on 2026-10-04: after the LG290P reset,
`str2str` failed to start 73 times in a row because `ttyACM0` no longer
existed.

Give the receiver a fixed name keyed on its USB serial number. Find the
numbers:

```bash
udevadm info -q property -n /dev/ttyACM0 | grep -E 'ID_VENDOR_ID|ID_MODEL_ID|ID_SERIAL_SHORT'
```

Then write `/etc/udev/rules.d/90-gnss-receiver.rules`, with your own values:

```
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d3", ATTRS{serial}=="5B5E068689", SYMLINK+="ttyGNSS"
```

```bash
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=tty --action=add
ls -l /dev/ttyGNSS
```

Use `/dev/ttyGNSS` everywhere below.

The LG290P board on ND presents a CH9102 USB-serial chip (1a86:55d3) with a
serial number, as above.  A ZED-F9P presents u-blox's own USB (1546:01a9)
**with no serial number**, so key its rule on vendor and product alone — fine
while the box carries one u-blox receiver:

```
SUBSYSTEM=="tty", ATTRS{idVendor}=="1546", ATTRS{idProduct}=="01a9", SYMLINK+="ttyGNSS"
```

(The home box still names `ttyACM0` directly; it has run 50 days without a
reset, but the same failure waits for it.)

**Only one program may own the port.** If gpsd is installed and set to the same
device, it fights the relay. Either disable it (`sudo systemctl disable --now
gpsd gpsd.socket`) or have the relay feed it — but not both on one port.

---

## 5. The time server: chrony on PPS

PPS marks *when* each second begins but not *which* second it is. chrony
numbers the seconds from another source — internet NTP servers below — and
takes the edge itself from the PPS. Add to `/etc/chrony/chrony.conf`, or a file
in `/etc/chrony/conf.d/`:

```
refclock PPS /dev/pps0 refid PPS precision 1e-9 prefer
allow 192.168.8.0/24          # your LAN
local stratum 1
```

Keep the distribution's `pool` line; it supplies the second numbers.

```bash
sudo systemctl restart chrony
chronyc -n sources
```

Expect `#* PPS` within a few minutes, with an error of a few microseconds or
better (`ScreenPI4`: `+/- 224ns`; `AC0G-ND-TIME`: `+/- 4511ns`). If the box must keep time without
internet, chrony needs the seconds from the receiver itself — gpsd's shared
memory driver (`refclock SHM 0`) does that, at the cost of gpsd owning the
serial port (§4).

---

## 6. The VTEC stream

### 6.1 Which messages the receiver must send

| Receiver | Must send, at 1 Hz | What hf-timestd uses it for |
|---|---|---|
| u-blox (UBX) | `RXM-RAWX` | pseudorange and carrier phase, per signal |
| | `NAV-SAT` | each satellite's elevation and azimuth |
| RTCM 3.3 | `1077` (GPS MSM7) — or `1074` (MSM4), or legacy `1004` | pseudorange and phase, GPS L1 + L2 |
| | `1005` or `1006`, every 1–10 s | the antenna's position, for elevations |
| | `1087` `1097` `1127` (GLONASS, Galileo, BeiDou) | carried, not yet used |

Today hf-timestd computes VTEC from **GPS L1 + L2 only**. GPS satellites with
no L2C signal (older Block IIR) drop out; the others pass through untouched.

**u-blox ZED-F9P.** Enable the two messages on the port the box reads (USB
here; `_UART1` for the serial port) and save them in flash. With `pyubx2`,
which hf-timestd already installs:

```python
from pyubx2 import UBXMessage, SET_LAYER_RAM, SET_LAYER_BBR, SET_LAYER_FLASH, TXN_NONE
import serial
msg = UBXMessage.config_set(
    SET_LAYER_RAM | SET_LAYER_BBR | SET_LAYER_FLASH, TXN_NONE,
    [("CFG_MSGOUT_UBX_RXM_RAWX_USB", 1),
     ("CFG_MSGOUT_UBX_NAV_SAT_USB", 1),
     ("CFG_RATE_MEAS", 1000)])
with serial.Serial("/dev/ttyGNSS", 115200, timeout=2) as s:
    s.write(msg.serialize())
```

u-center does the same through its configuration view.

**Quectel LG290P.** Configure the receiver, with Quectel's QGNSS tool or its
`$PQTM…` commands, to emit 1077, 1087, 1097, 1127 and 1005 at 1 Hz, and save
the configuration. Then check the result with §8's census — the stream is the
authority, not the configuration tool. Note that an LG290P base engine sends
**no broadcast ephemeris** in RTCM (no 1019/1020/1042/1046), whatever you ask
of it; §6.3 supplies the orbits instead.

### 6.2 Put the stream on the LAN: TCP port 9000

hf-timestd connects *to* the box, so the box must **listen**.

Both boxes use RTKLIB's `str2str` (`sudo apt install rtklib`), which passes
the receiver's bytes through unchanged, whatever their format.

**RTCM** (the LG290P box). `/etc/systemd/system/str2str.service`:

```ini
[Unit]
Description=RTKLIB str2str RTCM3 TCP server
After=network.target

[Service]
Type=simple
User=mjh
ExecStartPre=/bin/sh -c 'stty -F /dev/ttyGNSS 460800 raw -echo'
ExecStart=/usr/bin/str2str -in serial://ttyGNSS:460800:8:n:1 -out tcpsvr://:9000 -s 1000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Use the short name (`serial://ttyGNSS`), not a `/dev/serial/by-id/…` path:
older RTKLIB builds prefix `/dev/` themselves. `tcpsvr://` listens;
`tcpcli://` would push to one address, and nothing at the station listens.
`str2str` serves several clients at once.

**UBX** (the home box, `/etc/systemd/system/rawx-server.service`): the same
unit, with the u-blox's port speed and a `#ubx` format tag on the input:

```
ExecStart=/usr/bin/str2str -in serial://ttyGNSS:115200:8:n:1#ubx -out tcpsvr://:9000
```

**UBX with ser2net instead** (`sudo apt install ser2net`), if you prefer it.
`/etc/ser2net.yaml`:

```yaml
connection: &gnss
  accepter: tcp,9000
  connector: serialdev,/dev/ttyGNSS,115200n81,local
  options:
    kickolduser: true
```

`kickolduser` lets a reconnecting station take over from its own stale
connection; it also means a second client pushes the first one off.

### 6.3 Orbits for an RTCM receiver

For an RTCM stream, `timestd-vtec` downloads BKG's daily multi-GNSS broadcast
navigation file, `BRDC00WRD_S_<year><doy>0000_01D_MN.rnx.gz`. BKG builds it
from live IGS data streams and refreshes it every 15 minutes, so it holds
orbits valid for *now*. It needs no login. The service fetches it every 15
minutes into `ephemeris_dir`, keeps yesterday's file as well before 04:00 UTC,
and keeps whatever it already has if a download fails.

**So an RTCM station needs internet access for VTEC.** A u-blox station does
not.

---

## 7. The station side

Two independent settings, both recorded as the station's site deltas so a
reflash restores them (`ops/site-deltas/<site>/`).

**Time.** `/etc/chrony/conf.d/local-timeservers.conf` on the station:

```
server 192.168.8.144 iburst minpoll 4 maxpoll 6
```

```bash
sudo systemctl restart chrony && chronyc -n sources     # expect ^* 192.168.8.144, stratum 1
```

**VTEC.** In `/etc/hf-timestd/timestd-config.toml`:

```toml
[gnss_vtec]
enabled = true
host = "192.168.8.144"
port = 9000
protocol = "auto"            # or "ubx" / "rtcm3"; auto decides from the first bytes
ephemeris_dir = "data/brdc"  # RTCM only; relative to /var/lib/timestd
```

```bash
sudo systemctl enable --now timestd-vtec
```

An image installed before `timestd-vtec.service` existed lacks the unit; copy
it from the checkout:
`sudo install -m 0644 /opt/git/sigmond/hf-timestd/systemd/timestd-vtec.service /etc/systemd/system/ && sudo systemctl daemon-reload`.

---

## 8. Checking that it works

**What the box sends.** From the station, a 15-second census of the stream:

```bash
python3 - <<'EOF'
import socket, time, collections
s = socket.create_connection(("192.168.8.144", 9000), timeout=5); s.settimeout(5)
buf = b""; t = time.time()
while time.time() - t < 15:
    try: d = s.recv(8192)
    except socket.timeout: continue
    if not d: break
    buf += d
rtcm = collections.Counter(); i = 0
while (i := buf.find(b"\xd3", i)) >= 0 and i + 6 <= len(buf):
    n = ((buf[i+1] & 3) << 8) | buf[i+2]
    if i + 6 + n > len(buf): break
    rtcm[(buf[i+3] << 4) | (buf[i+4] >> 4)] += 1; i += 6 + n
print("bytes:", len(buf), " UBX frames:", buf.count(b"\xb5\x62"), " RTCM3 types:", dict(sorted(rtcm.items())))
EOF
```

`AC0G-ND`'s LG290P gives about 21 KB in 15 s and
`{1005: 16, 1033: 16, 1077: 15, 1087: 15, 1097: 15, 1127: 15}`. Zero bytes on
an open connection means the relay runs but nothing feeds it; "connection
refused" means nothing listens on the port.

**What the service makes of it.** `journalctl -u timestd-vtec -f`. A healthy
RTCM start reads:

```
Receiver speaks RTCM3: observations from MSM/1004, elevations from broadcast ephemeris + RTCM 1005
BRDC: 362 healthy GPS ephemerides in memory
Rx DCB estimated: 1.869 m (6.2 ns), …
VTEC: 14.79 TECU (Sats: 5)
```

after which one `VTEC:` summary appears per minute and
`/var/lib/timestd/data/gnss_vtec.csv` gains a row a second. A u-blox start
reads `Receiver speaks UBX (RXM-RAWX + NAV-SAT)` instead, with no `BRDC` line.

---

## 9. When it does not work

| What you see | What it means | What to do |
|---|---|---|
| `str2str` (or ser2net) restarting; `stty: /dev/ttyACM0: No such file or directory` | the receiver re-enumerated under another number | §4: a udev name keyed on the serial number |
| census: connection refused | nothing listens on the port | check the relay unit; `ss -tln \| grep 9000` on the box |
| census: 0 bytes on an open connection | relay up, receiver silent | receiver unplugged or reset; check `/dev/ttyGNSS`; check the relay owns the port (gpsd, §4) |
| census: RTCM 1004 only | legacy GPS-only output | works (GPS L1/L2), but configure MSM7 + 1005 (§6.1): without 1005 there are no elevations |
| log: `RTCM3 stream yields nothing usable … antenna position NOT YET` | no 1005/1006 in the stream | enable 1005 on the receiver |
| log: `… no ephemeris for G[…]` | the BRDC download failed | the station needs internet (§6.3); look for `BRDC fetch failed` lines |
| log: `0 UBX messages processed`, nothing else | hf-timestd older than `19972d4` reading an RTCM receiver | update hf-timestd |
| `timestd-vtec` killed with `Watchdog timeout`, repeatedly | hf-timestd older than `9ce5252`, with a silent or refusing box | update hf-timestd; it now waits in slices that pet the watchdog |
| `Failed to download DCB file … Earthdata` at start | no NASA Earthdata login for satellite biases | harmless: VTEC runs with satellite biases taken as zero; absolute values carry a few TECU of bias. Optional: [NASA Earthdata setup](https://github.com/HamSCI/hamsci-physics/tree/main/docs) |
| chrony on the box never selects `PPS` | no pulse, or no second numbers | `ppstest /dev/pps0` (§3); keep a `pool` line (§5) |
| chrony on the station shows the box with an error of milliseconds | the box lost PPS and fell back to its own clock | check the box's `chronyc sources` |

⚠ When reading a box's status page, the address of the connected client is the
*station's*. On ND, `192.168.8.200` belongs to the station VM, not the box.

---

## 10. What the numbers are

`timestd-vtec` levels carrier phase to code, per satellite, and maps slant TEC
to vertical with a single-layer ionosphere at 350 km. It estimates the
receiver's own inter-frequency bias once, from the first epoch with four
satellites above 20°, using the floor that true VTEC cannot fall below 2 TECU.
Without NASA's satellite bias file it takes each satellite's bias as zero. The
result tracks the ionosphere's changes well and its absolute level to within a
few TECU — fit for comparing against HF TEC and for watching disturbances, not
for calibrating other instruments.

Code: `scripts/live_vtec.py`, `src/hf_timestd/core/gnss_tec.py` (the analyzer),
`ubx_parser.py`, `rtcm3_adapter.py`, `broadcast_ephemeris.py`, `brdc_fetcher.py`.
Tests: `tests/unit/test_rtcm3_adapter.py` (real ND captures),
`tests/unit/test_live_vtec_watchdog.py`.
