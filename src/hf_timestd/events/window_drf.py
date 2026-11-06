"""Cut a time window out of the raw IQ buffer and write it as Digital RF.

Layout read:   <raw_root>/<CHANNEL>/<YYYYMMDD>/<start_unix>.bin.zst + .json
Layout written:
    out_dir/<CHANNEL>/            Digital RF (complex64, one subchannel)
    out_dir/<CHANNEL>/metadata/   Digital Metadata, one record per buffer file
    out_dir/order.json            the order, plus "missing": {channel: [start_unix]}
    out_dir/SHA256SUMS            sha256sum format, every other file, sorted

Each buffer file lands at sample index start_unix * sample_rate.  A file that
is absent, or short of its expected samples, leaves a gap; nothing fills it.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

FILE_SEC = 600
DEFAULT_RATE = 24000


def _epoch(t: datetime) -> float:
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.timestamp()


def _bin_path(raw_root: Path, channel: str, start: int) -> Path:
    day = datetime.fromtimestamp(start, timezone.utc).strftime("%Y%m%d")
    return Path(raw_root) / channel / day / f"{start}.bin.zst"


def window_files(raw_root, channel, start, end):
    """Return (existing, missing) file start times overlapping [start, end)."""
    t0, t1 = _epoch(start), _epoch(end)
    first = int(t0 // FILE_SEC) * FILE_SEC
    found, missing = [], []
    s = first
    while s < t1:
        (found if _bin_path(raw_root, channel, s).exists() else missing).append(s)
        s += FILE_SEC
    return found, missing


def _parse_utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _write_channel(raw_root, channel, order, found, missing, out_dir):
    import digital_rf as drf
    from ..core.binary_archive_writer import BinaryArchiveReader

    reader = BinaryArchiveReader(Path(raw_root), channel)
    loaded = []
    for s in found:
        samples = reader.read_minute(s)
        side = reader.read_metadata(s)
        if samples is None:
            missing.append(s)
            continue
        loaded.append((s, samples, side or {}))
    missing.sort()
    if not loaded:
        return 0, 0
    rate = int(loaded[0][2].get("sample_rate") or DEFAULT_RATE)
    first_idx = loaded[0][0] * rate
    chan_dir = Path(out_dir) / channel
    md_dir = chan_dir / "metadata"
    md_dir.mkdir(parents=True, exist_ok=True)

    writer = drf.DigitalRFWriter(
        str(chan_dir), np.complex64, 3600, 60000, first_idx,
        rate, 1, uuid_str=channel, compression_level=0, checksum=False,
        is_complex=True, num_subchannels=1, is_continuous=False,
        marching_periods=False)
    md_writer = drf.DigitalMetadataWriter(
        str(md_dir), 3600, 3600, rate, 1, "metadata")
    order_json = json.dumps(order, sort_keys=True)
    missing_json = json.dumps(missing)
    nbytes = 0
    try:
        for s, samples, side in loaded:
            n = len(samples)
            if side.get("samples_written") is not None:
                n = min(n, int(side["samples_written"]))
            idx = s * int(side.get("sample_rate") or rate)
            writer.rf_write(np.ascontiguousarray(samples[:n], dtype=np.complex64),
                            next_sample=idx - first_idx)  # relative to start_global_index
            nbytes += n * 8
            md_writer.write(idx, {
                "event_id": str(order.get("event_id", "")),
                "channel": channel,
                "start_unix": np.int64(s),
                "sidecar_json": json.dumps(side, sort_keys=True),
                "order_json": order_json,
                "missing_json": missing_json,
            })
    finally:
        writer.close()
    return len(loaded), nbytes


def _write_sums(out_dir: Path):
    lines = []
    for p in sorted(out_dir.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS":
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            lines.append((str(p.relative_to(out_dir)), h.hexdigest()))
    lines.sort()
    (out_dir / "SHA256SUMS").write_text(
        "".join(f"{h}  {rel}\n" for rel, h in lines))


def write_window_drf(raw_root, channels, order, out_dir):
    """Write the order's window for each channel; return a summary dict."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    start, end = _parse_utc(order["start_utc"]), _parse_utc(order["end_utc"])
    result = {"channels": {}, "bytes": 0}
    missing_by = {}
    for ch in channels:
        found, missing = window_files(raw_root, ch, start, end)
        n, nbytes = _write_channel(raw_root, ch, order, found, missing, out_dir)
        missing_by[ch] = list(missing)
        result["channels"][ch] = {"files": n, "missing": list(missing)}
        result["bytes"] += nbytes
    full = dict(order)
    full["channels"] = list(channels)
    full["missing"] = missing_by
    (out_dir / "order.json").write_text(json.dumps(full, indent=2, sort_keys=True))
    _write_sums(out_dir)
    return result
