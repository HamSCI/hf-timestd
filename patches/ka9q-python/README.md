# ka9q-python local patches

Patches that hf-timestd needs against the upstream `ka9q-python` package
until they can be merged upstream. Each is independently applicable.

These live in the venv (`/opt/hf-timestd/venv/lib/python3.11/site-packages/ka9q/`)
and **do not survive `pip install -U ka9q-python`** — reapply after any
upstream package upgrade. Until the corresponding upstream PR is merged,
this directory is the source of truth.

## 0001-bpsk-fractional-sample-interpolation.patch

Adds sub-sample interpolation to `BpskPpsCalibrator.process_samples` in
`pps_calibrator.py`. At an integer-sample edge detection (the existing
90° phase-jump gate), linearly interpolates the zero-crossing of the
in-phase component (after rotating to align the pre-transition phase
with the real axis) between samples `i-1` and `i`. Result is a
sub-sample fractional offset in `[0, 1]` that refines the integer-sample
chain_delay value.

**Why:** the integer-sample algorithm is bounded by ±0.5-sample
quantization (±31 µs at 16 kHz). Interpolation removes that floor;
the new floor is SNR-bounded. For an in-line GPSDO-fed BPSK injector
(>40 dB SNR typical) this delivers sub-microsecond per-sample precision.
Validated on bee1 (AC0G) at 45.375 MHz: 16 ns std dev as observed by
chrony, 170 ns total spread of `chain_delay_ns` values vs single
quantized value before the patch.

**Opt-in via constructor**: `enable_fractional_interpolation=True` (default).
Falls back to integer-sample detection when interpolation is not
possible (edge near batch start, near-zero reference amplitude, no
sign change in rotated I component).

**Reapply**:

```bash
sudo patch /opt/hf-timestd/venv/lib/python3.11/site-packages/ka9q/pps_calibrator.py \
    < patches/ka9q-python/0001-bpsk-fractional-sample-interpolation.patch
sudo systemctl restart timestd-core-recorder
```

**Upstream PR status**: NOT going upstream. The proper resolution is to
**port `BpskPpsCalibrator` entirely out of ka9q-python and into
`hf_timestd.core`**, since ka9q-python is a generic RTP stream container
library and BPSK chain-delay calibration is hf-timestd-specific payload
semantics. This patch directory is a stopgap until the in-tree port
lands. See `project_bpsk_pps_calibrator.md` memory note for the
architectural rationale.
