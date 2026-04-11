"""Unit tests for the Phase 1 ring buffer.

These cover the cross-process ring's invariants — SPSC roundtrip,
wraparound, overrun detection, anchor seqlock — without requiring
radiod or a real recorder process.  Each test creates its own ring
segment with a random channel name so tests can run in parallel.
"""

from __future__ import annotations

import os
import platform
import threading
import time
import uuid

import numpy as np
import pytest

pytest.importorskip("sysv_ipc")

from hf_timestd.core import ring_buffer as rb
from hf_timestd.core.ring_buffer import (
    RingBuffer,
    RingBufferError,
    RingBufferIncompatibleError,
    RingBufferOverrunError,
)
from hf_timestd.core import ring_buffer_reader as rbr
from hf_timestd.core.ring_buffer_reader import (
    RingBufferReader,
    _compute_overrun_margin,
)

GPS_EPOCH_UNIX_CONST = 315964800
BILLION = 1_000_000_000


# ─── helpers ────────────────────────────────────────────────────────────
def _unique_name(prefix: str = "TESTRB") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _fake_samples(n: int, start: int = 0) -> np.ndarray:
    """Deterministic complex64 samples; real = sample index, imag = -index."""
    idx = np.arange(start, start + n, dtype=np.float32)
    return idx + (-1.0j * idx).astype(np.complex64)


@pytest.fixture
def ring():
    name = _unique_name()
    # 1 kHz ring-internal rate + 2 s depth = 2000 samples; small so tests
    # wrap quickly.
    buf = RingBuffer.create(name, sample_rate=1000, ring_seconds=2)
    yield buf
    try:
        buf.destroy()
    except Exception:
        pass


@pytest.fixture
def ring_24k():
    name = _unique_name("RB24K")
    buf = RingBuffer.create(name, sample_rate=24000, ring_seconds=4)
    yield buf
    try:
        buf.destroy()
    except Exception:
        pass


# ─── platform gate ──────────────────────────────────────────────────────
def test_require_x86_passes_on_amd64():
    # This test host is x86-64 (plan gates the whole refactor on it).
    assert platform.machine().lower() in ("x86_64", "amd64")
    rb._require_x86()  # should not raise


def test_require_x86_refuses_aarch64(monkeypatch):
    monkeypatch.setattr(rb.platform, "machine", lambda: "aarch64")
    with pytest.raises(RuntimeError, match="seqlock"):
        rb._require_x86()


# ─── create / destroy ───────────────────────────────────────────────────
def test_create_initializes_header(ring):
    assert ring.channel_name.startswith("TESTRB_")
    assert ring.sample_rate == 1000
    assert ring.ring_size_samples == 2000
    assert ring.ring_seconds == 2
    # Epoch starts at 1 (non-zero sentinel for "anchor valid").
    reader = RingBufferReader.attach(ring.channel_name)
    try:
        assert reader.get_anchor()["epoch"] == 1
        # No samples yet.
        assert reader.write_cursor() == 0
        assert reader.producer_pid() == os.getpid()
    finally:
        reader.close()


def test_create_then_adopt_compatible(ring):
    # Second create() on the same name should adopt the existing segment.
    adopted = RingBuffer.create(
        ring.channel_name, sample_rate=ring.sample_rate, ring_seconds=ring.ring_seconds
    )
    try:
        assert adopted.ring_size_samples == ring.ring_size_samples
    finally:
        # Only destroy once (the producer owns the key); ring fixture cleans up.
        pass


def test_create_recreates_on_shape_mismatch():
    name = _unique_name("RBMISMATCH")
    a = RingBuffer.create(name, sample_rate=1000, ring_seconds=2)
    try:
        a.write_samples(_fake_samples(100), batch_first_rtp=1000)
        # Now create again with a different shape — should destroy & rebuild.
        b = RingBuffer.create(name, sample_rate=1000, ring_seconds=4)
        try:
            assert b.ring_size_samples == 4000
            # The producer that owns the new segment starts from cursor 0.
            assert b.write_cursor() == 0
        finally:
            b.destroy()
    except Exception:
        try:
            a.destroy()
        except Exception:
            pass
        raise


# ─── SPSC roundtrip ─────────────────────────────────────────────────────
def test_spsc_roundtrip_extract_samples(ring):
    reader = RingBufferReader.attach(ring.channel_name)
    try:
        ring.update_anchor(gps_time_ns=1_000_000_000_000, rtp_timesnap=5000)
        batch = _fake_samples(500, start=0)
        ring.write_samples(batch, batch_first_rtp=5000)

        assert reader.write_cursor() == 500
        out, meta = reader.extract_samples(500)
        np.testing.assert_array_equal(out, batch)
        assert meta["n_samples"] == 500
        assert meta["sample_rate"] == 1000
        assert meta["gps_time_ns"] == 1_000_000_000_000
        assert meta["rtp_timesnap"] == 5000
        assert meta["start_rtp_timestamp"] == 5000
    finally:
        reader.close()


def test_multiple_batches_advance_cursor(ring):
    reader = RingBufferReader.attach(ring.channel_name)
    try:
        ring.update_anchor(gps_time_ns=0, rtp_timesnap=0)
        rtp = 0
        for i in range(5):
            batch = _fake_samples(100, start=i * 100)
            ring.write_samples(batch, batch_first_rtp=rtp)
            rtp += 100
        assert reader.write_cursor() == 500
        out, _ = reader.extract_samples(500)
        assert np.all(out.real == np.arange(500, dtype=np.float32))
    finally:
        reader.close()


# ─── wraparound ─────────────────────────────────────────────────────────
def test_wraparound_preserves_recent_samples(ring):
    """Write past ring_size so the head wraps, then read back a small
    recent window to prove the wrap-copy reassembly is correct.
    """
    reader = RingBufferReader.attach(ring.channel_name)
    try:
        ring.update_anchor(gps_time_ns=0, rtp_timesnap=0)
        total = 3000  # ring is 2000 — will wrap once
        chunk = 500
        rtp = 0
        for start in range(0, total, chunk):
            batch = _fake_samples(chunk, start=start)
            ring.write_samples(batch, batch_first_rtp=rtp)
            rtp += chunk
        assert reader.write_cursor() == total
        # Read back a window that fits strictly inside the margin slack.
        # For ring=2000, margin=max(64,min(4096,125))=125.  A read of 400
        # samples ends at w1=3000 with s_start=2600, and
        # (w2-s_start)=400 <= 2000-125=1875 → no overrun.
        margin = _compute_overrun_margin(ring.ring_size_samples)
        count = ring.ring_size_samples - margin - 500
        assert count > 0
        out, _ = reader.extract_samples(count)
        expected = np.arange(
            total - count, total, dtype=np.float32
        )
        np.testing.assert_array_equal(out.real, expected)
    finally:
        reader.close()


def test_wraparound_large_ring(ring_24k):
    """Ring large enough to make extract_samples past the margin succeed."""
    r = ring_24k
    reader = RingBufferReader.attach(r.channel_name)
    try:
        r.update_anchor(gps_time_ns=0, rtp_timesnap=0)
        rs = r.ring_size_samples  # 96000
        batch = 1000
        rtp = 0
        written = 0
        while written < rs + 54000:
            chunk = _fake_samples(batch, start=written)
            r.write_samples(chunk, batch_first_rtp=rtp)
            rtp += batch
            written += batch
        margin = _compute_overrun_margin(rs)
        safe_count = rs - margin - batch
        out, _ = reader.extract_samples(safe_count)
        expected = np.arange(
            written - safe_count, written, dtype=np.float32
        )
        np.testing.assert_array_equal(out.real, expected)
    finally:
        reader.close()


# ─── overrun detection ─────────────────────────────────────────────────
def test_extract_past_window_raises_overrun(ring_24k):
    r = ring_24k
    reader = RingBufferReader.attach(r.channel_name)
    try:
        r.update_anchor(gps_time_ns=0, rtp_timesnap=0)
        rs = r.ring_size_samples
        # Write more than one ring's worth.
        total = rs + rs // 2
        batch = 2000
        rtp = 0
        for start in range(0, total, batch):
            r.write_samples(_fake_samples(batch, start=start), batch_first_rtp=rtp)
            rtp += batch
        # Requesting exactly ring_size samples should fail overrun because
        # we need (ring_size - margin) of headroom.
        with pytest.raises(RingBufferOverrunError):
            reader.extract_samples(rs)
    finally:
        reader.close()


# ─── anchor seqlock ────────────────────────────────────────────────────
def test_update_anchor_bumps_epoch(ring):
    reader = RingBufferReader.attach(ring.channel_name)
    try:
        e0 = reader.get_anchor()["epoch"]
        ring.update_anchor(gps_time_ns=111, rtp_timesnap=222)
        anchor = reader.get_anchor()
        assert anchor["epoch"] == e0 + 1
        assert anchor["gps_time_ns"] == 111
        assert anchor["rtp_timesnap"] == 222
        ring.update_anchor(gps_time_ns=333, rtp_timesnap=444)
        assert reader.get_anchor()["epoch"] == e0 + 2
    finally:
        reader.close()


def test_seqlock_read_under_concurrent_updates(ring):
    """Any anchor read that succeeds must be internally consistent, and at
    least some reads must succeed under concurrent writes.  In production,
    update_anchor is called at most once per radiod restart; this test
    simulates a pathological write rate with a short backoff so the reader
    can still wedge in between updates.
    """
    reader = RingBufferReader.attach(ring.channel_name)
    stop = threading.Event()

    def writer():
        i = 1
        while not stop.is_set():
            ring.update_anchor(
                gps_time_ns=i * BILLION, rtp_timesnap=i & 0xFFFFFFFF
            )
            i += 1
            # Let the reader acquire the GIL in between updates.
            time.sleep(0.0001)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 1.0
        successes = 0
        failures = 0
        while time.monotonic() < deadline:
            try:
                anchor = reader.get_anchor()
            except RingBufferError:
                failures += 1
                continue
            # Pairing invariant: gps_time_ns/BILLION == rtp_timesnap.
            assert anchor["gps_time_ns"] // BILLION == (
                anchor["rtp_timesnap"] & 0xFFFFFFFF
            )
            successes += 1
        assert successes > 0, (
            f"reader never observed a consistent anchor "
            f"(failures={failures})"
        )
    finally:
        stop.set()
        t.join(timeout=1.0)
        reader.close()


# ─── sample ↔ UTC mapping ──────────────────────────────────────────────
def test_head_utc_after_write(ring_24k):
    r = ring_24k
    reader = RingBufferReader.attach(r.channel_name)
    try:
        # Pick a GPS time that maps to a nice round UTC.
        # UTC = 1_800_000_000.0  → gps_ns = (UTC - GPS_EPOCH + LEAP) * 1e9
        leap = reader._leap
        target_utc = 1_800_000_000.0
        gps_ns = int((target_utc - GPS_EPOCH_UNIX_CONST + leap) * BILLION)
        r.update_anchor(gps_time_ns=gps_ns, rtp_timesnap=10_000)
        r.write_samples(_fake_samples(24000), batch_first_rtp=10_000)
        # Head is "one past the last written sample" → UTC + 1s.
        head = reader.head_utc()
        assert head is not None
        assert abs(head - (target_utc + 1.0)) < 1e-6
    finally:
        reader.close()


def test_extract_interval_roundtrip(ring_24k):
    r = ring_24k
    reader = RingBufferReader.attach(r.channel_name)
    try:
        leap = reader._leap
        target_utc = 1_800_000_000.0
        gps_ns = int((target_utc - GPS_EPOCH_UNIX_CONST + leap) * BILLION)
        r.update_anchor(gps_time_ns=gps_ns, rtp_timesnap=10_000)
        # Write 3s of samples, request the middle 1s.
        total = 24000 * 3
        batch = 6000
        rtp = 10_000
        for start in range(0, total, batch):
            r.write_samples(
                _fake_samples(batch, start=start), batch_first_rtp=rtp
            )
            rtp += batch
        samples, meta = reader.extract_interval(
            utc_start=target_utc + 1.0, duration_sec=1.0
        )
        assert len(samples) == 24000
        # Expected real part = indices 24000..47999
        np.testing.assert_array_equal(
            samples.real, np.arange(24000, 48000, dtype=np.float32)
        )
        # Metadata passthrough
        assert meta["gps_time_ns"] == gps_ns
        assert meta["sample_rate"] == 24000
        assert meta["n_samples"] == 24000
        # start_rtp = 10_000 + 24000 = 34_000
        assert meta["start_rtp_timestamp"] == 34_000
    finally:
        reader.close()


def test_extract_interval_feeds_buffer_timing(ring_24k):
    """The metadata dict the reader synthesizes must be consumable by
    buffer_timing.resolve_buffer_timing() unchanged."""
    from hf_timestd.core.buffer_timing import resolve_buffer_timing

    r = ring_24k
    reader = RingBufferReader.attach(r.channel_name)
    try:
        leap = reader._leap
        target_utc = 1_800_000_000.0
        gps_ns = int((target_utc - GPS_EPOCH_UNIX_CONST + leap) * BILLION)
        r.update_anchor(gps_time_ns=gps_ns, rtp_timesnap=10_000)
        r.write_samples(_fake_samples(48000), batch_first_rtp=10_000)
        _, meta = reader.extract_interval(
            utc_start=target_utc + 0.5, duration_sec=1.0
        )
        bt = resolve_buffer_timing(meta, sample_rate=24000)
        assert bt.source == "rtp_gps"
        # sample0_utc should match target_utc + 0.5 exactly.
        assert abs(bt.sample0_utc - (target_utc + 0.5)) < 1e-6
    finally:
        reader.close()
