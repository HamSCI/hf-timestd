"""Unit tests for hf_timestd.core.wwvb_protocol.

Validates the WWVB PM time-frame protocol layer against the NIST
"Enhanced WWVB Broadcast Format" spec (Lowe, 2013-11-06):

  * Section 4 (bit allocation, sync word, Hamming(31,26), DST/leap)
  * Section 6 worked example: July 4, 2012 at 17:30 UTC

No DSP involved — pure protocol bit-twiddling.
"""

from __future__ import annotations

import datetime as _dt
import random

import pytest

from hf_timestd.core.wwvb_protocol import (
    DstState,
    FRAME_BITS,
    LeapSecond,
    MINUTE_COUNTER_MAX,
    MINUTE_EPOCH,
    SYNC_M_BITS,
    SYNC_T_BITS,
    WwvbTimeFrame,
    encode_time_frame,
    from_minute_counter,
    hamming_decode,
    hamming_parity,
    minute_counter,
    parse_time_frame,
    sync_score,
)


UTC = _dt.timezone.utc


# =============================================================================
# Sync words (NIST Table 3)
# =============================================================================

class TestSyncWords:
    """Sync words are literal constants in the spec — no logic, just match."""

    def test_sync_t_bits_match_spec_table_3(self):
        # Table 3: sync_T = {0 0 1 1 1 0 1 1 0 1 0 0 0} (sync_T[12..0]).
        assert SYNC_T_BITS == (0, 0, 1, 1, 1, 0, 1, 1, 0, 1, 0, 0, 0)
        assert len(SYNC_T_BITS) == 13

    def test_sync_m_bits_match_spec_table_3(self):
        # Table 3: sync_M = {1 1 0 1 0 0 0 1 1 1 0 1 0}.
        assert SYNC_M_BITS == (1, 1, 0, 1, 0, 0, 0, 1, 1, 1, 0, 1, 0)
        assert len(SYNC_M_BITS) == 13

    def test_sync_t_and_sync_m_differ(self):
        # The whole point: receivers discriminate time vs message frames.
        assert SYNC_T_BITS != SYNC_M_BITS


# =============================================================================
# Minute counter ↔ datetime
# =============================================================================

class TestMinuteCounter:

    def test_epoch_is_zero(self):
        assert minute_counter(MINUTE_EPOCH) == 0

    def test_one_minute_after_epoch(self):
        assert minute_counter(MINUTE_EPOCH + _dt.timedelta(minutes=1)) == 1

    def test_section_6_worked_example_minute_counter(self):
        # NIST spec §6: July 4, 2012 at 17:30 UTC → minute counter 6,578,970.
        when = _dt.datetime(2012, 7, 4, 17, 30, tzinfo=UTC)
        assert minute_counter(when) == 6_578_970

    def test_roundtrip(self):
        for offset in [0, 1, 1000, 6_578_970, MINUTE_COUNTER_MAX - 1]:
            when = MINUTE_EPOCH + _dt.timedelta(minutes=offset)
            assert minute_counter(when) == offset
            assert from_minute_counter(offset) == when

    def test_naive_datetime_rejected(self):
        # Catching tz-naive datetimes early avoids silent timezone bugs in
        # the upstream pipeline.
        with pytest.raises(ValueError):
            minute_counter(_dt.datetime(2020, 1, 1, 0, 0))

    def test_pre_epoch_rejected(self):
        with pytest.raises(ValueError):
            minute_counter(_dt.datetime(1999, 12, 31, 23, 59, tzinfo=UTC))


# =============================================================================
# Hamming(31,26) parity
# =============================================================================

class TestHammingParity:

    def test_zero_input_gives_zero_parity(self):
        assert hamming_parity(0) == (0, 0, 0, 0, 0)

    def test_section_6_worked_example_parity(self):
        # NIST spec §6: time word 6,578,970 → parity {1, 0, 0, 1, 0}, where
        # time_par[4] is the MSB.  Our hamming_parity returns
        # (par0, par1, par2, par3, par4), so we expect (0, 1, 0, 0, 1).
        assert hamming_parity(6_578_970) == (0, 1, 0, 0, 1)

    def test_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            hamming_parity(MINUTE_COUNTER_MAX)
        with pytest.raises(ValueError):
            hamming_parity(-1)


# =============================================================================
# Hamming(31,26) decode
# =============================================================================

class TestHammingDecode:

    def test_clean_codeword_decodes_unchanged(self):
        time_word = 6_578_970
        parity = hamming_parity(time_word)
        decoded, errors = hamming_decode(time_word, parity)
        assert decoded == time_word
        assert errors == 0

    @pytest.mark.parametrize("bit", range(26))
    def test_single_data_bit_error_corrected(self, bit):
        # Flip each of the 26 time-word bits; decoder must recover.
        time_word = 6_578_970
        parity = hamming_parity(time_word)
        corrupted = time_word ^ (1 << bit)
        decoded, errors = hamming_decode(corrupted, parity)
        assert decoded == time_word, f"failed to correct bit {bit}"
        assert errors == 1

    @pytest.mark.parametrize("bit", range(5))
    def test_single_parity_bit_error_corrected(self, bit):
        # Flip each parity bit; decoder must flag the error (the time
        # word is fine, so the corrected value equals the input).
        time_word = 6_578_970
        parity = list(hamming_parity(time_word))
        parity[bit] ^= 1
        decoded, errors = hamming_decode(time_word, parity)
        assert decoded == time_word
        assert errors == 1

    def test_double_data_bit_errors_always_mis_correct(self):
        # Hamming(31,26) is a *perfect* code: n = 2^r - 1 = 31 with
        # r = 5 parity bits gives exactly 2^r - 1 = 31 distinct non-zero
        # syndromes, one per single-bit-error position (5 parity + 26
        # data).  No syndrome is left over for double-error detection,
        # so *every* double-bit error in the data bits aliases to some
        # single-bit-error syndrome and the decoder will mis-correct.
        #
        # This is a property of the spec, not a bug.  The NIST claim
        # of "detect up to 2 errors" relies on additional checks the
        # spec layers in (e.g. the redundant time[0] copy at position
        # 19, and a sanity check that the minute counter increments
        # monotonically); see the upstream framing layer.
        #
        # We assert the worst-case behavior here so that any future
        # refactor that *adds* double-error detection (e.g. via the
        # bit-19 cross-check) must update this test consciously.
        time_word = 6_578_970
        parity = hamming_parity(time_word)
        mis_corrected = 0
        flagged = 0
        for i in range(26):
            for j in range(i + 1, 26):
                corrupted = time_word ^ (1 << i) ^ (1 << j)
                decoded, errors = hamming_decode(corrupted, parity)
                if errors == 2:
                    flagged += 1
                elif decoded != time_word:
                    mis_corrected += 1
        # 26 choose 2 = 325 unique double-data-bit error patterns.
        assert flagged == 0, (
            f"Hamming(31,26) shouldn't flag any double-data errors as "
            f"uncorrectable (perfect code), but got {flagged}. Did "
            f"someone add a bit-19 cross-check? Update this test."
        )
        assert mis_corrected == 325, (
            f"All 325 double-data-bit patterns should mis-correct; "
            f"got {mis_corrected}"
        )

    def test_random_corruption_no_silent_corruption(self):
        # Stress test: for many random time words, single-bit errors at
        # random positions must always be corrected.  Guards against a
        # syndrome-table collision being introduced in future refactors.
        rng = random.Random(146)  # WSJT-X hash seed, for fun
        for _ in range(200):
            tw = rng.randint(0, MINUTE_COUNTER_MAX - 1)
            bit = rng.randint(0, 25)
            parity = hamming_parity(tw)
            corrupted = tw ^ (1 << bit)
            decoded, errors = hamming_decode(corrupted, parity)
            assert decoded == tw
            assert errors == 1


# =============================================================================
# Frame encode/decode round-trip
# =============================================================================

class TestFrameRoundTrip:

    def test_basic_roundtrip(self):
        when = _dt.datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        bits = encode_time_frame(when, DstState.IN_EFFECT, LeapSecond.NONE)
        assert len(bits) == FRAME_BITS
        assert all(b in (0, 1) for b in bits)
        frame = parse_time_frame(bits)
        assert frame.minute_of_frame == when
        assert frame.dst_state == DstState.IN_EFFECT
        assert frame.leap_second == LeapSecond.NONE
        assert frame.sync_errors == 0
        assert frame.parity_errors == 0
        assert frame.dst_ls_valid

    def test_sync_appears_first(self):
        bits = encode_time_frame(_dt.datetime(2020, 1, 1, 0, 0, tzinfo=UTC))
        assert tuple(bits[:13]) == SYNC_T_BITS

    def test_zero_bit_at_position_59(self):
        # NIST spec: bit 59 is always 0 (marker in legacy AM).
        bits = encode_time_frame(_dt.datetime(2020, 1, 1, 0, 0, tzinfo=UTC))
        assert bits[59] == 0

    def test_repeated_lsb_at_position_19(self):
        # NIST spec §4.3: position 19 carries a copy of time[0].
        for offset in (0, 1, 2, 3, 6_578_970):
            when = MINUTE_EPOCH + _dt.timedelta(minutes=offset)
            bits = encode_time_frame(when)
            assert bits[19] == (offset & 1), f"offset={offset}"

    @pytest.mark.parametrize("dst,leap", [
        (DstState.NOT_IN_EFFECT, LeapSecond.NONE),
        (DstState.IN_EFFECT, LeapSecond.NONE),
        (DstState.STARTING_TODAY, LeapSecond.NONE),
        (DstState.ENDING_TODAY, LeapSecond.NONE),
        (DstState.NOT_IN_EFFECT, LeapSecond.POSITIVE),
        (DstState.IN_EFFECT, LeapSecond.NEGATIVE),
    ])
    def test_dst_leap_combinations_roundtrip(self, dst, leap):
        when = _dt.datetime(2024, 3, 10, 7, 0, tzinfo=UTC)
        bits = encode_time_frame(when, dst, leap)
        frame = parse_time_frame(bits)
        assert frame.dst_state == dst
        assert frame.leap_second == leap

    def test_notice_bit_roundtrip(self):
        when = _dt.datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
        for n in (0, 1):
            frame = parse_time_frame(encode_time_frame(when, notice=n))
            assert frame.notice == n

    def test_dst_next_code_roundtrip(self):
        when = _dt.datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
        for code in (0, 1, 27, 42, 63):
            frame = parse_time_frame(encode_time_frame(
                when, dst_next_code=code,
            ))
            assert frame.dst_next_code == code

    def test_random_dates_roundtrip(self):
        rng = random.Random(0)
        for _ in range(50):
            offset = rng.randint(0, MINUTE_COUNTER_MAX - 1)
            when = MINUTE_EPOCH + _dt.timedelta(minutes=offset)
            bits = encode_time_frame(when)
            frame = parse_time_frame(bits)
            assert frame.minute_of_frame == when
            assert frame.parity_errors == 0


# =============================================================================
# Section 6 worked example (NIST spec)
# =============================================================================

class TestSection6Example:
    """End-to-end validation against the spec's only fully-worked frame.

    Spec §6 / Table 10: minute starting at 2012-07-04 17:30:00 UTC, with
    DST in effect (dst_on = 11), no leap second, dst_next code = 27
    (transition out of DST on 1st Sunday of November at 02:00 local),
    notice bit set.
    """

    EXAMPLE_TIME = _dt.datetime(2012, 7, 4, 17, 30, tzinfo=UTC)
    EXAMPLE_DST_NEXT = 0b011011  # 27, per Table 8 row 37

    def test_minute_counter_matches(self):
        assert minute_counter(self.EXAMPLE_TIME) == 6_578_970

    def test_parity_bits_match(self):
        par = hamming_parity(6_578_970)
        # Spec: time_par[4..0] = {1, 0, 0, 1, 0}
        assert par == (0, 1, 0, 0, 1)

    def test_encoded_frame_parses_back_to_spec_values(self):
        bits = encode_time_frame(
            self.EXAMPLE_TIME,
            DstState.IN_EFFECT,
            LeapSecond.NONE,
            self.EXAMPLE_DST_NEXT,
            notice=1,
        )
        frame = parse_time_frame(bits)
        assert frame.minute_of_frame == self.EXAMPLE_TIME
        assert frame.dst_state == DstState.IN_EFFECT
        assert frame.leap_second == LeapSecond.NONE
        assert frame.dst_next_code == self.EXAMPLE_DST_NEXT
        assert frame.notice == 1
        assert frame.sync_errors == 0
        assert frame.parity_errors == 0
        assert frame.dst_ls_valid

    def test_encoded_frame_bit_19_is_zero(self):
        # 6,578,970 is even → time[0] = 0 → position 19 = 0.
        bits = encode_time_frame(
            self.EXAMPLE_TIME, DstState.IN_EFFECT, LeapSecond.NONE,
        )
        assert bits[19] == 0
        assert bits[46] == 0  # time[0] in its primary position too

    def test_encoded_sync_word_is_literal(self):
        bits = encode_time_frame(
            self.EXAMPLE_TIME, DstState.IN_EFFECT, LeapSecond.NONE,
        )
        assert tuple(bits[:13]) == (0, 0, 1, 1, 1, 0, 1, 1, 0, 1, 0, 0, 0)


# =============================================================================
# Error handling
# =============================================================================

class TestErrorHandling:

    def test_wrong_frame_length_rejected(self):
        with pytest.raises(ValueError):
            parse_time_frame([0] * 59)
        with pytest.raises(ValueError):
            parse_time_frame([0] * 61)

    def test_non_binary_bit_rejected(self):
        bits = [0] * FRAME_BITS
        bits[10] = 2
        with pytest.raises(ValueError):
            parse_time_frame(bits)

    def test_invalid_dst_ls_flagged_not_raised(self):
        # Construct a frame with a dst_ls value not in Table 4 — e.g.,
        # 0b00000 isn't one of the 12 valid combinations.  The parser
        # must surface this via dst_ls_valid=False, not raise.
        when = _dt.datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        bits = encode_time_frame(when)
        # Force dst_ls bits all-zero: positions 47, 48, 50, 51, 52 carry
        # dst_ls[4], dst_ls[3], dst_ls[2], dst_ls[1], dst_ls[0].
        for pos in (47, 48, 50, 51, 52):
            bits[pos] = 0
        frame = parse_time_frame(bits)
        assert frame.dst_ls_valid is False
        assert frame.dst_state is None
        assert frame.leap_second is None

    def test_corrupted_sync_word_flagged_in_sync_errors(self):
        when = _dt.datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        bits = encode_time_frame(when)
        bits[0] ^= 1
        bits[5] ^= 1
        bits[12] ^= 1
        frame = parse_time_frame(bits)
        assert frame.sync_errors == 3
        # Time word still decodes — sync errors don't block payload.
        assert frame.minute_of_frame == when


# =============================================================================
# Sync correlation helper
# =============================================================================

class TestSyncScore:

    def test_perfect_match(self):
        assert sync_score(SYNC_T_BITS, SYNC_T_BITS) == 13

    def test_complete_mismatch(self):
        flipped = tuple(1 - b for b in SYNC_T_BITS)
        assert sync_score(flipped, SYNC_T_BITS) == 0

    def test_sync_t_vs_sync_m(self):
        # The two sync words differ in 8 positions — useful as a baseline
        # for selecting a discrimination threshold in the framing layer.
        score = sync_score(SYNC_M_BITS, SYNC_T_BITS)
        # Score is matches, so mismatches = 13 - score.
        assert 13 - score == sum(
            1 for a, b in zip(SYNC_M_BITS, SYNC_T_BITS) if a != b
        )

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError):
            sync_score(SYNC_T_BITS, SYNC_T_BITS[:-1])
