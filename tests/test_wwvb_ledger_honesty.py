"""The WWVB ledger must be able to say "I am blind", and must not count garbage.

Two defects observed on AC0G-B4 over 2026-08-17..19 motivate these tests.

1. **Nothing in a pass record distinguished failure from success.**  Across 432
   passes on the night of 08-19, the ten that decoded and the 422 that did not
   were separable only by the ``frames`` count itself -- the outcome, not an
   independent health signal:

       carrier_offset_hz   0.0 on all 10 decodes ... and on 211 of 422 blind
       seconds_detected    85-90 decoded          ... 68-95 blind
       mean_amp            6.1-7.4e-6 decoded     ... 5.8-7.8e-6 blind

   ``seconds_detected`` actively misleads: blind passes routinely report more
   detected seconds than any real decode reaches.  A source that cannot report
   its own blindness cannot safely join the Fusion pool, so the pass record
   carries an SNR estimate.

2. **The parity==0 noise gate leaks.**  5 of 18 frames accepted across those
   three nights carry minutes in 2053 or 2097 -- 28% garbage -- and they clear
   the gate whose premise (recorded at its call site in core_recorder_v2) is
   that every false positive has parity_errors >= 1.  One of them had
   parity_errors=0 AND sync_errors=0.  The +/-500 ms plausibility gate that
   would have caught them lives in ``build_l1_row``, which only runs when
   ``feed_fusion`` is enabled -- so in the default ledger-only configuration
   there was no gate at all beyond parity.

The frames below are the real records from the B4 ledger, not invented ones.
"""

import datetime as dt
import json
import math

import pytest

from hf_timestd.core.wwvb_fusion import (
    WWVB_MAX_PLAUSIBLE_MINUTE_SKEW_S,
    frame_minute_is_plausible,
)
from hf_timestd.core.wwvb_ledger import WwvbLedger

UTC = dt.timezone.utc


def _utc(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


class TestFrameMinutePlausibility:
    """A decoded minute is checked against the receiver's own approximate time.

    The host clock is used here strictly as a *sanity bench*, never as a
    correction -- the same judging role the Offset Judge is sanctioned for.
    The window is deliberately far looser than any real timing effect so it can
    only ever reject absurdity.
    """

    def test_accepts_a_real_decode(self):
        # Observed: minute 01:53 decoded at 01:54:16 (~77 s of buffer latency).
        assert frame_minute_is_plausible(
            _utc("2026-08-19T01:53:00Z"), now=_utc("2026-08-19T01:54:16Z")
        )

    @pytest.mark.parametrize("minute,now", [
        ("2053-09-29T08:38:00Z", "2026-08-19T00:04:04Z"),  # par=0 sync=0
        ("2097-08-02T21:52:00Z", "2026-08-18T05:31:16Z"),  # par=0 sync=1
    ])
    def test_rejects_the_garbage_that_cleared_the_parity_gate(self, minute, now):
        assert not frame_minute_is_plausible(_utc(minute), now=_utc(now))

    def test_rejects_a_single_bit_hour_error(self):
        """One flipped hour bit puts the minute an hour out -- still garbage."""
        now = _utc("2026-08-19T02:01:16Z")
        assert not frame_minute_is_plausible(_utc("2026-08-19T01:00:00Z"), now=now)

    def test_window_is_loose_enough_never_to_act_as_a_correction(self):
        """Must exceed the worst-case decode latency (90 s buffer + 30 s tick)
        by a wide margin, and stay far under the 3600 s single-bit hour error."""
        assert 240.0 < WWVB_MAX_PLAUSIBLE_MINUTE_SKEW_S < 1800.0

    def test_boundary(self):
        now = _utc("2026-08-19T02:00:00Z")
        skew = dt.timedelta(seconds=WWVB_MAX_PLAUSIBLE_MINUTE_SKEW_S)
        assert frame_minute_is_plausible(now - skew, now=now)
        assert not frame_minute_is_plausible(
            now - skew - dt.timedelta(seconds=1), now=now
        )

    def test_naive_datetime_is_treated_as_utc(self):
        assert frame_minute_is_plausible(
            dt.datetime(2026, 8, 19, 1, 53, 0), now=_utc("2026-08-19T01:54:16Z")
        )


class TestLedgerHonesty:
    def _read(self, tmp_path):
        f = sorted(tmp_path.glob("*.jsonl"))
        assert f, "ledger wrote nothing"
        # Strict parse: NaN is not valid JSON and breaks jq, which the module
        # docstring names as the intended postprocessor.
        return [json.loads(l, parse_constant=_reject) for l in f[0].read_text().splitlines() if l]

    def test_pass_record_carries_snr_and_the_rejected_count(self, tmp_path):
        led = WwvbLedger(tmp_path)
        led.record_pass(
            ts=_utc("2026-08-19T02:00:00Z"), buffer_s=90.0, mean_amp=6.5e-6,
            carrier_offset_hz=0.0, seconds_detected=88, bits=87,
            frames=1, frames_implausible=2, snr_db=12.5,
        )
        led.close()
        row = self._read(tmp_path)[0]
        assert row["snr_db"] == 12.5
        assert row["frames"] == 1
        assert row["frames_implausible"] == 2

    def test_unmeasurable_snr_is_null_not_nan(self, tmp_path):
        """estimate_snr_db returns nan on too few symbols; NaN is invalid JSON."""
        led = WwvbLedger(tmp_path)
        led.record_pass(
            ts=_utc("2026-08-19T02:00:00Z"), buffer_s=90.0, mean_amp=6.5e-6,
            carrier_offset_hz=0.0, seconds_detected=3, bits=2,
            frames=0, frames_implausible=0, snr_db=float("nan"),
        )
        led.close()
        assert self._read(tmp_path)[0]["snr_db"] is None

    def test_frame_record_is_marked_plausible_or_not(self, tmp_path):
        """Garbage is recorded, not silently dropped -- the rate is evidence."""
        led = WwvbLedger(tmp_path)
        for minute, plausible in (
            ("2026-08-19T01:53:00Z", True),
            ("2053-09-29T08:38:00Z", False),
        ):
            led.record_frame(
                ts=_utc("2026-08-19T01:54:16Z"), minute_of_frame=_utc(minute),
                dst_state=None, leap_second=None, parity_errors=0,
                sync_errors=0, inverted_polarity=False, mean_amp=6.1e-6,
                plausible=plausible,
            )
        led.close()
        rows = self._read(tmp_path)
        assert [r["plausible"] for r in rows] == [True, False]


def _reject(const):
    raise AssertionError(f"ledger emitted invalid JSON constant {const!r}")
