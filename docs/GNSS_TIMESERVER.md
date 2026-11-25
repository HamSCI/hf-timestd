# A LAN GNSS time server

One small box on the station's LAN keeps time for the whole network as a
stratum-1 NTP server. Its GNSS receiver supplies the PPS that disciplines it.

The box is optional. A station keeps time from the internet without it. With
it, the host clock settles to within microseconds instead of milliseconds.

The same receiver can also stream its raw measurements for the ionosphere
(VTEC). hf-timestd no longer reads that stream. `hamsci-physics` does; see §6.

This guide replaces `ZED_F9P_TEC_CONFIGURATION.md`, `GPS_TEC_OPTIONAL.md` and §4
of `STATION_SETUP_GUIDE.md`. The first two now live in `archive/`.

---

## 1. Two boxes that run today

| | AC0G home | AC0G-ND, Fargo |
|---|---|---|
| Box | `ScreenPI4`, Raspberry Pi 4, Debian 12 | `AC0G-ND-TIME`, Raspberry Pi 4, Debian 13 |
| LAN address | 192.168.1.80 | 192.168.8.144 |
| Receiver | u-blox ZED-F9P | Quectel LG290P |
| Raw stream for VTEC | UBX on TCP port 9000 | RTCM 3.3 on TCP port 9000 |
| chrony on PPS | ±224 ns | ±218 ns (±4.5 µs while settling) |
| Station it serves | AC0G-B4 | AC0G-ND |

---

## 2. Hardware

- **A small Linux computer** with a GPIO header. A Raspberry Pi 4 does it.
- **A GNSS receiver with a PPS output.** For VTEC as well, it must be dual-frequency:
  the ZED-F9P (L1/L2) and the LG290P (L1/L2/L5/E6) both qualify.
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

Both boxes carry these rules since 2026-10-04: `90-lg290p-ttygnss.rules` on
`AC0G-ND-TIME`, `90-gnss-receiver.rules` on `ScreenPI4`, each with a
`stable-device.conf` drop-in for its relay unit.

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
better (`ScreenPI4`: `+/- 224ns`; `AC0G-ND-TIME`: `+/- 4511ns` in its first
hours, `+/- 218ns` once settled). If the box must keep time without
internet, chrony needs the seconds from the receiver itself — gpsd's shared
memory driver (`refclock SHM 0`) does that, at the cost of gpsd owning the
serial port (§4).

---

## 6. The raw stream and VTEC

The same receiver can feed VTEC. The relay that puts its bytes on TCP port 9000
(`str2str` or `ser2net`), the receiver's message settings, the RTCM orbit
download and the checks that the stream works belong to `hamsci-physics`, which
runs the VTEC service. Read them there:
`hamsci-physics/docs/` and `/etc/hamsci-physics/gnss-vtec.toml`.

hf-timestd reads no GNSS stream. A `[gnss_vtec]` section in
`timestd-config.toml` no longer does anything; `hf-timestd validate` flags it.

---

## 7. The station side

Point chrony at the box. `/etc/chrony/conf.d/local-timeservers.conf` on the
station:

```
server 192.168.8.144 iburst minpoll 4 maxpoll 6
```

```bash
sudo systemctl restart chrony && chronyc -n sources     # expect ^* 192.168.8.144, stratum 1
```

Record the file as the station's site delta (`ops/site-deltas/<site>/`) so a
reflash restores it. hf-timestd's own install adds no GNSS box to chrony.

---

## 8. When it does not work

| What you see | What it means | What to do |
|---|---|---|
| the relay restarting; `stty: /dev/ttyACM0: No such file or directory` | the receiver re-enumerated under another number | §4: a udev name keyed on the serial number |
| chrony on the box never selects `PPS` | no pulse, or no second numbers | `ppstest /dev/pps0` (§3); keep a `pool` line (§5) |
| chrony on the station shows the box with an error of milliseconds | the box lost PPS and fell back to its own clock | check the box's `chronyc sources` |
