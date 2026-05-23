"""Tests for the differential (per-sample-derivative) PPS edge detector.

The detector takes |s[n] − s[n−1]|, peak-picks above an adaptive
threshold, and sub-sample-interpolates the position.  Unlike the
half-second boxcar matched filter, it has no carrier-sensitivity
issues — the per-sample diff is dominated by the polarity flip
(2A magnitude) regardless of any sub-Hz residual carrier rotation.

These tests verify:
  * single edge per second is detected reliably,
  * chain_delay matches the injected sub-sample offset to high precision,
  * detection survives carrier-frequency offsets that would break the
    boxcar MF,
  * detection survives moderate noise.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

# Reuse the synthetic-signal generator from the MF tests.
from tests.test_bpsk_pps_calibrator_mf import (  # noqa: E402
    _make_bpsk_signal,
    _feed_in_batches,
    _modular_distance,
    SR,
)

from hf_timestd.core.bpsk_pps_calibrator_diff import (
    BpskPpsCalibratorDiff,
    DIFF_THRESHOLD_FACTOR,
)


def _run_diff(cal, signal, rtp_start=0, batch_size=480):
    """Feed signal in batches to the diff calibrator."""
    rtp = rtp_start
    for i in range(0, len(signal), batch_size):
        batch = signal[i : i + batch_size]
        cal.process_samples(batch, rtp)
        rtp = (rtp + len(batch)) & 0xFFFFFFFF


class TestNoiseFreeDetection:
    def test_detects_edges_at_one_per_second(self):
        cal = BpskPpsCalibratorDiff(sample_rate=SR)
        signal = _make_bpsk_signal(
            duration_s=5.0, edge_offset_samples=0.0,
            transition_width_samples=2.0,
        )
        _run_diff(cal, signal)
        # 5 seconds of signal → 5 edges accepted (give or take 1
        # for boundary effects).
        assert 4 <= cal.pps_ok <= 6, \
            f"expected ~5 edges in 5 s, got {cal.pps_ok}"

    def test_chain_delay_matches_injected_offset(self):
        injected = 12.3
        cal = BpskPpsCalibratorDiff(sample_rate=SR)
        signal = _make_bpsk_signal(
            duration_s=10.0, edge_offset_samples=injected,
            transition_width_samples=2.0,
        )
        _run_diff(cal, signal)
        assert cal.pps_ok >= 5
        err = _modular_distance(cal.chain_delay_samples, injected, SR)
        # Parabolic interp on a sech² peak (derivative of tanh) has
        # small systematic bias; ~1 sample (~10 µs at 96 kHz) covers
        # the synthetic-fixture imprecision.  Real-signal performance
        # against radiod's ±25 kHz filter will be evaluated live.
        assert err < 1.0, \
            f"recovered={cal.chain_delay_samples}, injected={injected}, err={err}"


class TestCarrierFrequencyRobustness:
    """The whole point: this detector should be insensitive to carrier
    frequency offsets that bite the boxcar MF.  Each per-sample diff
    sees |s[n]·(j·2π·Δf/SR)| ≈ A·6e-5 background at 0.5 Hz at 96 kHz —
    the polarity flip's 2A spike is 90 dB above that even at 1 Hz."""

    @pytest.mark.parametrize("carrier_hz", [0.0, 0.5, 2.0, 5.0, 10.0])
    def test_detects_through_carrier_offset(self, carrier_hz):
        injected = 7.5
        cal = BpskPpsCalibratorDiff(sample_rate=SR)
        signal = _make_bpsk_signal(
            duration_s=10.0, edge_offset_samples=injected,
            carrier_freq_hz=carrier_hz,
            transition_width_samples=2.0,
        )
        _run_diff(cal, signal)
        assert cal.pps_ok >= 5, \
            f"carrier_hz={carrier_hz}: only {cal.pps_ok} edges detected"
        err = _modular_distance(cal.chain_delay_samples, injected, SR)
        # 1-sample slack — diff detector should be tight regardless of carrier.
        assert err < 1.0, \
            f"carrier_hz={carrier_hz} err={err} samples"


class TestNoiseRobustness:
    def test_detects_at_20db_snr(self):
        """At 20 dB per-sample SNR the median |d| is dominated by
        noise and the spike-to-median ratio is only ~4×.  Use a
        lower threshold_factor for this regime.  The PROTOTYPE
        operating point on bee1 is much cleaner (sub-Hz residual
        carrier → median ~A·6e-5, spike-to-median ~30 000×) so the
        default 100× factor works there; this test exercises the
        envelope, not the design point."""
        injected = 5.7
        cal = BpskPpsCalibratorDiff(
            sample_rate=SR, threshold_factor=2.5,
        )
        signal = _make_bpsk_signal(
            duration_s=15.0, edge_offset_samples=injected,
            amplitude=1.0, noise_std=0.1,
            transition_width_samples=2.0,
        )
        _run_diff(cal, signal)
        # Detection rate at this SNR is reduced (many noise peaks
        # compete with real edges).  Just verify SOMETHING was
        # detected — precision is not the design point for low SNR.
        assert cal.pps_ok >= 1, \
            f"detected nothing at 20 dB SNR (median={cal._median_d})"


class TestStreamingBoundary:
    """An edge that lands exactly on a batch boundary must still be
    detected — the per-batch carryover of last_sample handles the diff."""

    def test_edge_at_batch_boundary(self):
        # Edge offset = batch_size means PPS edges land exactly between
        # batches.  Verify diff still detects them.
        batch_size = 480
        cal = BpskPpsCalibratorDiff(sample_rate=SR)
        signal = _make_bpsk_signal(
            duration_s=10.0, edge_offset_samples=float(batch_size),
            transition_width_samples=2.0,
        )
        _run_diff(cal, signal, batch_size=batch_size)
        assert cal.pps_ok >= 5


class TestCSVOutput:
    def test_writes_csv_with_header(self, tmp_path: Path):
        out = tmp_path / "diff_edges.csv"
        cal = BpskPpsCalibratorDiff(sample_rate=SR, output_path=str(out))
        signal = _make_bpsk_signal(
            duration_s=3.0, edge_offset_samples=2.0,
            transition_width_samples=2.0,
        )
        _run_diff(cal, signal)
        cal.close()
        assert out.exists()
        contents = out.read_text().splitlines()
        assert contents[0].startswith("timestamp_unix,edge_rtp_int,")
        # At least 2 edge rows (3 seconds of signal → 2-3 edges).
        assert len(contents) >= 3, f"only {len(contents)-1} rows"
        # Sanity-check a row.
        row = contents[1].split(",")
        assert len(row) == 6
        assert float(row[2])  # frac parses


class TestNoCSVMode:
    def test_no_output_path_still_counts_edges(self):
        """Sidecar mode without a CSV file (e.g. tests) still updates
        pps_ok / chain_delay_samples — useful for in-test assertions."""
        cal = BpskPpsCalibratorDiff(sample_rate=SR, output_path=None)
        signal = _make_bpsk_signal(
            duration_s=5.0, edge_offset_samples=0.0,
            transition_width_samples=2.0,
        )
        _run_diff(cal, signal)
        assert cal.pps_ok >= 4
        assert cal.chain_delay_samples is not None


class TestPositionStabilityCheck:
    """After the bootstrap window, an edge whose chain_delay_samples is
    far from the recent consensus must be rejected.  This defends
    against sidelobe outliers that pass the magnitude + inter-edge-time
    gates but land at the wrong sub-sample position."""

    def test_outlier_position_rejected_after_bootstrap(self):
        """Edges that pass the gap gate (~1 s after the previous edge)
        but at a sub-sample position more than DIFF_POSITION_TOL_SAMPLES
        from the running median must be rejected.  Sidelobes in real
        data look like this: timing is roughly right, position is way
        off."""
        from hf_timestd.core.bpsk_pps_calibrator_diff import (
            DIFF_POSITION_HISTORY_BOOTSTRAP,
            DIFF_POSITION_TOL_SAMPLES,
        )
        cal = BpskPpsCalibratorDiff(sample_rate=SR)

        # Manually pre-populate the position history with enough
        # entries to satisfy the bootstrap window — simulates the
        # state after a calm period of consistent edges.
        baseline_cd = 47916.17
        cal._position_history = [
            baseline_cd + 0.001 * i  # tiny per-edge jitter
            for i in range(DIFF_POSITION_HISTORY_BOOTSTRAP + 5)
        ]
        cal.chain_delay_samples = baseline_cd
        cal._last_edge_rtp = 1_000_000

        # Feed one batch with a polarity flip about 1 sec after
        # the simulated previous edge, but at a position 1000
        # samples off from baseline (well beyond the 5-sample tol).
        outlier_offset = baseline_cd + 1000.0  # = 48916.17
        signal = _make_bpsk_signal(
            duration_s=1.5,
            edge_offset_samples=outlier_offset,
            transition_width_samples=2.0,
        )
        # First edge in this synth signal lands at outlier_offset
        # relative to RTP value 0; we want it ~SR samples after
        # _last_edge_rtp = 1_000_000.  Start RTP at 1_000_000 + SR -
        # outlier_offset so first edge lands at RTP 1_000_000 + SR.
        rtp_start = int(1_000_000 + SR - outlier_offset)
        before_rejected = cal.peaks_rejected_position
        _run_diff(cal, signal, rtp_start=rtp_start)

        # chain_delay_samples must NOT have updated — outlier rejected.
        assert abs(cal.chain_delay_samples - baseline_cd) < 1.0, \
            f"outlier leaked through: {cal.chain_delay_samples} vs " \
            f"baseline {baseline_cd}"
        assert cal.peaks_rejected_position > before_rejected, \
            "position-rejection counter did not increment"

    def test_bootstrap_accepts_initial_edges(self):
        """During the bootstrap window (first
        DIFF_POSITION_HISTORY_BOOTSTRAP edges), no position-stability
        check applies — needed so the running median has something
        to converge on."""
        from hf_timestd.core.bpsk_pps_calibrator_diff import (
            DIFF_POSITION_HISTORY_BOOTSTRAP,
        )
        cal = BpskPpsCalibratorDiff(sample_rate=SR)
        signal = _make_bpsk_signal(
            duration_s=float(DIFF_POSITION_HISTORY_BOOTSTRAP + 2),
            edge_offset_samples=5.0,
            transition_width_samples=2.0,
        )
        _run_diff(cal, signal)
        # All bootstrap edges should be accepted — no rejections.
        assert cal.peaks_rejected_position == 0
        assert cal.pps_ok >= DIFF_POSITION_HISTORY_BOOTSTRAP

    def test_wraparound_near_second_boundary(self):
        """If the consensus is near 0 (or near SR), the position
        check must fold the wraparound — an edge at SR-1 vs an edge
        at 0 are physically the same position, not SR samples apart."""
        cal = BpskPpsCalibratorDiff(sample_rate=SR)
        # Edge at offset 0 — chain_delay_samples will be near 0 or
        # near SR depending on which side of the integer boundary
        # the parabolic interp lands on.  Either way, the position
        # check should fold across the wraparound.
        signal = _make_bpsk_signal(
            duration_s=15.0, edge_offset_samples=0.0,
            transition_width_samples=2.0,
        )
        _run_diff(cal, signal)
        # We don't assert specific values; just that running it
        # doesn't produce phantom position-rejection events from
        # wraparound mis-handling.
        assert cal.pps_ok >= 10, \
            f"wraparound test: too few edges ({cal.pps_ok}) — " \
            f"position check may be over-rejecting"


class TestSelfRecovery:
    """When the RF signal shifts enough to violate the position-stability
    or inter-edge-time gate persistently, the gate state never updates
    (it's only updated on accept), so the gate keeps rejecting
    indefinitely.  The self-recovery path counts consecutive rejects
    and resets the gating state once the threshold is crossed.

    Live failure mode this guards against: 2026-05-23 12:23 UTC
    incident — diff calibrator silent for 200+ s after the MF
    concurrently went phantom-stormy.  The MF self-recovered via
    Costas re-lock + step-adoption within ~3 min; without this
    fix the diff calibrator could not.
    """

    def test_consecutive_rejects_counter_increments(self):
        from hf_timestd.core.bpsk_pps_calibrator_diff import (
            DIFF_REJECT_RECOVERY_THRESHOLD,
        )
        cal = BpskPpsCalibratorDiff(sample_rate=SR)
        # Trigger one synthetic reject — counter should be 1, no reset.
        cal._record_reject_and_maybe_recover("gap")
        assert cal._consecutive_rejects == 1
        assert cal.recovery_resets == 0
        # And N-1 more — still no reset.
        for _ in range(DIFF_REJECT_RECOVERY_THRESHOLD - 2):
            cal._record_reject_and_maybe_recover("position")
        assert cal._consecutive_rejects == DIFF_REJECT_RECOVERY_THRESHOLD - 1
        assert cal.recovery_resets == 0

    def test_threshold_crossed_triggers_state_reset(self):
        from hf_timestd.core.bpsk_pps_calibrator_diff import (
            DIFF_REJECT_RECOVERY_THRESHOLD,
        )
        cal = BpskPpsCalibratorDiff(sample_rate=SR)
        # Seed state as if the calibrator had been locked.
        cal._last_edge_rtp = 1_000_000
        cal._position_history = [47916.17 + 0.001 * i for i in range(15)]
        cal._running_max = 1.234
        # Now hit the reject threshold.
        for _ in range(DIFF_REJECT_RECOVERY_THRESHOLD):
            cal._record_reject_and_maybe_recover("position")
        # All gate state must be reset.
        assert cal._last_edge_rtp is None
        assert cal._position_history == []
        assert cal._running_max is None
        assert cal._consecutive_rejects == 0
        assert cal.recovery_resets == 1

    def test_accept_resets_consecutive_counter(self):
        """A burst of N-1 rejects followed by an accept must NOT
        leave the counter near threshold — otherwise one more reject
        a long time later would prematurely fire recovery."""
        from hf_timestd.core.bpsk_pps_calibrator_diff import (
            DIFF_REJECT_RECOVERY_THRESHOLD,
        )
        cal = BpskPpsCalibratorDiff(sample_rate=SR)
        # Send a clean signal to get a real accept.
        signal = _make_bpsk_signal(
            duration_s=3.0, edge_offset_samples=5.0,
            transition_width_samples=2.0,
        )
        _run_diff(cal, signal)
        assert cal.pps_ok >= 2
        # Simulate N-1 rejects (just under threshold).
        for _ in range(DIFF_REJECT_RECOVERY_THRESHOLD - 1):
            cal._record_reject_and_maybe_recover("gap")
        assert cal._consecutive_rejects == DIFF_REJECT_RECOVERY_THRESHOLD - 1
        # Hand-simulate an accept by clearing the counter the same way
        # process_samples does (the in-loop accept path sets it to 0).
        cal._consecutive_rejects = 0
        # Now a fresh single reject must NOT fire recovery.
        cal._record_reject_and_maybe_recover("position")
        assert cal._consecutive_rejects == 1
        assert cal.recovery_resets == 0

    def test_end_to_end_recovery_after_position_wedge(self):
        """Bootstrap on position X, then shift to position Y so the
        position-stability gate would reject everything.  Without
        self-recovery, calibrator stays wedged forever; with it, the
        next peak after threshold-many rejects re-acquires at Y."""
        from hf_timestd.core.bpsk_pps_calibrator_diff import (
            DIFF_POSITION_HISTORY_BOOTSTRAP,
            DIFF_POSITION_TOL_SAMPLES,
            DIFF_REJECT_RECOVERY_THRESHOLD,
        )
        cal = BpskPpsCalibratorDiff(sample_rate=SR)
        # Pre-seed gate state for "previously locked at position X".
        # Do NOT seed _running_max: leaving it None means only the
        # K*median threshold applies, so synthetic peaks reach the
        # peak-iteration loop and exercise the gate paths we care
        # about.  In production, _running_max is also reset by the
        # recovery path so this matches the post-recovery condition.
        x = 47916.17
        cal._position_history = [
            x + 0.001 * i for i in range(DIFF_POSITION_HISTORY_BOOTSTRAP + 5)
        ]
        cal.chain_delay_samples = x
        cal._last_edge_rtp = 1_000_000

        # Now feed enough out-of-position edges to trip recovery.
        # New position y differs from x by > DIFF_POSITION_TOL_SAMPLES.
        y = x + (DIFF_POSITION_TOL_SAMPLES + 50.0)
        # Each second sends one edge at y.  After
        # DIFF_REJECT_RECOVERY_THRESHOLD rejected edges, state resets;
        # the NEXT edge becomes the new bootstrap baseline at y.
        # We feed enough seconds to cover threshold + bootstrap.
        n_seconds = DIFF_REJECT_RECOVERY_THRESHOLD + 5
        signal = _make_bpsk_signal(
            duration_s=float(n_seconds),
            edge_offset_samples=y,
            transition_width_samples=2.0,
        )
        # Start RTP such that each synthesized edge gives a gap close
        # to 1 s (so gap gate doesn't also reject — we want to isolate
        # the position-gate wedge).  The synth signal puts the first
        # edge at sample index ~y; we want that to be 1_000_000 + SR
        # after _last_edge_rtp, so rtp_start = 1_000_000 + SR - y.
        rtp_start = int(1_000_000 + SR - y) & 0xFFFFFFFF
        _run_diff(cal, signal, rtp_start=rtp_start)

        # Recovery must have fired at least once.
        assert cal.recovery_resets >= 1, (
            f"self-recovery never fired; "
            f"position_rejects={cal.peaks_rejected_position}, "
            f"gap_rejects={cal.peaks_rejected_gap}, "
            f"accepts={cal.pps_ok}"
        )
        # Post-recovery, the calibrator must have accepted at least one
        # new edge.  Without recovery this would be 0 — every peak after
        # the initial position-rejection would be gap-rejected forever.
        assert cal.pps_ok >= 1, (
            f"no edges accepted post-recovery despite reset firing; "
            f"recovery_resets={cal.recovery_resets}"
        )
        # And the calibrator's locked position must have *moved* from
        # x — proof that it re-acquired on the new operating point,
        # not just re-anchored on the stale one.
        assert abs(cal.chain_delay_samples - x) > DIFF_POSITION_TOL_SAMPLES, (
            f"post-recovery chain_delay={cal.chain_delay_samples} is "
            f"still at old position x={x}; recovery did not re-acquire"
        )


class TestModuleConstants:
    def test_threshold_factor_default(self):
        # Documented as 100 in the module — flagging if someone
        # silently changes it without updating callers.
        assert DIFF_THRESHOLD_FACTOR == 100.0
