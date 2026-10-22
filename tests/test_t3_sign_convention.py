"""The sign test: one known host-clock offset, checked at every consumer.

Michael ruled on 2026-10-22 (docs/design/review-fusion-kalman-20261022.md,
section 15, "Rulings on the five ruling-1 questions", ruling 1) that the
sign convention is MEASUREMENT_MODEL.md's own:

    label_utc = rtp_time + rtp_to_utc_offset_ns

The offset is what you ADD to the station's time to get UTC.  So a host
clock that runs AHEAD of UTC by 5.000 ms needs an offset of -5.000 ms.

This module builds that station.  The host clock runs +5.000 ms ahead of
UTC; radiod stamps the ring from the host clock (METROLOGY.md section 4.3),
so every tick arrival lands 5.000 ms late on the ring.  The test feeds
those arrivals to fusion as L1 rows, and then follows the fused D_clock
through every consumer named in the consumer map
(~mjh/hamsci/proofs/d-clock-fused-consumers-20261022.md).

Each assertion states the value the ruled convention expects.  Where the
code disagrees, the test carries a strict xfail whose reason names the
file and line.  The suite stays green, and the disagreement stays loud: a
strict xfail that starts passing fails the suite, so whoever fixes a
consumer must also lift its marker.  This module fixes nothing.

The calibration stays OFF.  ``_update_calibration`` pulls each broadcast's
mean D_clock to zero (multi_broadcast_fusion.py, ``_update_calibration``),
so with it on, every consumer reads about zero and no sign shows.  The
fusion engine here runs with ``auto_calibrate=False`` and an empty data
root, so it neither learns a hardware offset nor loads a saved one.
``test_the_calibration_hides_the_sign`` shows what that bypass prevents.

d_clock, raw and fused, means HOST minus UTC.  The metrology engine
defines it as ``timing_error_ms = raw_arrival_ms - expected_delay_ms``
(metrology_engine.py:1304, and ``(arrival_utc - expected_utc)`` at :1633),
where the arrival is read on the host-stamped ring.  Fusion passes it
through unchanged (``d_clock = raw_toa``, multi_broadcast_fusion.py:2157,
:2163).  So d_clock carries the OPPOSITE sign to rtp_to_utc_offset_ns,
and a consumer that publishes or adds +d_clock inverts the correction.
"""

import ast
import inspect
import json
import logging
import tempfile
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from hf_timestd.core import core_recorder_v2
from hf_timestd.core import multi_broadcast_fusion as mbf
from hf_timestd.core.authority_manager import AuthorityManager
from hf_timestd.core.fusion_status_probe import FusionStatusProbe
from hf_timestd.core.fusion_status_writer import FusionStatusWriter
from hf_timestd.core.multi_broadcast_fusion import (
    MultiBroadcastFusion,
    anchor_sign_line,
)
from hf_timestd.core.offset_judge import (
    ChronyBench,
    FusionBench,
    LabelPlaneAnchorSample,
)

# The station under test.  HOST_AHEAD_MS is host minus UTC.
HOST_AHEAD_MS = 5.000
# What the ruled convention expects any rtp_to_utc offset to read.
EXPECTED_OFFSET_MS = -HOST_AHEAD_MS
EXPECTED_OFFSET_NS = int(round(EXPECTED_OFFSET_MS * 1e6))

# A fixed true UTC "now", and the host clock's reading of it.
TRUE_UTC_NOW = 1_790_000_000.0
HOST_NOW = TRUE_UTC_NOW + HOST_AHEAD_MS / 1e3

# Four broadcasts, two stations, so fusion runs multi-station.
BROADCASTS = [("WWV", 5.0, 9.1), ("WWV", 10.0, 9.4),
              ("WWV", 15.0, 9.8), ("WWVH", 10.0, 24.6)]
MINUTES = 30
# One microsecond: far below the 10 ms an inverted sign would show, and
# above the 0.24 us a float second resolves at a Unix epoch.
TOL_MS = 1e-3


def _l1_row(t_minute, station, freq, prop_delay_ms):
    """One L1 metrology row as the metrology engine writes it.

    The tick leaves the transmitter on the UTC second and arrives
    ``prop_delay_ms`` later in true UTC.  The ring labels it with the host
    clock, 5 ms ahead.  The engine then writes
    ``raw_toa_ms = timing_error_ms = arrival_on_ring - expected_delay``.
    """
    # Kept in milliseconds after the second: at a Unix epoch a float
    # second resolves only about 0.2 us.
    true_arrival_ms = prop_delay_ms
    ring_label_ms = true_arrival_ms + HOST_AHEAD_MS
    raw_toa_ms = ring_label_ms - prop_delay_ms
    return {
        "timestamp_utc": datetime.fromtimestamp(t_minute, timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "station_id": station,
        "frequency_mhz": freq,
        "raw_toa_ms": raw_toa_ms,
        "light_travel_time_ms": prop_delay_ms - 1.5,
        "snr_db": 20.0,
        "quality_flag": "A",
        "channel_name": f"{station}_{freq:g}",
    }


def _run_fusion(auto_calibrate):
    """Drive the real fuse() over 30 minutes of synthetic L1 rows.

    Only the readers are stubbed; ``_read_latest_measurements`` runs as
    shipped, so the L1 row reaches fusion through ``d_clock = raw_toa``.
    """
    logging.disable(logging.WARNING)
    try:
        with tempfile.TemporaryDirectory() as td:
            fu = MultiBroadcastFusion(data_root=Path(td),
                                      auto_calibrate=auto_calibrate)
            fu.physics_model = None
            fu.arrival_matrix = None
            fu._run_global_differential_solve = \
                lambda lookback_minutes=30: (None, 0)
            fu._read_tick_timing_observations = lambda lb: {}
            fu._read_gnss_vtec = lambda: None
            fu._check_upstream_freshness = lambda: (True, 0.0)
            fu._read_l2_physics = lambda lb: {}

            t0 = float(int(time.time() // 60) * 60) - 60.0 * MINUTES
            state = {"n": 0}

            def read_l1(lookback_minutes):
                rows = {}
                for k in range(state["n"]):
                    for s, f, delay in BROADCASTS:
                        t = t0 + 60.0 * k
                        rows[(t, s, f)] = _l1_row(t, s, f, delay)
                return rows

            fu._read_l1_metrology = read_l1
            result = None
            for n in range(1, MINUTES + 1):
                state["n"] = n
                result = fu.fuse(lookback_minutes=MINUTES,
                                 force_l1_only=True, skip_write=True)
            return result
    finally:
        logging.disable(logging.NOTSET)


@pytest.fixture(scope="module")
def fused():
    result = _run_fusion(auto_calibrate=False)
    assert result is not None
    return result


@pytest.fixture
def status_file(fused, tmp_path):
    """fusion_status.json, written by the shipped writer from the result."""
    path = tmp_path / "fusion_status.json"
    FusionStatusWriter(path, cycle_interval_sec=60.0).update(
        fused, chrony_fed=False, skip_reasons=[])
    return path


# ── d_clock itself ──────────────────────────────────────────────────────


def test_raw_d_clock_reads_host_minus_utc(fused):
    """Raw d_clock = host - UTC = +5 ms: the opposite sign to the offset."""
    assert fused.d_clock_raw_ms == pytest.approx(HOST_AHEAD_MS, abs=TOL_MS)
    assert fused.d_clock_raw_ms == pytest.approx(-EXPECTED_OFFSET_MS,
                                                 abs=TOL_MS)


def test_fused_d_clock_reads_host_minus_utc(fused):
    """With the calibration off, fused d_clock = raw = host - UTC."""
    assert fused.d_clock_fused_ms == pytest.approx(HOST_AHEAD_MS, abs=TOL_MS)


def test_the_calibration_hides_the_sign():
    """With the calibration on, fused d_clock reads zero while raw holds
    +5 ms: every consumer below would read about zero, whichever sign it
    applies.  That is why this module turns the calibration off."""
    result = _run_fusion(auto_calibrate=True)
    assert result.d_clock_raw_ms == pytest.approx(HOST_AHEAD_MS, abs=TOL_MS)
    assert abs(result.d_clock_fused_ms) < 0.1


def test_the_status_file_publishes_raw_beside_calibrated(tmp_path):
    """Step 2 (review section 15): with the calibration ON, as on every
    station, fusion_status.json labels the near-zero fused value as
    calibrated and carries the +5 ms the calibration zeroed beside it,
    with a sigma of its own."""
    result = _run_fusion(auto_calibrate=True)
    path = tmp_path / "fusion_status.json"
    FusionStatusWriter(path, cycle_interval_sec=60.0).update(
        result, chrony_fed=False, skip_reasons=[])
    fusion = json.loads(path.read_text())["fusion"]
    assert fusion["d_clock_fused_calibrated"] is True
    assert abs(fusion["d_clock_fused_ms"]) < 0.1
    assert fusion["d_clock_uncalibrated_ms"] == pytest.approx(HOST_AHEAD_MS,
                                                              abs=TOL_MS)
    sigma = fusion["uncalibrated_uncertainty_ms"]
    assert sigma is not None and 0.0 < sigma < 10.0


def test_the_cycle_summary_says_calibrated(fused):
    """The journal's per-cycle line names the fused value as calibrated
    and prints the uncalibrated one beside it."""
    line = mbf.cycle_summary_line(fused)
    assert line.startswith("Fused D_clock (calibrated): ")
    assert f"uncalibrated {HOST_AHEAD_MS:+.3f} ± " in line


def test_the_status_file_carries_d_clock_unchanged(fused, status_file):
    """The seam every reader below shares (fusion_status_writer.py:155)."""
    fusion = json.loads(status_file.read_text())["fusion"]
    assert fusion["available"] is True
    assert fusion["n_stations"] >= 2
    assert fusion["kalman_state"] in ("ACQUIRING", "LOCKED")
    assert fusion["d_clock_fused_ms"] == pytest.approx(HOST_AHEAD_MS,
                                                       abs=TOL_MS)


# ── the Offset Judge ────────────────────────────────────────────────────


def test_chrony_bench_control_reads_true_utc():
    """Control.  The judge's utc means TRUE UTC (BenchReading docstring),
    and ChronyBench honours it: a host 5 ms fast reads utc = wall - 5 ms.
    FusionBench must agree with it on the same station."""
    out = textwrap.dedent(f"""\
        Reference ID    : C0A800CB (192.168.0.203)
        Stratum         : 2
        Ref time (UTC)  : Thu Aug 05 12:00:00 2026
        System time     : {HOST_AHEAD_MS / 1e3:.9f} seconds fast of NTP time
        Last offset     : +0.000000500 seconds
        RMS offset      : 0.000002000 seconds
        Frequency       : 1.000 ppm slow
        Residual freq   : +0.001 ppm
        Skew            : 0.010 ppm
        Root delay      : 0.000100000 seconds
        Root dispersion : 0.000050000 seconds
        Update interval : 16.0 seconds
        Leap status     : Normal
        """)

    def runner(args, **kw):
        stdout = out if args[1] == "tracking" else ""
        return SimpleNamespace(returncode=0, stdout=stdout)

    bench = ChronyBench(runner=runner, time_fn=lambda: HOST_NOW,
                        mono_fn=lambda: 0.0)
    reading = bench.poll()
    assert (reading.utc - TRUE_UTC_NOW) * 1e3 == pytest.approx(0.0, abs=TOL_MS)


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="INVERTED: offset_judge.py:623 FusionBench writes "
           "utc = wall + d_clock; d_clock is host - UTC, so true UTC is "
           "wall - d_clock.  It reads wall + 5 ms, 10 ms from ChronyBench.")
def test_fusion_bench_reads_true_utc(status_file):
    pub = json.loads(status_file.read_text())["utc_published"]
    pub_s = datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
    # The bench's own freshness gate reads the wall clock against
    # utc_published; place "now" just after the write.
    wall = pub_s + 1.0
    bench = FusionBench(status_path=status_file, time_fn=lambda: wall,
                        mono_fn=lambda: 0.0)
    reading = bench.poll()
    assert reading is not None and reading.tier == "T3"
    true_utc = wall - HOST_AHEAD_MS / 1e3
    assert (reading.utc - true_utc) * 1e3 == pytest.approx(0.0, abs=TOL_MS)


# ── the authority manager's T3 rung ─────────────────────────────────────


def _probe(status_file):
    return FusionStatusProbe(status_path=status_file, freshness_sec=3600.0)


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="INVERTED: fusion_status_probe.py:110 hands d_clock_fused_ms "
           "(host - UTC) to the manager as offset_ms unchanged; the rung "
           "publishes it as rtp_to_utc_offset (UTC - host).  Reads +5 ms.")
def test_t3_probe_offset_follows_the_convention(status_file):
    result = _probe(status_file).poll()
    assert result.available, result.reason
    assert result.offset_ms == pytest.approx(EXPECTED_OFFSET_MS, abs=TOL_MS)


def _publish_authority(status_file, tmp_path):
    out = tmp_path / "authority.json"
    mgr = AuthorityManager(probes=[_probe(status_file)], output_path=out,
                           a_level_provider=lambda: "A1",
                           upgrade_hysteresis=1)
    state = mgr.tick()
    assert state.t_level_active == "T3"
    return out, state


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="INVERTED: authority_manager.py:827 publishes T3's offset_ms "
           "as rtp_to_utc_offset_ns unchanged, so +d_clock reaches every "
           "client (row 11 of the consumer map).  Reads +5000000 ns.")
def test_authority_publishes_the_ruled_offset_on_t3(status_file, tmp_path):
    out, state = _publish_authority(status_file, tmp_path)
    assert state.rtp_to_utc_offset_ns == EXPECTED_OFFSET_NS
    assert json.loads(out.read_text())["rtp_to_utc_offset_ns"] \
        == EXPECTED_OFFSET_NS


# ── the client: hamsci_dsp.timing.acquire_anchor_utc ────────────────────


def _host_stamped_rtp_to_utc(first_rtp, channel_info, wallclock_hint_sec):
    """radiod's GPS_TIME is the host clock (METROLOGY.md section 4.3), so
    the radiod pair maps a sample taken at true UTC T to T + 5 ms."""
    return TRUE_UTC_NOW + HOST_AHEAD_MS / 1e3


def test_client_anchor_control_follows_the_convention(tmp_path):
    """Control.  acquire_anchor_utc ADDS rtp_to_utc_offset_ns
    (hamsci_dsp/timing.py:443), as the ruled convention says.  Hand it the
    offset the convention prescribes and the anchor lands on true UTC."""
    from hamsci_dsp.timing import AuthorityReader, acquire_anchor_utc

    path = tmp_path / "authority.json"
    path.write_text(json.dumps({
        "schema": "v1",
        "utc_published": datetime.now(timezone.utc).isoformat()
        .replace("+00:00", "Z"),
        "a_level": "A1", "t_level_active": "T3",
        "rtp_to_utc_offset_ns": EXPECTED_OFFSET_NS, "sigma_ns": 1_000_000,
    }))
    anchor = acquire_anchor_utc(
        first_rtp=1234, channel_info=object(),
        rtp_to_utc=_host_stamped_rtp_to_utc,
        authority_reader=AuthorityReader(path=path))
    assert anchor.source == "rtp_to_utc+authority"
    assert (anchor.utc - TRUE_UTC_NOW) * 1e3 == pytest.approx(0.0, abs=TOL_MS)


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="INVERTED upstream: the client adds the offset correctly "
           "(hamsci_dsp/timing.py:443), but authority_manager.py:827 and "
           "fusion_status_probe.py:110 publish +d_clock, so the anchor "
           "lands 10 ms off true UTC (+5 ms host error, doubled).")
def test_client_anchor_from_fusion_lands_on_true_utc(status_file, tmp_path):
    from hamsci_dsp.timing import AuthorityReader, acquire_anchor_utc

    out, _ = _publish_authority(status_file, tmp_path)
    anchor = acquire_anchor_utc(
        first_rtp=1234, channel_info=object(),
        rtp_to_utc=_host_stamped_rtp_to_utc,
        authority_reader=AuthorityReader(path=out))
    assert anchor.source == "rtp_to_utc+authority"
    assert (anchor.utc - TRUE_UTC_NOW) * 1e3 == pytest.approx(0.0, abs=TOL_MS)


# ── chrony's FUSE feed: retired ─────────────────────────────────────

# Step 4 of the ruled order (review section 15) retired the FUSE refclock:
# fusion writes no chrony SHM segment.  The feed wrote
# ``reference = system - d_clock``, which AGREED with the convention: true
# UTC, with chrony's reference - system reading -5 ms on this station.
# Its sign was never the problem; the calibration that zeroes d_clock was.
# tests/test_fuse_retired.py holds the retirement.


# ── T6 disambiguation, the T3 branch: deleted ───────────────────────

# Step 3 of the ruled order (review section 15) deleted
# CoreRecorderV2._get_disambiguation_reference.  It had no caller since
# e003e68 (2026-09-19).  Its T3 branch returned d_clock_fused_ms (host -
# UTC, and calibrated to zero) as a reference offset: +5 ms on this
# station where the ruled convention reads -5 ms.  A strict xfail marked
# that here until the deletion.  The test below keeps its intent: the
# core recorder must not read the calibrated fused value at all, so no
# T6 path can take it for a reference again.


def test_core_recorder_reads_no_fused_d_clock():
    """No T6 path in the core recorder reads fusion's calibrated value."""
    assert not hasattr(core_recorder_v2.CoreRecorderV2,
                       "_get_disambiguation_reference")
    tree = ast.parse(inspect.getsource(core_recorder_v2))
    reads = [node for node in ast.walk(tree)
             if isinstance(node, ast.Constant)
             and node.value == "d_clock_fused_ms"]
    assert reads == [], "core_recorder_v2 reads d_clock_fused_ms again"


# ── the anchor-direct sign line ─────────────────────────────────────────


def test_anchor_sign_line_prints_one_convention(fused):
    """multi_broadcast_fusion.py:4534.  The line prints reference - system,
    +ve = host slow, which IS the ruled convention (UTC - host).  A host
    5 ms fast prints -5.0 on the anchor and NTP terms, and the ring plane
    (host-stamped, corrected by d_clock) agrees with a registration on
    true UTC.  AGREES."""
    anchor = LabelPlaneAnchorSample(
        bench="hf_acquired", tier="T3", reference_time=TRUE_UTC_NOW,
        system_time=HOST_NOW, sigma_ns=1e6, age_s=0.0)
    assert anchor.offset_s * 1e3 == pytest.approx(EXPECTED_OFFSET_MS,
                                                  abs=TOL_MS)
    line = anchor_sign_line(
        anchor_reference_minus_system_ms=anchor.offset_s * 1e3,
        # chrony's sources field: system - reference, +ve = host fast.
        pool_median_system_minus_reference_ms=HOST_AHEAD_MS,
        d_clock_fused_ms=fused.d_clock_fused_ms,
    )
    assert line.endswith(
        "anchor -5.0 ms | NTP pool -5.0 ms | ring−registration +0.0 ms "
        "(d_clock calibrated)"), line
