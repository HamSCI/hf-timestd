# Configuration Reference

Signal Recorder is configured using a TOML file. The default location is `config/grape-production.toml`.

## `[station]`

This section contains information about your station.

- `callsign` (string, required): Your amateur radio callsign.
- `grid_square` (string, required): Your 6-character Maidenhead grid square.
- `id` (string, required): Unique identifier for your station (e.g., "AC0G").
- `instrument_id` (string, required): Instrument ID (e.g., "RX888").
- `description` (string, optional): Human-readable station description.

**Example:**
```toml
[station]
callsign = "AC0G"
grid_square = "EM38ww"
id = "AC0G"
instrument_id = "RX888"
description = "GRAPE station with RX888 MkII and ka9q-radio"
```

## `[ka9q]`

This section configures the ka9q-radio receiver integration.

- `status_address` (string, optional, default: "239.251.200.193"): ka9q-radio status multicast address.
- `auto_create_channels` (boolean, optional, default: true): Whether to automatically create channels.

**Example:**
```toml
[ka9q]
status_address = "239.251.200.193"
auto_create_channels = true
```

## `[recorder]`

This section configures the recording process.

- `data_dir` (string, optional): Base directory for recordings (default: "/var/lib/signal-recorder/data").
- `archive_dir` (string, optional): Directory for processed data (default: "/var/lib/signal-recorder/archive").
- `recording_interval` (integer, optional, default: 60): Length of each recording file in seconds.
- `continuous` (boolean, optional, default: true): Whether to record continuously.

**Example:**
```toml
[recorder]
data_dir = "/mnt/grape-data/raw"
archive_dir = "/mnt/grape-data/archive"
recording_interval = 60
continuous = true
```

### `[[recorder.channels]]`

This is a list of tables, where each table defines a channel to record.

- `ssrc` (integer, required): Synchronization Source ID (SSRC) from ka9q-radio (equals frequency in Hz).
- `frequency_hz` (integer, required): Frequency in Hertz.
- `preset` (string, optional, default: "iq"): Recording preset ("iq" for IQ data).
- `sample_rate` (integer, optional, default: 12000): Sample rate in Hz.
- `description` (string, required): Human-readable channel description.
- `enabled` (boolean, optional, default: true): Whether this channel is enabled.
- `processor` (string, optional, default: "grape"): Processor to use for this channel.

**Example:**
```toml
[[recorder.channels]]
ssrc = 10000000
frequency_hz = 10000000
preset = "iq"
sample_rate = 12000
description = "WWV 10 MHz"
enabled = true
processor = "grape"

[[recorder.channels]]
ssrc = 15000000
frequency_hz = 15000000
preset = "iq"
sample_rate = 12000
description = "WWV 15 MHz"
enabled = true
processor = "grape"
```

## `[processor]`

This section contains global processor configuration.

- `enabled` (boolean, optional, default: false): Whether processing is enabled.

**Example:**
```toml
[processor]
enabled = false  # Enable when ready to test processing
```

### `[processor.grape]`

This section configures the GRAPE processor.

- `process_time` (string, optional, default: "00:05"): Time to start processing (UTC).
- `process_timezone` (string, optional, default: "UTC"): Timezone for processing schedule.
- `expected_files_per_day` (integer, optional, default: 1440): Expected number of files per day.
- `max_gap_minutes` (integer, optional, default: 5): Maximum gap in minutes before alerting.
- `repair_gaps` (boolean, optional, default: true): Whether to repair gaps in data.
- `interpolate_max_minutes` (integer, optional, default: 2): Maximum gap size for interpolation.
- `output_sample_rate` (integer, optional, default: 10): Output sample rate in Hz.
- `output_format` (string, optional, default: "digital_rf"): Output format.

**Example:**
```toml
[processor.grape]
process_time = "00:05"
process_timezone = "UTC"
expected_files_per_day = 1440
max_gap_minutes = 5
repair_gaps = true
interpolate_max_minutes = 2
output_sample_rate = 10
output_format = "digital_rf"
```

## `[uploader]`

This section configures the upload process to PSWS.

- `enabled` (boolean, optional, default: false): Whether uploads are enabled.
- `upload_enabled` (boolean, optional, default: false): Alternative enable flag.
- `protocol` (string, optional, default: "rsync"): Upload protocol.
- `upload_time` (string, optional, default: "00:30"): Time to start uploads (UTC).
- `upload_timezone` (string, optional, default: "UTC"): Timezone for upload schedule.
- `max_retries` (integer, optional, default: 5): Maximum number of upload retries.
- `retry_delay_seconds` (integer, optional, default: 300): Delay between retries in seconds.
- `exponential_backoff` (boolean, optional, default: true): Whether to use exponential backoff.
- `queue_dir` (string, optional): Directory for upload queue.
- `max_queue_size_gb` (integer, optional, default: 100): Maximum queue size in GB.

**Example:**
```toml
[uploader]
enabled = false
upload_enabled = false
protocol = "rsync"
upload_time = "00:30"
upload_timezone = "UTC"
max_retries = 5
retry_delay_seconds = 300
exponential_backoff = true
queue_dir = "/var/lib/signal-recorder/upload_queue"
max_queue_size_gb = 100
```

### `[uploader.rsync]`

This section contains rsync-specific configuration for PSWS uploads.

- `host` (string, required): PSWS server hostname.
- `port` (integer, optional, default: 22): SSH port.
- `user` (string, required): PSWS username.
- `ssh_key` (string, required): Path to SSH private key.
- `remote_base_path` (string, required): Base path on remote server.
- `bandwidth_limit` (integer, optional, default: 0): Bandwidth limit in KB/s (0 = unlimited).
- `verify_after_upload` (boolean, optional, default: true): Whether to verify uploads.
- `delete_after_upload` (boolean, optional, default: false): Whether to delete local files after upload.

**Example:**
```toml
[uploader.rsync]
host = "pswsnetwork.eng.ua.edu"
port = 22
user = "your_username"
ssh_key = "/home/user/.ssh/id_rsa_psws"
remote_base_path = "/data/AC0G"
bandwidth_limit = 0
verify_after_upload = true
delete_after_upload = false
```

## `[logging]`

This section configures logging.

- `level` (string, optional, default: "INFO"): Log level (DEBUG, INFO, WARNING, ERROR).
- `console_output` (boolean, optional, default: true): Whether to output to console.

**Example:**
```toml
[logging]
level = "INFO"
console_output = true
```

## `[monitoring]`

This section configures health monitoring.

- `enable_metrics` (boolean, optional, default: false): Whether to enable metrics collection.

**Example:**
```toml
[monitoring]
enable_metrics = false
```

