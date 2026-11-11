# Archived I/Q in half precision (F16) on the station, F32 everywhere else

Michael Hauan, with Claude, 2026-11-11.  Evidence: `~/hamsci/tasks/f16-archive/README.md` on the
devbox (the WB6CXC study), copied in short below.

## Why

Station disk runs out first.  On 2026-11-11 four station VMs paused when their thin pools filled, and
K3LR's root store sits at 82 %, 177 GiB of it hf-timestd's raw I/Q.  An event worth keeping (a flare,
an eclipse, a storm) asks for more days of I/Q than a station can hold today.

hf-timestd stores the six WWV/WWVH channels as 24 kHz complex64 (F32 I and Q), zstd-compressed.  zstd
barely shrinks them: noise does not compress, so a file holds 87-93 % of its raw size.  Half-precision
floats (IEEE binary16, "F16") with zstd hold 39-46 %.  The same disk then keeps about 2.1 times the
days.

Michael's framing: hf-timestd measures and annotates timing in real time, from the live F32 stream.
The archive serves post-analysis, which looks at Doppler, phase and fine spectral detail.  So F16 must
keep everything those analyses can see, and it need not serve timing at all.

## What the study found (WB6CXC, 2026-11-11)

- The stored samples sit far below F16's normal range: RMS −73 to −112 dB re 1.0, peaks at or below
  −64 dB.  F16 holds values below 6.1×10⁻⁵ (−84 dB) with fewer bits, so a plain cast keeps only
  43-73 dB of signal-to-rounding ratio, worst on 20 and 25 MHz.
- A fixed scale of 2¹² before the cast fixes that on every channel: 73.5-73.8 dB.  Any power of two
  from 2⁸ to 2²⁴ gives the same.  At 2¹² a sample would have to reach +24 dB re 1.0 to overflow.
- With that scale, the rounding error's spectrum lies 46-77 dB under the receiver's own noise floor
  across the passband, carrier ±20 Hz included, and runs flat (no spurs).  Integration lowers error
  and noise together, so no integration time digs out a feature F16 lost.  The 10 Hz carrier
  (GRAPE style) keeps phase within 0.002-0.05 mrad rms and Doppler within 0.05 mHz rms.
- hf-timestd's own MetrologyEngine, run on two hours of F32 and of F16, gave tick measurements that
  agree to 0.02-0.23 µs (median) with no bias.

Not yet tested: a strong-signal station.  The trial below covers that.

## Goals

1. hf-timestd writes its raw I/Q archive in F16, scaled by 2¹², behind a per-station setting.
2. Every on-station reader of that archive gets F32 back, unchanged in meaning, whatever the file holds.
3. Everything that leaves the station leaves in F32: the Digital RF that goes to PSWS and the other
   repositories, and meteor-scatter's WAVs for MSQP and Zenodo.  Downloaders see no change.
4. meteor-scatter's kept slots move to F16 the same way.
5. A trial at B4 and ND proves goal 1 at strong-signal stations before F16 becomes the default.

## Non-goals

- radiod's stream, and every live measurement.  Both stay F32.  F16 lives on disk only.
- The T6 pilot channel.  hf-timestd never stores it.
- Converting files already on disk.  Michael: let them age out under the usual trimming.
- Changing the sample rate.  The archive stays at 24 kHz.

## Anti-goals

These would count as failures even if the code worked:

- A downloaded file that a standard Digital RF or WAV tool cannot open, or that differs in shape from
  today's.
- An F16 file without its scale recorded beside it, so that a reader returns values 2¹² too large.
- A reader anywhere on the station that assumes complex64 and silently misreads an F16 file.
- Any change to what hf-timestd measures in real time.

## Constraints

- Digital RF 2.6.14 refuses float16 ("Failed to find datatype"), and WAV has no 16-bit float type.
  So F16 stays inside our own on-station formats, and every export converts to F32.
- Old F32 files and new F16 files share the archive for days, through the changeover and on any
  station that has not switched.  Readers must take both.
- Station configs change only through sigmond (`smd config`), never by hand.
- Rob co-develops hf-timestd and meteor-scatter.  He reviews this design before anyone builds it.

## How it works

### The file

hf-timestd's archive keeps its layout: `raw_buffer/<CHANNEL>/<YYYYMMDD>/<start>.bin.zst` beside
`<start>.json`.  An F16 file holds interleaved I, Q pairs as little-endian binary16, each sample
multiplied by 2¹² before the cast.  The sidecar already carries `"dtype": "complex64"`; an F16 file
says `"dtype": "complex-f16"` (name left to the builder) and adds `"scale": 4096`.  A sidecar without
a scale means 1.

### One reader

`BinaryArchiveReader` becomes the single place that turns a file into samples.  It reads the
sidecar's dtype and scale, and returns complex64 divided by the scale.  Every module that reads
`raw_buffer` today goes through it, or gets changed to: `events/window_drf.py`, `tiered_storage.py`,
`t6_anomaly_capture.py`, `timing_validation_service.py`, `quota_manager.py`,
`resource_guardian.py`, `metrology_service.py` and anything else a search for `raw_buffer`,
`.bin.zst` or `complex64` turns up.  Size estimates that assume 8 bytes a sample read the sidecar too.

The archive has readers outside hf-timestd as well, and each must take F16 files before any station
switches:

- hamsci-physics, which runs GRAPE's daily pipeline from the same `/var/lib/timestd` root:
  `grape/raw_reader.py`, `grape/spool.py`, and the decimation code behind them.  GRAPE's results go to
  PSWS, so this forms a second path off the station.  Its 10 Hz decimated buffer stays complex64; that
  product holds 600 samples a minute and costs little.
- sigmond: `gap_hourly.py`, `commands/check.py`, `discovery/local_resources.py`.
- station-web: `services/grape_service.py`.

Whether these import hf-timestd's reader or carry a small copy of it follows each repo's existing
dependencies.  Either way, one test fixture (an F16 file and its sidecar) runs through all of them.

### The setting

A per-station setting in hf-timestd's config chooses F32 or F16 for the archive.  It defaults to F32
in the release that ships this.  sigmond sets it (`smd config`).  The scale stays a constant in code,
not a setting: one value works on every channel, and a setting would invite a wrong one.

### Leaving the station

`events/window_drf.py` already converts archive windows to Digital RF in complex64.  Through the one
reader it gets F32 from F16 files with no other change.  Its Digital RF metadata records that the
station stored this window in F16 at scale 2¹², so a scientist can see what happened to the data.

### meteor-scatter

Today a decoded slot keeps its two-channel F32 I/Q WAV as `<stem>.wav`.  Under F16 it keeps
`<stem>.iq16.zst`: interleaved F16 I/Q at the same 2¹² scale, zstd-compressed, with dtype and scale in
the slot's existing sidecar.  `msqp package` and `msqp wav` read through
one reader and write 32-bit float WAVs, as now.  The same per-station setting pattern applies, with the
same trial.

## The trial

1. The release that carries this ships F16 off by default.
2. sigmond switches B4 and ND to F16.  Both run T6 and hear strong signals.
3. For at least three days, a check compares their F16 files with the F32 stream, by the study's
   fine-detail test: the rounding error's spectrum against the noise floor, and the 10 Hz carrier's
   phase and Doppler.  It also confirms that `window_drf.py` exports open in standard Digital RF tools.
4. If the error stays at least 40 dB under the noise floor everywhere, and the exports open, the next
   release makes F16 the default.  meteor-scatter follows the same steps.

## What success looks like

- B4 and ND pass the trial.
- The storage ledger shows hf-timestd's archive holding about twice the days for the same space.
- Downloaded Digital RF and MSQP WAVs open and read exactly as they do today.

## Calls made while writing (Claude's, for Michael and Rob to check)

A reviewer read the first draft as a builder would.  These settle what it asked:

- **Where the cast happens.**  The hot buffer in RAM (`/dev/shm`) stays F32; every live reader keeps
  reading F32.  The cast to F16 happens when a chunk goes to disk, at the existing compression step.
  No half-converted file ever reaches the cold tier: a chunk lands whole, in one dtype, with its sidecar.
- **Which variants.**  Only `.bin.zst` files carry F16.  The uncompressed `.bin` (memmap) and `.lz4`
  variants stay complex64, so the zero-copy path keeps working where it exists.
- **The reader's contract may change.**  `BinaryArchiveReader` may hand back the sidecar with the
  samples, and should read its sample rate from the sidecar rather than a constant (the code
  holds 20000; the archive runs at 24000).  The module list above names candidates; the builder
  confirms the true byte-decoding call sites by search, and the shared fixture covers each one.
- **Overflow and non-finite values.**  If any sample in a chunk would exceed F16's range after
  scaling, that whole chunk stays F32 (its sidecar says so) and the writer logs one warning.  Nothing
  clips, and nothing becomes inf by rounding.  NaN and inf already in the stream pass through
  unchanged, since F16 holds both.
- **Size accounting.**  The quota manager, resource guardian and storage ledger count bytes actually
  on disk.  Where they must predict, they use the configured dtype's size (4 bytes a sample for F16,
  8 for F32).  During the changeover, measured bytes tell the truth about mixed days.
- **Sigmond's readers** read sidecars only.  The new fields arrive as additions, so their keys stay
  valid.  The fixture still runs through them.
- **The T6 anomaly capture** (`t6_anomaly_capture.py`, its own 96 kHz event store) stays F32.  It
  stores rare, short events, and this change leaves it alone.
- **meteor-scatter.**  An F16 slot keeps `<stem>.iq16.zst` beside its existing JSON sidecar, which
  gains dtype and scale.  The choice runs per station, so old `.wav` slots and new `.iq16.zst` slots
  share the slot tree; every glob that counts or lists kept slots (`archive.py` and the msqp tools)
  learns both.  meteor-scatter gets its own small reader module rather than a dependency on hf-timestd.
- **The trial's reference costs no disk.**  At the moment it encodes a chunk, the writer holds both
  the F32 samples and their F16 form.  While the trial runs, it measures the study's fine-detail
  figures right there, on one minute of each chunk, and records them in that chunk's sidecar: the
  rounding error's worst 0.75 Hz bin against the noise floor across the passband, and the 10 Hz
  carrier's phase difference (rms).  The pass test reads those sidecars: the error at least 40 dB
  under the floor in every chunk, and the phase within 1 mrad rms.  (An earlier draft compared the
  RAM ring with the file; the ring holds about 600 s and a chunk lands after its 10 minutes close, so
  that pairing would race.)

## Left to the builder

- The dtype name in the sidecar, and the config key's name and place.
- Tests, including one that writes an F16 file and reads it back through every reader listed above.
