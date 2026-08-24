"""T6 phase continuity across the 32-bit RTP counter wrap.

``2**32 % 96000 == 23296`` samples = 242.667 ms: at every counter wrap
(~12 h 26 m at 96 kHz) the mod-sample-rate phase of the masked counter
jumps by that amount while the physical edge is unmoved.  Regressions
for the AC0G-B4 outages of 2026-08-23 23:33Z and 2026-08-24 06:50Z:

* the MF calibrator rejected every post-wrap edge as a phantom
  (23,296 samples "off-position"), froze its chain delay, and the
  fine/coarse cross-check tripped DEGRADED→UNLOCKED;
* a re-lock after the wrap landed one wrap-phase away from the
  persisted disambiguation, so every re-derivation read 16.6 + 242.7
  ≈ 257 ms, was refused by the ±250 ms plausibility guard, and T6
  stayed wedged until a restart re-based the counter.

The fix keeps all T6 phase arithmetic in one continuous counter domain
(``hf_timestd.core.rtp_domain.RtpUnwrapper``), so a wrap is a
non-event: edges keep being accepted, the reported chain delay is
unchanged, and a post-wrap re-lock lands where the pre-wrap lock was.
"""
import numpy as np

from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage
from hf_timestd.core.bpsk_pps_calibrator_mf import BpskPpsCalibratorMF

SR = 96_000
WRAP = 1 << 32
WRAP_PHASE = WRAP % SR  # 23,296 samples = 242.667 ms
BATCH = 1740  # production batch size on B4


def make_pps(n_samples: int, edge_offset: float, start_index: int = 0,
             width_samples: float = 2.0) -> np.ndarray:
    """Polarity-flip BPSK baseband: one smooth flip per second at
    ``edge_offset`` (sample-index domain), polarity alternating each
    second.  Same recipe as the MF calibrator's own tests."""
    t = np.arange(start_index, start_index + n_samples, dtype=np.float64)
    nearest_k = np.round((t - edge_offset) / SR)
    distance = t - (nearest_k * SR + edge_offset)
    sign = np.where(nearest_k % 2 == 0, 1.0, -1.0)
    polarity = sign * np.tanh(distance / width_samples)
    return polarity.astype(np.complex64)


def feed(cal, signal, rtp_start, batch=BATCH):
    """Feed in production-sized batches with the 32-bit-masked declared
    RTP a real stream carries."""
    i = 0
    last = None
    while i < len(signal):
        chunk = signal[i:i + batch]
        r = cal.process_samples(chunk, (rtp_start + i) & 0xFFFFFFFF)
        if r is not None:
            last = r
        i += batch
    return last


def phase_distance(a: float, b: float, modulus: int = SR) -> float:
    d = (a - b) % modulus
    return min(d, modulus - d)


class TestCalibratorAcrossWrap:
    """The MF lock must ride through the counter wrap."""

    EDGE = 43_181.0  # arbitrary position within the second

    def _run(self, seconds_before_wrap, seconds_after_wrap):
        total = seconds_before_wrap + seconds_after_wrap
        rtp_start = (WRAP - seconds_before_wrap * SR) & 0xFFFFFFFF
        # Edge positions are defined on the sample-index axis; the
        # calibrator sees them at rtp = rtp_start + index.
        sig = make_pps(total * SR, self.EDGE)
        cal = BpskPpsCalibratorMF(sample_rate=SR, consecutive_required=5)
        feed(cal, sig, rtp_start)
        return cal

    def test_edges_after_the_wrap_are_still_accepted(self):
        cal = self._run(seconds_before_wrap=12, seconds_after_wrap=10)
        # Locked well before the wrap (~5 edges); the ~10 post-wrap
        # edges must be accepted, not held as phantoms.
        assert cal.locked
        assert cal.pps_phantom == 0
        assert cal.pps_ok >= 15

    def test_reported_chain_delay_is_unchanged_by_the_wrap(self):
        cal = self._run(seconds_before_wrap=12, seconds_after_wrap=10)
        rtp_start = WRAP - 12 * SR
        expected = (rtp_start + self.EDGE) % SR
        assert cal._chain_delay_samples is not None
        assert phase_distance(cal._chain_delay_samples, expected) < 2.0

    def test_relock_after_the_wrap_lands_on_the_prewrap_phase(self):
        """The B4 wedge: an unlock after the wrap re-acquires one
        wrap-phase (23,296 samples) away from every persisted /
        externally-disambiguated value, and is refused forever."""
        seconds_before, seconds_after = 12, 22
        total = seconds_before + seconds_after
        rtp_start = WRAP - seconds_before * SR
        sig = make_pps(total * SR, self.EDGE)
        cal = BpskPpsCalibratorMF(sample_rate=SR, consecutive_required=5)
        # Lock, cross the wrap...
        split = (seconds_before + 2) * SR
        feed(cal, sig[:split], rtp_start & 0xFFFFFFFF)
        before = cal._chain_delay_samples
        assert before is not None
        # ...then lose the lock (storm, gap, watchdog) and re-acquire
        # from post-wrap samples only.
        cal.reset()
        feed(cal, sig[split:], (rtp_start + split) & 0xFFFFFFFF)
        after = cal._chain_delay_samples
        assert after is not None
        assert phase_distance(after, before) < 2.0


class TestFineStageAcrossWrap:
    """The fine estimate's phase must not jump at the counter wrap."""

    def test_estimate_phase_continuous_across_wrap(self):
        edge = 43_181.0
        fold = 10
        seconds_before = 15  # wrap lands inside the second fold block
        total = 3 * fold
        rtp_start = WRAP - seconds_before * SR

        sig_r = make_pps(total * SR, edge).real.astype(np.float64)
        iq = (sig_r + 0j).astype(np.complex64)

        stage = BpskEdgeFineStage(SR, fold_seconds=fold)
        stage.set_coarse_offset_samples((rtp_start + edge) % SR)

        ests = []
        i = 0
        while i < len(iq):
            r = stage.process_samples(
                iq[i:i + BATCH], (rtp_start + i) & 0xFFFFFFFF)
            if r is not None:
                ests.append(r)
            i += BATCH

        assert len(ests) >= 2  # one block before, one after the wrap
        phases = [
            (int(e.edge_rtp) + float(e.edge_subsample)) % SR for e in ests
        ]
        for p in phases[1:]:
            assert phase_distance(p, phases[0]) < 2.0
