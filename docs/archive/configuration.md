# Configuration Reference

`hf-timestd` is configured using a TOML file.

Default location:

- `config/timestd-config.toml`

Template:

- `config/timestd-config.toml.template`

---

## `[station]`

Station identity and location.

---

## `[ka9q]`

ka9q-radio (`radiod`) integration and discovery.

---

## `[recorder]`

Recorder configuration:

- `mode`: `test` or `production`
- `test_data_root` / `production_data_root`
- per-minute binary archive written under `raw_buffer/`

---

## `[[recorder.channels]]`

List of time standard channels to record.

Examples:

- `WWV 2.5 MHz`, `WWV 5 MHz`, `WWV 10 MHz`, `WWV 15 MHz`, `WWV 20 MHz`, `WWV 25 MHz`
- `CHU 3.33 MHz`, `CHU 7.85 MHz`, `CHU 14.67 MHz`

---

## `[uploader]`

Uploader configuration exists for optional post-processing workflows, but `hf-timestd` does not generate Digital RF outputs.

If you need PSWS/GRAPE uploads, use `grape-recorder`.
