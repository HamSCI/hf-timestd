# Setting up timing on a new station

What every site needs, what the software works out for itself, and what it
still has to be told. Written 2026-09-17 after bringing up DASI-009/AI6VN on
v3.40 and finding four defects that would have hit any station.

The governing principle, and the thread running through every change below:

> **Ask the hardware. Do not ask the operator.**
> An operator can be wrong, can skip a prompt, and cannot answer at all in an
> unattended install. A device that reports its own state cannot.

---

## 1. What the station works out for itself

### The GPSDO, and whether it can serve T5

`gpsdo-monitor` publishes a document per device at `/run/gpsdo/<serial>.json`.
`hf_timestd.core.gpsdo_capability` reads it and decides T5 without asking
anyone:

| device | `pps_study` | verdict |
|---|---|---|
| LBE-1421/1423 | `enabled: true, edges: 60` | **T5** — it can place a second |
| LBE-mini | `enabled: false, edges: 0` | **not T5** — but it names a second, which T6 needs for disambiguation |

A GPSDO always disciplines the ADC — that is the **A axis**, and every
supported model does it. Only some can place a second, the **T axis**. Those
are different jobs and only the second one makes a T5 source.

`enabled` is a *setting*; `edges` is *evidence*. The resolver requires the
evidence, because believing a setting over a measurement is the bug it
replaces: AC0G-ND carried `lb1421_enabled = true` against an LBE-mini for its
whole life, and every surface read "T5 is on" while the probe reported
`no reading yet` forever.

Configuration precedence: **explicit off** always wins; **explicit on is
refused** against a device the probe confirms has no PPS, with a reason that
names the model; **explicit on with no probe** is obeyed, because a missing
daemon is not evidence of missing hardware; **unset follows the probe**.

### The TS-1, and the T6 channel frequency

The injector enumerates as a USB CDC console — `239a:801e Adafruit Trinket
M0`, at `/dev/serial/by-id/usb-Adafruit_Trinket_M0_*`. Read it with
`sudo ts1 probe`:

```
TS1_PRESENT=yes    TS1_FIRMWARE=TimeSync v2.7, Board ID # 9f1e37b5
TS1_MODE=GPS-PPS   TS1_GPS_LOCK=yes   TS1_SATS=13
TS1_REF_HZ=10000000   TS1_TX_HZ=84225000
```

That works before hf-timestd has ever run. `scripts/setup-station.sh` — the
`init` hook declared in `deploy.toml` — probes the injector during bringup and
writes `enabled = true` plus the derived `frequency_hz`. **Verified end to end
on AI6VN, 2026-09-17: the bringup armed T6 with no operator input.**

---

## 2. The arithmetic, from the designer

P. Elliott WB6CXC publishes the frequency plan for TS-1 TimeSync mode:

```
Freq: 84.225 MHz
Alias (fSample  64.800 MHz) : 19.425 MHz
Alias (fSample 129.600 MHz) : 45.375 MHz
```

Fold the TX into the first Nyquist zone — reduce modulo the sample rate
**first**, then reflect:

```
r     = tx mod fs
alias = r  if 2r <= fs  else  fs - r
```

⛔ Ours computed `fs - tx` whenever `tx > fs/2`, which is the second zone
only. At 64.8 Msps that returns **−19,425,000 Hz** — the negation of the
published value, written into the config. The defect sat in three places and
is fixed in all three (`ts1-probe.sh`, `setup-station.sh`,
`core/ts1_channel.py`), with tests that read both shell files.

Only `frequency_hz` depends on the sample rate. `sample_rate = 96000`,
`low_edge_hz = −25000`, `high_edge_hz = +25000`, `consecutive_required = 10`
are constants of the detector.

### Do not retune a TS-1 casually

84.225 MHz was chosen "to both minimize signal re-transmission through the
antenna, and to place any aliased harmonic frequencies as far as possible from
any 'interesting' bands" — no closer than **0.475 MHz**, counting the first
five aliased harmonics. Other frequencies "can be configured to suit other
sample rates", so **read `TS1_TX_HZ`; never assume it**. But changing it is a
band-planning exercise, not a config tweak.

### The injector is designed weak — do not pad it

TimeSync mode emits a low-amplitude signal through an 84 MHz bandpass and
attenuator at **approximately −33 dBm**, then attenuated again by the RX-888's
60 MHz low-pass, since 84.225 MHz sits above that corner. It is filtered twice
before the ADC. A station that adds attenuation to "fix overdrive" is fighting
the design.

---

## 3. Where the sample rate must come from

radiod publishes it: `INPUT_SAMPRATE` (status tag 10), reachable as
`st.frontend.input_samprate`, carried in the front-end block of **every**
channel's status packet.

Prefer it over both alternatives, for reasons that compound:

- **Over a prompt.** `setup-station.sh` asked, then fell back to `129600000`
  unasked. A 64.8 Msps site that skipped the prompt got a channel 25.95 MHz
  from the pilot, which locks never and complains never.
- **Over the config file.** `radiod@<instance>.conf` is ambiguous: AC0G-B4
  carries `samprate = 12000` (channel output) at line 17 and
  `samprate = 129600000` (the RX888) at line 38. A naive parse is off by four
  orders of magnitude. The status stream separates input from output by
  construction.
- **Over anything local, when radiod is remote.** A radiod on another machine
  has no config file here at all, while its status multicast still answers.

`hf_timestd.core.ts1_channel.resolve_t6_frequency_hz` implements the ladder —
**configured > radiod > default** — and returns the origin, so a guess can
never be logged as a measurement.

> ⚠ **Not yet wired.** The module and its tests exist; `setup-station.sh`
> still prompts and defaults. The arithmetic it uses is now correct, so no
> site gets a wrong answer from a *stated* rate — but a site that stays
> silent still gets the 129.6 Msps assumption.

### Remote radiod needs `ttl>0`

Status crossing the LAN does not mean samples will. AC0G-B4 runs `ttl=0`:

```
Radiod reporting TTL=0 for SSRC ...: Multicast data restricted to localhost
loopback only!
```

So you can read the sample rate from a remote radiod and still receive no IQ.
A remote-radiod T6 needs `ttl>0` as a deliberate site decision.

---

## 4. What the station must still be told

**A TS-1 whose USB is not connected.** The injector's working output is the
BPSK addition to the HF path; the USB console only reports its operating
state. The two are separable — one TS-1 can feed several receivers through a
splitter, so a site can have several T6-capable hosts and at most one USB
connection. **T6 capability belongs to the RF path, not to the USB cable.**

So the probe is a *sufficient* detector, never a necessary one. You cannot
infer the absence of a TS-1 from the absence of USB — which is exactly why a
fresh install ships `t6_pps.enabled = false` and why TS-1 presence alone does
not raise the tier.

The interactive path handles this: it asks "Do you have a BPSK PPS injector?"
and takes a frequency. Two gaps remain:

1. **Unattended installs cannot answer.** `--non-interactive` takes the `"n"`
   default, and `auto_or_prompt` honours no `STATION_TS1_*` environment
   variable — the overrides stop at `STATION_RX888_ADC_HZ`.
2. **The answer does not survive a reimage.** `site-profile.toml`, described
   in sigmond as *the single non-secret per-site source of truth*, has no
   field for the injector, though `PROVISIONING-INPUTS.md` already lists
   "BPSK PPS injector frequency" as a per-site input. AI6VN was reimaged twice
   on 2026-09-17 and its T6 configuration had to be re-established both times.

Both are open. The fix has the same shape as everything above: **probe >
declaration > off**, with the declaration living somewhere that survives.

---

## 5. Install hygiene that bites every station

**Re-chown the checkout after any `smd install` of hf-timestd on an image
older than d06cff7.** `scripts/install.sh` used to chown `src`, `scripts`,
`docs` and `pyproject.toml` to `timestd` while leaving `.git` as `sigmond`.
That split is what `sigmond.gitowner` refuses (sigmond#43/#44) and what
`smd doctor` cannot see (sigmond#93) — the component then **silently declines
every update**.

```bash
sudo chown -R sigmond:sigmond /opt/git/sigmond/hf-timestd   # FIRST
sudo -u sigmond git -C /opt/git/sigmond/hf-timestd pull --ff-only
```

The chown must come first: `gitowner` refuses to pull across a split tree, and
does so quietly. The chown bought nothing in the first place — `timestd`
belongs to group `sigmond` and the tree is 2775 setgid, so the service account
already reads the source.

**Components live at `/opt/git/sigmond/<name>.** Not `/opt/git/<name>`.
`ls /opt/git/` returns just `sigmond`, which reads as an empty install and is
the parent directory.

**A lost reach is a registry lookup, not a port scan.** RAC ports derive from
a name hash, so a remembered port goes stale:

```bash
curl -s -u admin:admin http://10.3.2.1:7500/api/proxy/tcp            # hamsci-vpn
fleet-ssh wd30 'ssh gw2 "curl -s -u admin:admin http://127.0.0.1:7501/api/proxy/tcp"'
```

`lastStartTime` dates the last registration, which is how you tell whether a
repair landed. Each gateway hashes into its own band, so a port outside the
fleet's usual range is not evidence of a stale allocation.

---

## 6. Diagnostic cautions earned the hard way

**Per-interval ppm from radiod is estimator noise, not a rate measurement.**
AI6VN read −12.7 ppm twice in a row, agreeing to 0.14 ppm, which looked like a
free-running converter. Eighteen readings over 25 minutes: mean **−0.51 ppm**,
span 104.8. Two consecutive samples of a noisy series prove nothing. AC0G-B4
swings −25.4 to +18.3 ppm and runs T6 perfectly.

**Check the magnitude before adopting a mechanism.** A 12.7 ppm rate error
deviates a one-second period by 12.7 µs. The observed `edge_period` deviations
were 1.9 ms to 330 ms — four orders of magnitude larger. The hypothesis was
dead on arithmetic alone.

**C/N0 on an injector-only station is not a comparable number.** It failed to
track two independent 20 dB changes and reads 84–94 dB-Hz, implausible for any
receiver. Do not steer by it, and do not target another station's value.

**Characterise before you repair.** Chowning AI6VN's hf-timestd before
recording its state destroyed the direct evidence; the diagnosis survived only
because the other 24 components were untouched.

**A test nobody watched fail is not a test.** Two guards written this session
passed against the very defects they existed to catch — one scanned physical
lines while the defect spanned five via continuations, the other asserted a
substring that appeared elsewhere in the file. Mutation-testing found both.
