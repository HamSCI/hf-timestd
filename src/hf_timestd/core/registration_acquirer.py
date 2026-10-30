"""RegistrationAcquirer — the received ticks place the second boundary.

Spec: docs/superpowers/specs/2026-09-06-t3-self-registration-design.md.

The registration ``utc(sample) = sample0_utc + sample/sample_rate`` has a
rate (the GPSDO's, needs no help) and an origin (a measurement).  Before
this module the origin came from radiod's non-atomic (GPS_TIME,
RTP_TIMESNAP) pair, and the ±20 ms tick search around it could not
re-find the second when the pair landed off (232 ms on ND, 701 ms on
B4, 2026-09-06).  Here the tick train itself places the second: fold the
band-limited envelope at one second, find the peaks, fit them to the
expected station delays by a common shift, hold the result in the RTP
frame, and corroborate or correct it every minute.

When one folded peak fits two stations (WWV or BPM on a shared channel)
and no sibling channel can choose, the 800 ms minute marker does: WWV and
WWVH transmit one, BPM does not (spec §12, task 15).

Sign convention (pinned in the plan's Global Constraints):
    correction_s = expected_delay_s - fold_position_s, wrapped to (-0.5, 0.5]
    sample0_utc_acquired = sample0_utc_label + correction_s
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple

import numpy as np
from hamsci_dsp.counter_map import CounterMap
from scipy.signal import butter, sosfiltfilt

from .counter_epoch_tracker import COUNTER_EPOCH_SKEW_ALLOWANCE_S, COUNTER_EPOCH_STEP_S
from .tick_edge_detector import TickEdgeDetector

logger = logging.getLogger(__name__)

# Task 7 fix round 1: how far, in samples of one channel's counter, a held
# counter map may be read from its rtp_ref.  CounterMap reads RTP as a
# signed 32-bit difference, so past 2**31 (24 h 51 m at 24 kHz) it rolls
# the wrong way; half that leaves margin on either side.
COUNTER_MAP_REACH = 2**30


def _rtp_gap(a: int, b: int) -> int:
    """|a - b| in the 32-bit RTP counter, signed reading."""
    d = (int(a) - int(b)) & 0xFFFFFFFF
    return abs(d - 2**32 if d >= 2**31 else d)

ACQ_MIN_FOLD_SNR_DB = 10.0
FOLD_LENGTHS_S = (60, 120, 180)
TONE_BANDS_HZ: Dict[str, Tuple[float, float]] = {
    "1000": (900.0, 1100.0),  # WWV and BPM ticks
    "1200": (1100.0, 1300.0),  # WWVH ticks
}
BAND_OF_STATION: Dict[str, str] = {"WWV": "1000", "BPM": "1000", "WWVH": "1200"}
# Label seconds-in-minute excluded from the fold: 0 (marker), 29/59 (no
# tick), each widened by ±1 s because the label may be ~0.7 s wrong.
FOLD_SKIP_SECONDS = frozenset({59, 0, 1, 28, 29, 30})
# Fold-peak geometry: a 5 ms tick through a 200 Hz band is a ~10 ms bump.
# PEAK_MIN_SEPARATION_MS is a GUARD BAND, added on each side of a peak's own
# measured half-max width when marking samples taken -- it is not itself the
# two-peak resolving floor.  Two ticks resolve once separated by roughly
# (the weaker tick's half-max width + 2 * PEAK_MIN_SEPARATION_MS), which is
# usually well under PEAK_MAX_WIDTH_MS but scales with the actual bump, not
# with a fixed 25 ms exclusion regardless of width.
PEAK_MIN_SEPARATION_MS = 8.0
PEAK_MAX_WIDTH_MS = 25.0
# Spec §10 (task-11b): a fold-lattice phantom -- real noise that only crosses
# ACQ_MIN_FOLD_SNR_DB at one particular fold length -- is absent from a fold
# of either half of the same buffer; a genuine tick recurs every second, so
# it survives the halving even when weak.  3.0 ms is generous next to a
# tick's own ~5 ms half-max width; it only needs to reject a peak that moved
# to a DIFFERENT lattice line, not to demand sub-millisecond repeatability.
PEAK_PERSISTENCE_MS = 3.0
ENVELOPE_LPF_HZ = 400.0
ORIGIN_SIGMA_FLOOR_MS = 1.0

# Same-site broadcasts (WWV on 2.5-25 MHz, all from Fort Collins) share the
# great-circle path, so their corrections agree far more tightly than those
# derived from different sites (Fort Collins / Kauai / Lintong) -- mjh 2026-09-06.
SAME_SITE_AGREE_MS = 1.5
CROSS_SITE_AGREE_MS = 4.0


@dataclass(frozen=True)
class FoldPeak:
    band: str
    position_s: float  # onset (half-rise) position within the label second
    snr_db: float
    width_ms: float


def _band_envelope(audio: np.ndarray, sample_rate: int, band: str) -> np.ndarray:
    """Band-limited tick envelope, returned in **float32**.

    A 180 s buffer at 24 kHz is 34.6 MB in float64 and half that in
    float32, and the acquirer holds one envelope per tone band across the
    full fold and both half folds (final review, I2: a bootstrap minute
    added ~200 MB of peak RSS against MemoryHigh=900M, on the night when
    every channel bootstraps at once).  The filtering itself stays in
    float64 -- the butter coefficients are float64, and a float32 SOS is
    where biquad cascades go unstable -- so only the LIVE array narrows;
    the intermediates die with this call.
    """
    lo, hi = TONE_BANDS_HZ[band]
    sos = butter(4, [lo, hi], btype="bandpass", fs=sample_rate, output="sos")
    x = sosfiltfilt(sos, np.asarray(audio, dtype=np.float64))
    np.abs(x, out=x)
    sos_lp = butter(2, ENVELOPE_LPF_HZ, btype="lowpass", fs=sample_rate, output="sos")
    return sosfiltfilt(sos_lp, x).astype(np.float32, copy=False)


def band_envelopes(audio: np.ndarray, sample_rate: int) -> Dict[str, np.ndarray]:
    """One envelope per tone band, computed ONCE (final review, I2)."""
    return {band: _band_envelope(audio, sample_rate, band) for band in TONE_BANDS_HZ}


def fold_envelope(
    env: np.ndarray,
    sample_rate: int,
    sample0_utc_label: float,
    n_seconds: int,
    start_offset_s: float = 0.0,
) -> Tuple[np.ndarray, int]:
    """Average ``n_seconds`` label-seconds of an ALREADY band-limited
    envelope into one second, starting ``start_offset_s`` into ``env``.

    ``sample0_utc_label`` labels sample 0 of ``env``, not of the offset
    start, so a caller folding the second half of a buffer passes the same
    label it used for the first (final review, I2: the half-fold
    persistence gate used to re-filter a slice of the raw audio, which
    both re-derived the envelope and put a filtfilt edge transient at the
    halfway point).

    Rows whose label second-in-minute is in FOLD_SKIP_SECONDS are left
    out.  Accumulates in place rather than materialising ``np.stack(rows)``
    (31 MB at 180 s).  Returns (profile[sample_rate], rows_used).
    """
    base = int(round(start_offset_s * sample_rate))
    label0 = sample0_utc_label + start_offset_s
    first_sec = int(np.ceil(label0))
    i0 = base + int(round((first_sec - label0) * sample_rate))
    acc = np.zeros(sample_rate, dtype=np.float64)
    rows = 0
    n_env = len(env)
    for k in range(n_seconds):
        if (first_sec + k) % 60 in FOLD_SKIP_SECONDS:
            continue
        a = i0 + k * sample_rate
        b = a + sample_rate
        if b > n_env:
            break
        acc += env[a:b]
        rows += 1
    if rows == 0:
        return np.zeros(sample_rate), 0
    acc /= rows
    return acc, rows


def fold_tick_train(
    audio: np.ndarray,
    sample_rate: int,
    sample0_utc_label: float,
    band: str,
    n_seconds: int,
) -> Tuple[np.ndarray, int]:
    """Average ``n_seconds`` label-seconds of the band envelope into one
    second.  Rows whose label second-in-minute is in FOLD_SKIP_SECONDS
    are left out.  Returns (profile[sample_rate], rows_used).

    A convenience wrapper over ``_band_envelope`` + ``fold_envelope``: it
    derives the envelope for one band and throws it away.  The acquirer
    itself does not use this -- it derives every fold of an attempt from
    one envelope per band (I2)."""
    env = _band_envelope(audio, sample_rate, band)
    return fold_envelope(env, sample_rate, sample0_utc_label, n_seconds)


def find_fold_peaks(
    profile: np.ndarray,
    sample_rate: int,
    band: str,
    min_snr_db: float = ACQ_MIN_FOLD_SNR_DB,
) -> List[FoldPeak]:
    """Peaks above ``min_snr_db`` over a robust baseline.  SNR is
    20·log10(peak_above_baseline / MAD·1.4826).  Position is the half-rise
    sample on the leading edge, which sits on the tick onset; the apex
    would sit ~5 ms late."""
    if profile.size == 0 or not np.any(profile):
        return []
    baseline = np.median(profile)
    dev = profile - baseline
    mad = np.median(np.abs(dev)) * 1.4826
    if mad <= 0:
        return []
    # ACQ_MIN_FOLD_SNR_DB is a FLOOR.  The profile has ~len/30 independent
    # bins after the 400 Hz envelope LPF (24000 -> ~800), and pure noise
    # reaches sqrt(2 ln n_eff) ≈ 3.7σ somewhere among them; one extra σ of
    # margin puts the effective threshold near 13 dB, so noise alone never
    # names a tick.  A real tick folds far above this (a 5 dB tick gains
    # ~17 dB over 54 rows).
    n_eff = max(16.0, len(dev) * 2.0 * ENVELOPE_LPF_HZ / sample_rate)
    noise_extreme = np.sqrt(2.0 * np.log(n_eff)) + 1.0
    thr = mad * max(10 ** (min_snr_db / 20.0), noise_extreme)
    sep = int(PEAK_MIN_SEPARATION_MS * sample_rate / 1000)
    max_w = int(PEAK_MAX_WIDTH_MS * sample_rate / 1000)
    n = len(dev)
    order = np.argsort(dev)[::-1]
    taken = np.zeros_like(dev, dtype=bool)
    peaks: List[FoldPeak] = []
    for idx in order:
        if dev[idx] < thr:
            break
        # exclusion zone wraps circularly: position_s is defined mod 1 s,
        # so a peak near sample 0 and one near sample n-1 can be neighbors.
        if taken[(np.arange(idx - sep, idx + sep + 1)) % n].any():
            continue
        half = dev[idx] / 2.0
        # walk back to the half-rise point (circular within the second)
        j = idx
        steps = 0
        while dev[j % len(dev)] > half and steps < max_w:
            j -= 1
            steps += 1
        onset = (j + 1) % len(dev)
        # width at half max
        k = idx
        steps2 = 0
        while dev[k % len(dev)] > half and steps2 < max_w:
            k += 1
            steps2 += 1
        width_samples = steps + steps2
        width_ms = width_samples * 1000.0 / sample_rate
        if width_ms > PEAK_MAX_WIDTH_MS:
            continue  # a plateau, not a tick
        # Taken zone is the MEASURED bump plus PEAK_MIN_SEPARATION_MS of
        # guard on each side (circular within the second, as the
        # acceptance check above already is) -- not a fixed ±max_w, which
        # would force unrelated ticks apart by ~sep+max_w instead of the
        # intended ~width+2*sep.
        taken[(np.arange(onset - sep, onset + width_samples + sep + 1)) % n] = True
        peaks.append(
            FoldPeak(
                band=band,
                position_s=onset / sample_rate,
                snr_db=float(20 * np.log10(dev[idx] / mad)),
                width_ms=width_ms,
            )
        )
    peaks.sort(key=lambda p: -p.snr_db)
    return peaks


BAND_ARBITRATION_MS = 6.0


def arbitrate_bands(
    peaks_by_band: Dict[str, List[FoldPeak]], agree_ms: float = BAND_ARBITRATION_MS
) -> List[FoldPeak]:
    """Assign each fold position to ONE tone band.  A 5 ms tick has a sinc
    main lobe ~200 Hz wide, so a 1200 Hz tick leaks into 900-1100 Hz and a
    1000 Hz tick into 1100-1300 Hz; no filter separates them.  The band
    whose centre matches the tone responds more strongly, so when two
    bands carry a peak at the same position the weaker one is the leak.
    agree_ms=6.0: a measured WWVH leak's onset sat ~2.75 ms from its true
    1200-band peak (a real, same-tick artifact); distinct stations in
    different bands are >=18 ms apart in the tests here, so 6 ms merges
    the former without any real risk of merging the latter."""
    allp = [p for ps in peaks_by_band.values() for p in ps]
    allp.sort(key=lambda p: -p.snr_db)
    kept: List[FoldPeak] = []
    for p in allp:
        clash = any(
            k.band != p.band
            and abs(((p.position_s - k.position_s) + 0.5) % 1.0 - 0.5) * 1000.0
            <= agree_ms
            for k in kept
        )
        if not clash:
            kept.append(p)
    return kept


def peaks_from_envelopes(
    envelopes: Dict[str, np.ndarray],
    sample_rate: int,
    sample0_utc_label: float,
    n_seconds: int,
    start_offset_s: float = 0.0,
) -> List[FoldPeak]:
    """Fold every tone band's envelope over ``n_seconds`` and arbitrate the
    leaks between them -- the ``fold_envelope``/``find_fold_peaks``/
    ``arbitrate_bands`` pipeline, shared by the full-buffer fold and the
    peak-persistence half-folds (task-11b) so both apply exactly the same
    detection threshold."""
    by_band: Dict[str, List[FoldPeak]] = {}
    for band, env in envelopes.items():
        profile, rows = fold_envelope(
            env, sample_rate, sample0_utc_label, n_seconds, start_offset_s
        )
        if rows == 0:
            continue
        by_band[band] = find_fold_peaks(profile, sample_rate, band)
    return arbitrate_bands(by_band)


def _fold_and_arbitrate(
    audio: np.ndarray, sample_rate: int, sample0_utc_label: float, n_seconds: int
) -> List[FoldPeak]:
    """``peaks_from_envelopes`` on envelopes derived here and discarded --
    the raw-audio entry point, kept for callers that hold no envelopes."""
    return peaks_from_envelopes(
        band_envelopes(audio, sample_rate), sample_rate, sample0_utc_label, n_seconds
    )


@dataclass(frozen=True)
class Hypothesis:
    correction_s: float
    sigma_ms: float
    assignments: tuple  # ((station, band, position_s, snr_db), ...)
    support: int
    unambiguous: bool


def wrap_half_second(x_s: float) -> float:
    """Wrap into (-0.5, 0.5]."""
    y = (x_s + 0.5) % 1.0 - 0.5
    return 0.5 if y == -0.5 else y


def _peak_persists_in_both_halves(
    envelopes: Dict[str, np.ndarray],
    sample_rate: int,
    s0: float,
    n_sec: int,
    h: "Hypothesis",
) -> bool:
    """Spec §10 (task-11b): fold the FIRST half and the SECOND half of the
    same buffer separately and require that, for at least one assignment in
    ``h``, a peak in the same band lies within ``PEAK_PERSISTENCE_MS`` of
    that assignment's ``position_s`` in BOTH halves.  A fold-lattice
    phantom -- a peak that only crosses the detection floor at the one
    fold length the acquirer happened to use -- is absent from a shorter
    fold on either side; a genuine tick, present every second, survives
    the halving even at reduced SNR."""
    half = n_sec // 2
    if half < 1:
        return False
    # I2: both halves come off the SAME envelopes the full fold used --
    # this gate used to re-filter the whole buffer twice more per band
    # (~0.5 s of CPU and ~100 MB of allocation per attempt), and it passed
    # `audio_all` rather than a slice for the first half.
    first = peaks_from_envelopes(envelopes, sample_rate, s0, half)
    second = peaks_from_envelopes(
        envelopes, sample_rate, s0, n_sec - half, start_offset_s=float(half)
    )
    for _station, band, position_s, _snr in h.assignments:
        in_first = any(
            p.band == band
            and abs(wrap_half_second(position_s - p.position_s)) * 1000.0
            <= PEAK_PERSISTENCE_MS
            for p in first
        )
        in_second = any(
            p.band == band
            and abs(wrap_half_second(position_s - p.position_s)) * 1000.0
            <= PEAK_PERSISTENCE_MS
            for p in second
        )
        if in_first and in_second:
            return True
    return False


def _sigma_ms_from_snr(snr_db: float) -> float:
    # Tick rise through a 200 Hz band ~5 ms; timing sigma ≈ rise / (S/N).
    rise_ms = 5.0
    return max(ORIGIN_SIGMA_FLOOR_MS, rise_ms / (10 ** (snr_db / 20.0)) * 3.0)


def _combine_pairings(
    corrections_s: List[float], snrs_db: List[float]
) -> Tuple[float, float]:
    """SNR-weighted mean correction and the sigma that goes with it --
    one formula, shared by ``fit_template`` and by
    ``strip_non_registering_stations`` so a re-derived hypothesis weighs
    its evidence exactly as the original fit did."""
    w = np.array([10 ** (snr / 10.0) for snr in snrs_db])
    corr = float(np.sum(w * np.array(corrections_s)) / np.sum(w))
    sig = float(min(_sigma_ms_from_snr(snr) for snr in snrs_db) / np.sqrt(len(snrs_db)))
    return wrap_half_second(corr), max(ORIGIN_SIGMA_FLOOR_MS, sig)


def fit_template(
    peaks: List[FoldPeak],
    expected_delays_s: Dict[str, float],
    agree_ms: float = CROSS_SITE_AGREE_MS,
) -> List[Hypothesis]:
    """Fit the peak set to the station delay template by a common shift.

    Every (peak, station) pairing whose tone bands agree proposes a
    correction; pairings whose corrections agree within ``agree_ms`` form
    one hypothesis with support = number of peaks.  A hypothesis is
    unambiguous with support >= 2 or when the peak's band admits exactly
    one eligible station.

    ``agree_ms`` defaults to ``CROSS_SITE_AGREE_MS``: every pairing this
    function forms compares two DIFFERENT stations (WWV vs. BPM, or WWV
    vs. WWVH) sharing one tone band, which is a cross-site comparison by
    definition (task-11 fix round 1, controller ruling 2026-09-07) --
    the earlier literal ``3.0`` was a plan error, not a deliberately
    chosen tolerance, and sat marginally below the jitter a weak (14-18
    dB) secondary station's fold position carries at 180 s of
    integration, intermittently stalling real acquisitions that should
    have resolved."""
    pairings = []
    for p in peaks:
        compatible = [s for s in expected_delays_s if BAND_OF_STATION.get(s) == p.band]
        for s in compatible:
            corr = wrap_half_second(expected_delays_s[s] - p.position_s)
            pairings.append((corr, s, p, len(compatible)))
    if not pairings:
        return []
    pairings.sort(key=lambda x: x[0])
    used = [False] * len(pairings)
    hyps: List[Hypothesis] = []
    for i, (corr_i, _, _, _) in enumerate(pairings):
        if used[i]:
            continue
        group = []
        seen_peaks = set()
        for j, (corr_j, s_j, p_j, n_comp) in enumerate(pairings):
            if used[j] or id(p_j) in seen_peaks:
                continue
            if abs(wrap_half_second(corr_j - corr_i)) * 1000.0 <= agree_ms:
                group.append(j)
                seen_peaks.add(id(p_j))
        for j in group:
            used[j] = True
        members = [pairings[j] for j in group]
        corr, sigma_ms = _combine_pairings(
            [m[0] for m in members], [m[2].snr_db for m in members]
        )
        support = len(members)
        unamb = support >= 2 or (support == 1 and members[0][3] == 1)
        hyps.append(
            Hypothesis(
                correction_s=corr,
                sigma_ms=sigma_ms,
                assignments=tuple(
                    (m[1], m[2].band, m[2].position_s, m[2].snr_db) for m in members
                ),
                support=support,
                unambiguous=unamb,
            )
        )
    hyps.sort(key=lambda h: (-h.support, -sum(a[3] for a in h.assignments)))
    return hyps


# Spec §10 already keeps BPM out of the timing product; task 16a keeps it
# out of the registration too.  Live on AC0G-ND, 2026-09-07 20:36Z: every
# shared channel fit two 1000 Hz peaks 34 ms apart as WWV + BPM, called
# the pair unambiguous on support 2, verified it, and anchored the ring
# and the FUSE feed on it -- and FUSE then told chrony the host ran 23-28
# ms fast against an NTP consensus of 5-13 ms until corroboration reset
# the plane six minutes later.  BPM at ND on 5/10/15 MHz in mid-afternoon
# does not happen; the second peak was a WWV artefact wearing BPM's
# delay.
NON_REGISTERING_STATIONS = frozenset({"BPM"})


def strip_non_registering_stations(
    hyps: List[Hypothesis], expected_delays_s: Dict[str, float]
) -> List[Hypothesis]:
    """Drop every ``NON_REGISTERING_STATIONS`` assignment and re-derive
    what the survivors support.

    A WWV + BPM pair loses its second station and becomes a single-peak
    WWV hypothesis, carrying the correction of the WWV peak ALONE -- not a
    mean dragged toward the artefact.  Support and unambiguity follow from
    what remains, and the peak's band still counts BPM among the stations
    that COULD read it, so the lone WWV reading stays ambiguous and waits
    for the minute marker (§12) or a sibling.  A hypothesis left with no
    assignment at all disappears.

    ``fit_template`` itself keeps fitting every station in the template,
    BPM included: the fit is how the acquirer learns that a second reading
    of the same peak exists, and dropping BPM from the template instead
    would make every lone 1000 Hz peak "unambiguously WWV" by
    construction -- the fold-lattice trap of task-11b, reopened."""
    out: List[Hypothesis] = []
    for h in hyps:
        kept = tuple(a for a in h.assignments if a[0] not in NON_REGISTERING_STATIONS)
        if not kept:
            continue
        if len(kept) == len(h.assignments):
            out.append(h)
            continue
        corrs = [
            wrap_half_second(expected_delays_s[st] - pos)
            for st, _band, pos, _snr in kept
            if st in expected_delays_s
        ]
        snrs = [snr for st, _band, _pos, snr in kept if st in expected_delays_s]
        if not corrs:
            continue
        corr, sigma_ms = _combine_pairings(corrs, snrs)
        # How many stations in the template could read this peak?  Two
        # (WWV and BPM on 1000 Hz) leaves the reading ambiguous even
        # though only one of them may register.
        readings = max(
            len([s for s in expected_delays_s if BAND_OF_STATION.get(s) == band])
            for _st, band, _pos, _snr in kept
        )
        out.append(
            Hypothesis(
                correction_s=corr,
                sigma_ms=sigma_ms,
                assignments=kept,
                support=len(kept),
                unambiguous=len(kept) >= 2 or (len(kept) == 1 and readings == 1),
            )
        )
    out.sort(key=lambda h: (-h.support, -sum(a[3] for a in h.assignments)))
    return out


MARKER_SEARCH_HALF_S = 1.5
MARKER_LEN_S = 0.800
MARKER_MIN_SNR_DB = 6.0
MARKER_BIN_S = 0.001


def locate_minute_marker(
    audio: np.ndarray,
    sample_rate: int,
    sample0_utc_label: float,
    band: str,
    minute_utc: int,
) -> Optional[Tuple[float, float]]:
    """``marker_in_envelope`` on an envelope derived here and discarded --
    the raw-audio entry point, for callers that hold no envelopes."""
    return marker_in_envelope(
        _band_envelope(audio, sample_rate, band),
        sample_rate,
        sample0_utc_label,
        minute_utc,
    )


def marker_in_envelope(
    env: np.ndarray,
    sample_rate: int,
    sample0_utc_label: float,
    minute_utc: int,
) -> Optional[Tuple[float, float]]:
    """Find the 800 ms marker near where the label puts second 0 of
    ``minute_utc``, in an ALREADY band-limited envelope.  Decimates it to
    MARKER_BIN_S bins over the ±MARKER_SEARCH_HALF_S search segment and
    scores each MARKER_LEN_S window by its bin-median (a regular tick
    occupies too few bins to move a median; only a sustained ~800 ms tone
    lifts one). Returns (onset offset from the label's minute_utc in
    seconds, SNR dB) or None.

    Takes the envelope rather than the audio (task 15) so the acquirer's
    marker search comes off the SAME two envelopes its folds do -- one
    derivation per band per attempt (I2) -- and so the search can run over
    the whole CONCATENATED bootstrap buffer, where a minute boundary has
    run-up on both sides.  The live ring hands the service [minute,
    minute + 60 s): inside one such buffer the minute's own marker sits at
    sample ~0 and the ±1.5 s search never fits, which is why the
    integer-second search found nothing on either station before this."""
    centre = int(round((minute_utc - sample0_utc_label) * sample_rate))
    half = int(MARKER_SEARCH_HALF_S * sample_rate)
    bin_len = max(1, int(round(sample_rate * MARKER_BIN_S)))
    window_bins = int(round(MARKER_LEN_S / MARKER_BIN_S))
    L = window_bins * bin_len
    a = centre - half
    b = centre + half + L
    if a < 0 or b > len(env):
        return None
    seg = env[a:b]
    # Score is a sliding MEDIAN over the 800 ms window, taken on a 1 ms
    # decimation of the envelope -- not a windowed MEAN over the raw
    # envelope, and not a ratio against the raw envelope's per-sample
    # MAD.  Both of those were tried first and both leak: a regular 5 ms
    # tick recurs every second and can only ever nudge a MEAN over 800
    # 1 ms bins by ~5/800 of its amplitude, but a windowed-mean score's
    # own MAD shrinks just as fast as a per-sample MAD when tick SNR
    # rises, so the ratio between them stays roughly constant instead of
    # closing -- measured on the shipped (now-replaced) version: the
    # marker-absent case read +9.4 dB at 30 dB tick SNR and +19 dB at 40
    # dB, both above the 6 dB gate.  A tick occupies ~5 of 800 one-ms
    # bins in any window; a MEDIAN over that window cannot be moved by
    # so small a minority no matter how strong the tick, so only a
    # genuine ~800 ms tone -- which owns the whole window -- lifts it.
    n_bins = len(seg) // bin_len
    decimated = seg[: n_bins * bin_len].reshape(n_bins, bin_len).mean(axis=1)
    windows = np.lib.stride_tricks.sliding_window_view(decimated, window_bins)
    score = np.median(windows, axis=1)
    k = int(np.argmax(score))
    baseline = np.median(decimated)
    mad = np.median(np.abs(decimated - baseline)) * 1.4826
    if mad <= 0:
        return None
    snr_db = float(20 * np.log10((score[k] - baseline) / mad))
    if snr_db < MARKER_MIN_SNR_DB:
        return None
    # refine to the half-rise onset of the full-rate envelope inside the
    # winning (bin-resolution) window
    onset_bin_sample = k * bin_len
    win = seg[onset_bin_sample : onset_bin_sample + L]
    thr = baseline + 0.5 * (np.max(win) - baseline)
    rise = int(np.argmax(win > thr))
    onset_sample = a + onset_bin_sample + rise
    return (onset_sample / sample_rate) - (minute_utc - sample0_utc_label), snr_db


MARKER_HYPOTHESIS_AGREE_MS = 10.0
# WWV and WWVH open every minute with an 800 ms tone (1000 Hz from Fort
# Collins, 1200 Hz from Kauai).  BPM does not transmit a minute marker at
# all -- its second pulses are 10 cycles of 1 kHz and its minute structure
# differs -- so an 800 ms tone standing on a tick train's own fold
# position says the train is not BPM's.
MARKER_STATIONS = frozenset({"WWV", "WWVH"})


def marker_position_s(expected_delay_s: float, correction_s: float) -> float:
    """Where a hypothesis puts its station's minute marker: the offset
    from the LABEL's minute, which is what ``marker_in_envelope``
    measures, wrapped into one second.

    A hypothesis says the label plane sits ``correction_s`` off truth
    (``utc = label + correction``), so a transmission leaving the station
    at second 0 arrives ``expected_delay_s - correction_s`` after the
    label's minute.  That is the same position the station's ordinary
    ticks fold to -- which is the whole point of the check: the marker has
    to stand ON the tick train it is being asked to name.

    It follows that two hypotheses built from ONE peak (WWV vs. BPM on a
    shared channel) predict the SAME marker position: ``correction`` is
    ``expected_delay - peak_position`` for each, so both reduce to the
    peak position.  Position therefore CONFIRMS that a marker belongs to
    this peak; it can never choose between the two stations reading it.
    The choice comes from BPM transmitting no marker at all."""
    return wrap_half_second(expected_delay_s - correction_s)


def marker_names_one_hypothesis(
    open_hyps: List[Hypothesis],
    expected_delays_s: Dict[str, float],
    marker_for_band,
    agree_ms: float = MARKER_HYPOTHESIS_AGREE_MS,
) -> Optional[Tuple[Hypothesis, str, float, float, tuple]]:
    """Spec §12: does the 800 ms minute marker name exactly one of the
    open hypotheses?

    ``marker_for_band(band)`` returns that band's marker as (offset from
    the label's minute in seconds, SNR dB) or None -- the caller owns the
    search so it can be done once per band, over whatever audio it holds.

    A marker found in a band keeps the hypotheses whose predicted marker
    position (``marker_position_s``) it agrees with to within
    ``agree_ms``, and of those only the ones whose station actually
    transmits a marker (``MARKER_STATIONS``).  Exactly one survivor names
    the station; anything else leaves every hypothesis open.

    Returns (hypothesis, band, marker offset, marker SNR, stations
    excluded because they transmit no marker) or None."""
    bands: List[str] = []
    for h in open_hyps:
        for station, band, _pos, _snr in h.assignments:
            if station in MARKER_STATIONS and band not in bands:
                bands.append(band)
    for band in bands:
        mk = marker_for_band(band)
        if mk is None:
            continue
        offset_s, snr_db = mk
        agreeing: List[Tuple[Hypothesis, str]] = []
        for h in open_hyps:
            for station, b, _pos, _snr in h.assignments:
                if b != band or station not in expected_delays_s:
                    continue
                predicted = marker_position_s(
                    expected_delays_s[station], h.correction_s
                )
                if abs(wrap_half_second(offset_s - predicted)) * 1000.0 <= agree_ms:
                    agreeing.append((h, station))
                    break
        keep = [(h, s) for h, s in agreeing if s in MARKER_STATIONS]
        excluded = tuple(s for _h, s in agreeing if s not in MARKER_STATIONS)
        if len(keep) == 1:
            return keep[0][0], band, offset_s, snr_db, excluded
    return None


def integer_second_correction(
    marker_offset_s: float, expected_delay_s: float, fractional_correction_s: float
) -> int:
    """Whole seconds to add to the label plane on top of the fold's
    fractional correction.  The marker appears at ``expected_delay + walk``
    in the label frame, so the total correction is ``expected_delay −
    marker_offset``; the fold already supplied its fractional part."""
    total = expected_delay_s - marker_offset_s
    return int(round(total - fractional_correction_s))


# Task 17b (review W2/C2).  Moving a station's UTC by a whole second is
# the one error this system has no other detector for: ticks are 1 s
# periodic and ``TickEdgeDetector`` searches ±20 ms, so a plane wrong by
# exactly ±1 s produces a fine-search residual of ~0 -- gate (b) passes,
# ``corroborate`` tightens, and the station publishes the error as
# authoritative T3.  ``MARKER_MIN_SNR_DB`` (6 dB) is the bar for NAMING a
# station, where a wrong answer costs the WWV-vs-BPM delay difference,
# ~37 ms.  Moving the second costs 1000 ms, so it gets its own bar, at
# the observed evidence rather than at the detection floor: the measured
# markers ran 26-39 dB (review I3).
MARKER_INT_SECOND_MIN_SNR_DB = 20.0
# ...and it may move the second by at most ONE.  The marker search spans
# [centre − 1.5 s, centre + 2.3 s], so k ∈ {−2, −1, 0, +1} is reachable;
# a two-second claim describes a marker the search should not have
# reached, not a two-second walk.
MARKER_INT_SECOND_MAX_K = 1
# Attempts an acquisition will WAIT for a second minute to confirm a
# non-zero whole second before giving up and registering without it.
# The whole second is the only quantity here worth a delay: the fold
# plane is available immediately and is what c7b2106 shipped, so waiting
# costs the pre-marker behaviour for a minute or two and buys the one
# correction that cannot be checked afterwards.
WHOLE_SECOND_HOLD_MAX_MINUTES = 2


@dataclass(frozen=True)
class WholeSecondDecision:
    """What the minute marker is allowed to do to the whole second.

    ``k_raw`` is the whole second the marker IMPLIES, always measured and
    always reported; ``k_int`` is the one actually applied to the plane.
    They differ exactly when a gate refused, and then ``unresolved`` is
    true and the registration says so (``whole_second_unresolved``).
    """

    k_int: int
    k_raw: int
    agrees: bool
    unresolved: bool
    reason: str


def whole_second_from_marker(
    marker_offset_s: float,
    marker_snr_db: float,
    expected_delay_s: float,
    fractional_correction_s: float,
    *,
    confirmed_k: Optional[int] = None,
    agree_ms: float = MARKER_HYPOTHESIS_AGREE_MS,
    min_snr_db: float = MARKER_INT_SECOND_MIN_SNR_DB,
    max_k: int = MARKER_INT_SECOND_MAX_K,
) -> WholeSecondDecision:
    """Bound and unwrap the marker's whole-second claim (task 17b).

    The agreement test compares WITHOUT wrapping::

        |marker_offset − marker_position| ≤ agree_ms

    At 7a99e21 it wrapped, and the wrap discarded exactly the quantity
    ``integer_second_correction`` then acted on -- so a marker landing a
    whole second from the fold position both promoted the hypothesis and
    moved the plane by a second (review W2, with B4's own numbers:
    d_WWV = 4.027 ms, fold peak 80.79 ms, a marker at +1080.790 ms
    "agreeing" and shifting the plane -1076.8 ms).

    ``confirmed_k`` is the whole second the marker implied on the
    IMMEDIATELY PRECEDING minute, or None when there was no such minute
    or it implied something else.  A whole second moves the plane only
    when two consecutive minutes of an independent fold agree on it; the
    review measured the marker at +81.712 and +81.837 ms on consecutive
    minutes, so that evidence is already in hand on a healthy channel.

    ``reason`` names the outcome, and one value is actionable: on
    ``"unconfirmed"`` a second minute can still supply the confirmation,
    so the caller may wait.  Every other refusal is final for this fold.
    """
    predicted_s = marker_position_s(expected_delay_s, fractional_correction_s)
    agrees = abs(float(marker_offset_s) - predicted_s) * 1000.0 <= float(agree_ms)
    k_raw = integer_second_correction(
        marker_offset_s, expected_delay_s, fractional_correction_s
    )
    if k_raw == 0:
        # Nothing to gate: the fold's fractional correction is the whole
        # answer, which is also what c7b2106 always did.
        return WholeSecondDecision(0, 0, agrees, False, "zero")
    if not agrees:
        reason = "disagrees"
    elif not (float(marker_snr_db) >= float(min_snr_db)):
        # `not (x >= y)` and not `x < y`: a NaN SNR passes `<` (review
        # M1) and must not pass here.
        reason = "snr"
    elif abs(k_raw) > int(max_k):
        reason = "magnitude"
    elif confirmed_k is None or int(confirmed_k) != k_raw:
        reason = "unconfirmed"
    else:
        return WholeSecondDecision(k_raw, k_raw, agrees, False, "confirmed")
    return WholeSecondDecision(0, k_raw, agrees, True, reason)


@dataclass
class Registration:
    """The acquired origin, held in the RTP frame: utc(sample at rtp) =
    utc_ref + (rtp − rtp_ref) / sample_rate, valid within one counter epoch."""

    counter_epoch_id: str
    rtp_ref: int
    utc_ref: float
    sample_rate: int
    sigma_ms: float
    method: str = "fold+template"
    n_minutes: int = 0
    channel: str = ""
    hypotheses_open: int = 0
    stations: tuple = ()
    # task-11b: an acquired-but-unverified plane is a CANDIDATE, not yet
    # trusted -- ``_try_acquire``/``resolve_ambiguity`` always create one
    # with ``verified=False``; ``adopt`` copies whatever value the donor
    # carries (a fused plane built purely from already-ACQUIRED, hence
    # already-verified, siblings is verified).
    verified: bool = False
    # The counter epoch as a MEASURED quantity: UTC of RTP sample 0 under
    # radiod's pair (CounterEpochTracker.epoch_offset_s).  ``fuse_registrations``
    # clustered on this until Task 6 (final review, C1); it now converts
    # through ``counter_map`` instead, and this field stays as provenance.
    # NaN when the writer never saw a valid pair.
    epoch_offset_s: float = float("nan")
    # task 17b: the minute marker implied a non-zero whole second and a
    # gate refused it, so this plane's SECOND rests on the label frame
    # (radiod's host-stamped pair) rather than on the marker -- exactly
    # where it rested at c7b2106, when the marker search could not reach
    # a live minute at all.  Provenance, not a refusal: the fractional
    # correction is unaffected and the plane is as good as the pre-marker
    # one.  Published in the channel file and the station summary so the
    # offline analysis and the operator can both see which planes carry a
    # second nothing independent has confirmed.
    whole_second_unresolved: bool = False
    # Task 6: the counter ``rtp_ref`` lives in.  Since ka9q-radio e5f790dd
    # every radiod channel starts its own RTP counter (ND, 2026-10-06:
    # k0 = 51, 51, 51, 51, 50 on its 24 kHz channels, one channel 20 ms
    # off the rest), so a plane says nothing about another channel's RTP
    # until both counters are mapped to radiod's input-sample index.
    # ``rtp_domain`` names the channel whose counter ``rtp_ref`` is in;
    # empty means "this registration's own ``channel``", which is what
    # every acquirer before Task 7 wrote.  ``counter_map`` maps that
    # counter to the input-sample index; None when nobody measured it.
    counter_map: Optional[CounterMap] = None
    rtp_domain: str = ""

    @property
    def domain(self) -> str:
        """The channel whose counter ``rtp_ref`` is in: ``rtp_domain``, or
        this registration's own ``channel`` when the writer named none."""
        return self.rtp_domain or self.channel

    def in_domain(
        self, rtp: int, cmap: Optional[CounterMap], domain: str
    ) -> Optional["Registration"]:
        """This plane re-expressed at ``rtp`` in channel ``domain``'s counter,
        or None when nothing can relate the two counters.

        Same counter (``domain`` names this plane's own): the plain
        projection, ``sample0_utc_for``.  Another counter: through both
        counter maps, ``utc_ref + (cmap.input_index(rtp) -
        self.counter_map.input_index(rtp_ref)) / input_samprate``, and only
        when both maps exist and name one radiod run.  ``same_run``
        compares the maps' epochs alone, here within
        ``COUNTER_EPOCH_SKEW_ALLOWANCE_S`` (2 s): radiod's GPS_TIME can
        arrive up to 816 ms late, and ``RegistrationAcquirer`` keeps its own
        plane across such a shift, so a sibling's must convert across it
        too.  Runs hours apart stay apart.

        Channels at different output rates (24 kHz against 96 kHz) never
        convert: ``same_run`` also requires equal ``olen`` and
        ``input_samprate``, so the converted plane always ticks at this
        plane's own rate.

        Never raw RTP across channels: two channels' counters start at
        different blocks, and the difference reads as a timing offset."""
        if domain and domain == self.domain:
            return replace(
                self,
                rtp_ref=int(rtp) & 0xFFFFFFFF,
                utc_ref=self.sample0_utc_for(rtp),
                counter_map=cmap if cmap is not None else self.counter_map,
                rtp_domain=domain,
            )
        if cmap is None or self.counter_map is None:
            return None
        if not self.counter_map.same_run(cmap, tol_s=COUNTER_EPOCH_SKEW_ALLOWANCE_S):
            return None
        delta_in = cmap.input_index(rtp) - self.counter_map.input_index(self.rtp_ref)
        return replace(
            self,
            rtp_ref=int(rtp) & 0xFFFFFFFF,
            utc_ref=self.utc_ref + delta_in / float(cmap.input_samprate),
            counter_map=cmap,
            rtp_domain=domain,
        )

    def sample0_utc_for(self, start_rtp: int) -> float:
        """UTC of the sample at ``start_rtp``, across a counter wrap.

        The RTP counter holds 32 bits, so at 24 kHz it returns to zero every
        2**32/24000 = 178,956.97 s (49.7 h).  A raw subtraction reads that
        return as 49.7 hours of travel backwards: ND wrapped at ~11:00:56Z on
        2026-09-09 and four channels published a correction of -178,956.5 s
        while still reporting ACQUIRED and verified.  Take the difference in
        the counter's own modular arithmetic instead, exactly as
        ``buffer_timing._rtp_delta_signed``, ``ring_buffer_reader`` and
        ``native_anchor`` already do, and as ``test_t6_rtp_wrap_continuity``
        has required of the T6 anchor since before this plane existed.

        The signed reading assumes the two counter values lie within 2**31
        samples of each other -- 24.85 h at 24 kHz.  Every caller asks about
        the minute in hand against a registration the staleness gate keeps
        under 300 s, so that bound sits four orders of magnitude away.
        """
        delta = (int(start_rtp) - int(self.rtp_ref)) & 0xFFFFFFFF
        if delta > 0x7FFFFFFF:
            delta -= 0x100000000
        return self.utc_ref + delta / float(self.sample_rate)


@dataclass(frozen=True)
class MinuteArrival:
    """One station, this minute, measured on the acquirer's own held plane
    (Task 9, "T3 stands alone").

    ``residual_ms`` = fold peak position - expected delay, wrapped to
    ±500 ms, minus 1000 ms per whole second the minute marker confirmed.
    Its sign is that of ``TickEdgeDetector.timing_error_ms``: positive
    means the plane's labels run LATE, and ``corroborate`` subtracts it.
    ``arrival_rtp`` names the peak in the first full label second of the
    minute's audio, in this channel's own counter."""

    station: str
    residual_ms: float
    sigma_ms: float
    arrival_rtp: float
    fold_snr_db: float


def residuals_for_verify(
    arrivals: Dict[str, MinuteArrival],
) -> Dict[str, Tuple[float, float]]:
    """``measure_minute``'s answer in the shape ``verify`` and
    ``corroborate`` take: station -> (residual ms, sigma ms)."""
    return {s: (a.residual_ms, a.sigma_ms) for s, a in arrivals.items()}


# Task 9: the rolling self-measurement fold.  Up to three minutes per tone
# band, the same depth bootstrap folds to (FOLD_LENGTHS_S), so a channel
# that acquired on a 180 s fold can still see its station on the held plane.
SELF_FOLD_MINUTES = 3
# Run-up kept from the previous minute's audio so the marker search
# (±MARKER_SEARCH_HALF_S around the label minute) fits when the live ring
# hands over [minute, minute + 60 s).  2.5 s covers the 1.5 s half-window
# plus a margin for the band filter's edge.
SELF_RUNUP_S = 2.5
# How much of the previous minute's tail to keep: enough to find
# SELF_RUNUP_S before the next start even when consecutive buffers overlap.
SELF_TAIL_KEEP_S = 5.0
# Fix round 1: a residual past this is a whole second, never a tick
# timing error (a fold residual wraps to ±500 ms, a tick search spans
# ±20 ms).  ``verify`` and ``corroborate`` reset on it at once.  Task 10
# (ruling 1): the test is strictly greater than 900 ms.  A fold peak
# exactly half a second from its expected delay wraps to +500.0 ms, which
# is no whole second; a true whole second reads near 1000 ms.
WHOLE_SECOND_GUARD_MS = 900.0


class RegistrationAcquirer:
    """Acquire from the signal, hold on the ruler, corroborate or correct
    every minute (spec §2 rules 1-3)."""

    STATE_BOOTSTRAP = "BOOTSTRAP"
    STATE_ACQUIRED = "ACQUIRED"
    CORRECT_K_SIGMA = 3.0
    CORRECT_MINUTES = 2
    CORRECT_MIN_CHANNELS = 2  # here: stations on this channel's ensembles
    FILTER_MEMORY_MINUTES = 30
    # TickEdgeDetector.LABEL_ANCHOR_MAX_SIGMA_MS = 6.0: tick-like ensembles only.
    TIMING_SIGMA_MAX_MS = TickEdgeDetector.LABEL_ANCHOR_MAX_SIGMA_MS
    # task-11b: consecutive minutes with no ensemble at all for this
    # registration's stations (host-label-anchored minute, filter skipped
    # them, ...) before an unverified plane is given up on and reset.
    VERIFY_MAX_MINUTES = 3
    # task-11b fix round 2 (N1): sigma_single_ms alone does not discriminate
    # a marker-anchored ensemble -- that search sits on the signal's OWN
    # marker grid, so its sigma_1 stays tick-like (~0.01 ms) however wrong
    # our plane is; only ensemble_timing_error_ms carries the plane error
    # for that anchor.  Bound it to the same window an acquired-anchored
    # confirmation is implicitly held to (TickEdgeDetector.SEARCH_WINDOW_MS,
    # 20 ms: an acquired-anchored search outside that window returns window
    # scatter, sigma_1 >> TIMING_SIGMA_MAX_MS, and is already rejected on
    # sigma alone) so marker- and acquired-anchored evidence are held to
    # one equivalent standard.
    VERIFY_MAX_RESIDUAL_MS = TickEdgeDetector.SEARCH_WINDOW_MS

    def __init__(self, channel: str, sample_rate: int):
        self.channel = channel
        self.sample_rate = int(sample_rate)
        self._state = self.STATE_BOOTSTRAP
        self._reg: Optional[Registration] = None
        self._epoch: Optional[str] = None
        self._buf: List[Tuple[np.ndarray, float, int, int]] = (
            []
        )  # (audio, label_s0, start_rtp, minute)
        self._bad_minutes = 0
        self._open: List[Hypothesis] = []
        self._verify_pending = 0
        self._epoch_offset_s = float("nan")
        # task 17b: (minute_utc, k_raw) of the last attempt that found a
        # marker, and how many attempts have already been spent waiting
        # for a second consecutive minute to confirm a non-zero whole
        # second.  Both live only inside one BOOTSTRAP episode.
        self._marker_k_seen: Optional[Tuple[int, int]] = None
        self._marker_k_holds = 0
        # Task 10: the whole second ``_try_acquire`` added to the label it
        # acquired on (None for a plane adopted or resolved from a
        # sibling, or no plane), and the whole second a condemned plane's
        # marker says that label needs instead.  The second survives only
        # the whole-second guard's own reset: any other reset (a new
        # counter epoch, a map step, a failed verification) drops it.
        self._acq_k_int: Optional[int] = None
        self._overruling_second: Optional[int] = None
        # Task 7: this channel's counter map, as the recorder last
        # published it (``note_counter_map``).  It survives ``reset``: the
        # map describes the counter, not the plane.
        self._cmap: Optional[CounterMap] = None
        # Task 9: the self-measurement on the held plane.  All of it
        # belongs to ONE plane and clears with it (``_clear_self_measure``).
        # band -> [(minute, profile, rows, rtp, utc the plane gave that rtp)]
        self._fold_history: Dict[
            str, List[Tuple[int, np.ndarray, int, int, float]]
        ] = {}
        self._self_tail: Optional[Tuple[np.ndarray, int]] = None
        self._acq_minute: Optional[int] = None
        self._self_marker_k: Dict[str, Tuple[int, int]] = {}
        self._self_marker_logged: Dict[str, str] = {}

    def _clear_self_measure(self) -> None:
        """Forget every minute measured on the plane being retired: a fold
        history smeared across two planes would measure neither."""
        self._fold_history = {}
        self._self_tail = None
        self._acq_minute = None
        self._acq_k_int = None
        self._self_marker_k = {}
        self._self_marker_logged = {}

    @property
    def state(self) -> str:
        return self._state

    @property
    def registration(self) -> Optional[Registration]:
        return self._reg

    @property
    def counter_map(self) -> Optional[CounterMap]:
        """This channel's counter map as last noted; None before any."""
        return self._cmap

    def reset(self, why: str, *, loud: bool = False) -> None:
        """``loud``: a WARNING rather than INFO, for a reset that means the
        held plane was wrong (a counter-map step), not merely retired."""
        logger.log(
            logging.WARNING if loud else logging.INFO,
            f"[{self.channel}] registration reset -> BOOTSTRAP ({why})",
        )
        self._state = self.STATE_BOOTSTRAP
        self._reg = None
        self._buf.clear()
        self._bad_minutes = 0
        self._open.clear()
        self._verify_pending = 0
        self._marker_k_seen = None
        self._marker_k_holds = 0
        self._overruling_second = None
        self._clear_self_measure()

    def adopt(self, reg: Registration) -> None:
        """Adopt a sibling's (usually fused) plane as this channel's own.

        Stamped ``channel=self.channel`` (never the donor's -- a sibling
        fusion carries ``channel="fused"``, and writing that verbatim would
        make ``RegistrationStore`` file this channel's plane under
        ``fused.json`` instead of ``<channel>.json``, silently dropping this
        channel's own provenance) and ``method="adopted"`` so
        ``RegistrationStore.read_siblings`` can recognise and skip a purely
        derived plane -- otherwise an adopted echo of the fusion re-enters
        the next fusion as if it were independent corroborating evidence,
        understating sigma by sqrt(n_adopters+1) (T3 self-registration
        review, C1, 2026-09-06)."""
        self._reg = self._stamped(Registration(
            **{**reg.__dict__, "channel": self.channel, "method": "adopted"}
        ))
        self._epoch = reg.counter_epoch_id
        self._state = self.STATE_ACQUIRED
        self._buf.clear()
        self._bad_minutes = 0
        self._verify_pending = 0
        self._clear_self_measure()

    # ── counter domain (Task 7) ────────────────────────────────────
    def note_counter_map(self, cmap: Optional[CounterMap]) -> None:
        """Take this minute's counter map for this channel.

        Every plane this acquirer holds or creates lives in this channel's
        own RTP counter.  It carries ``rtp_domain=self.channel`` and this
        map, rolled forward to the plane's ``rtp_ref`` so that
        ``Registration.in_domain`` always reads the map within its 2**31
        sample reach.

        A new map must continue the held one, or the held plane is wrong
        and resets, loudly.  Continuity is decided from the counters, not
        from ``CounterMap.same_run``: ``same_run`` compares the maps'
        epochs to 0.5 s, and a late GPS_TIME packet (B4: up to 816 ms)
        moves an epoch that far on one radiod run.  So:

        * both maps must give the same input-sample index at a common RTP;
          a whole-block difference is a counter-map step on this run
          (``counter map moved N block(s)``), anything else another run;
        * the epochs must agree within ``COUNTER_EPOCH_SKEW_ALLOWANCE_S``
          (2 s, the tracker's own allowance for late pairs).  A channel
          that restarts at the same start block maps its counter exactly
          as before, and only the epoch, hours away, tells the runs apart.

        None from a producer that never sent a map changes nothing.  None
        after a map withdraws it: the recorder publishes None once its map
        has gone stale, and a map read past its reach converts every
        sibling about 49.7 h wrong.  The held plane keeps the map it was
        stamped with, which continuity already proved.

        A continuous map re-stamps the held plane only while the plane's
        ``rtp_ref`` lies within ``COUNTER_MAP_REACH`` of it.  ``in_domain``
        works on absolute input indices, so a map stamped once in reach
        stays exact for the plane's whole life; rolling the newest map
        back to an ever-older ``rtp_ref`` would not."""
        if cmap is None:
            if self._cmap is not None:
                logger.warning(
                    f"[{self.channel}] counter map withdrawn by the recorder; "
                    f"no sibling converts into this counter until one returns"
                )
                self._cmap = None
            return
        held = self._cmap
        if held is not None and _rtp_gap(cmap.rtp_ref, held.rtp_ref) > COUNTER_MAP_REACH:
            self._cmap = cmap
            self.reset(
                f"counter map {_rtp_gap(cmap.rtp_ref, held.rtp_ref)} samples from the "
                f"held one, past reach: continuity cannot be shown",
                loud=True,
            )
            return
        self._cmap = cmap
        if held is not None:
            why = self._counter_map_break(held, cmap)
            if why is not None:
                self.reset(why, loud=True)
                return
        if self._reg is not None:
            self._stamped(self._reg)

    @staticmethod
    def _counter_map_break(held: CounterMap, new: CounterMap) -> Optional[str]:
        """Why ``new`` does not continue ``held``, or None when it does."""
        geometry = (new.ilen, new.olen, new.input_samprate)
        if geometry != (held.ilen, held.olen, held.input_samprate):
            return (
                f"counter map geometry {(held.ilen, held.olen, held.input_samprate)} "
                f"-> {geometry}: another radiod run"
            )
        epoch_step = new.input_epoch_gps_s - held.input_epoch_gps_s
        if abs(epoch_step) > COUNTER_EPOCH_SKEW_ALLOWANCE_S:
            return f"counter map epoch moved {epoch_step:+.3f} s: another radiod run"
        at = new.rtp_ref
        blocks = (new.input_index(at) - held.input_index(at)) / new.ilen
        whole = round(blocks)
        if abs(blocks - whole) > 1e-6:
            return f"counter map moved {blocks:+.6f} blocks: another radiod run"
        if whole:
            n = abs(whole)
            return (
                f"counter map moved {n} block{'' if n == 1 else 's'} "
                f"({whole * new.ilen / new.input_samprate * 1000.0:+.1f} ms "
                f"at rtp {at}) on one radiod run"
            )
        return None

    def _stamped(self, reg: Registration) -> Registration:
        """``reg`` named in this channel's counter, with this channel's map
        rolled to its ``rtp_ref`` (Task 6 review: map reach).  A map out of
        reach of ``rtp_ref`` leaves the plane's existing map in place."""
        reg.rtp_domain = self.channel
        if (
            self._cmap is not None
            and _rtp_gap(reg.rtp_ref, self._cmap.rtp_ref) < COUNTER_MAP_REACH
        ):
            reg.counter_map = self._cmap.rolled_forward(reg.rtp_ref)
        return reg

    def map_at(self, rtp: int) -> Optional[CounterMap]:
        """This channel's map rolled to ``rtp``, for fusion's ``at_map``.

        The held map rolls forward with it, so it never ages past reach
        while minutes keep arriving.  None when no map is held, or when the
        held map lies more than ``COUNTER_MAP_REACH`` from ``rtp`` (a stale
        map the recorder failed to roll): no sibling converts then."""
        if self._cmap is None or _rtp_gap(rtp, self._cmap.rtp_ref) > COUNTER_MAP_REACH:
            return None
        self._cmap = self._cmap.rolled_forward(rtp)
        return self._cmap

    # ── acquisition ────────────────────────────────────────────────
    def offer_minute(
        self,
        audio: np.ndarray,
        label,
        start_rtp: int,
        minute_utc: int,
        expected_delays_s: Dict[str, float],
        counter_epoch_id: str,
        epoch_offset_s: float = float("nan"),
    ) -> Optional[Registration]:
        """``epoch_offset_s``: the counter epoch as a MEASURED quantity
        (``CounterEpochTracker.epoch_offset_s``).  The id string is
        per-process and cannot be compared across channels, so every
        registration carries this offset as well (final review, C1).  Since
        Task 6 fusion converts through counter maps rather than clustering
        on it.  NaN keeps the pre-C1 id-string behaviour."""
        if self._epoch is not None and counter_epoch_id != self._epoch:
            self.reset(f"counter epoch {self._epoch} -> {counter_epoch_id}")
        elif self._epoch_stepped(epoch_offset_s):
            # The id string is only ``ep-<int(offset_s)>`` (C1), so a
            # re-anchor that moves the mapping DOWN by 0.5-1.0 s opens a new
            # epoch in CounterEpochTracker while spelling it the same way.
            # The offset itself always sees it: within one epoch the tracker
            # reports a running MINIMUM, which moves by at most
            # COUNTER_EPOCH_STEP_S per observation.
            self.reset(
                f"counter epoch offset {self._epoch_offset_s:.3f} -> "
                f"{float(epoch_offset_s):.3f} s"
            )
        self._epoch = counter_epoch_id
        self._epoch_offset_s = float(epoch_offset_s)
        if self._state == self.STATE_ACQUIRED and self._reg is not None:
            return self._reg
        self._buf.append(
            (
                np.asarray(audio, dtype=np.float64),
                float(label.sample0_utc),
                int(start_rtp),
                int(minute_utc),
            )
        )
        self._buf = self._buf[-3:]
        return self._try_acquire(expected_delays_s)

    def _epoch_stepped(self, epoch_offset_s: float) -> bool:
        """Has the counter epoch's implied offset moved past the step?"""
        held = self._epoch_offset_s
        if math.isnan(held) or math.isnan(epoch_offset_s):
            return False
        return abs(float(epoch_offset_s) - held) > COUNTER_EPOCH_STEP_S

    def _try_acquire(
        self, expected_delays_s: Dict[str, float]
    ) -> Optional[Registration]:
        # Concatenate the buffered minutes in the frame of the OLDEST label;
        # each minute is re-labelled onto that frame through RTP, so a ring
        # anchor refresh between minutes cannot smear the fold.
        a0, s0, rtp0, _ = self._buf[0]
        pieces = []
        for audio, s_lbl, rtp, _ in self._buf:
            # gap/overlap between consecutive buffers, in samples, from RTP
            want = rtp - rtp0
            have = sum(len(p) for p in pieces)
            if want > have:
                pieces.append(np.zeros(want - have))
            elif want < have:
                audio = audio[have - want :]
            pieces.append(audio)
        audio_all = np.concatenate(pieces)
        del pieces
        n_sec = min(180, len(audio_all) // self.sample_rate)
        # I2: ONE envelope per band per attempt, in float32, and the raw
        # concatenation freed as soon as they exist.  Every fold of this
        # attempt -- the full one and both halves of the persistence gate --
        # comes off these two arrays; deriving each fold from the audio
        # again cost ~100 MB of live float64 per band and ~0.5 s of CPU.
        envelopes = band_envelopes(audio_all, self.sample_rate)
        del audio_all
        # fold EVERY band, even one with no eligible station: the leak of a
        # 1200 Hz tick into 900-1100 Hz is only recognisable by comparison
        best = [
            p
            for p in peaks_from_envelopes(envelopes, self.sample_rate, s0, n_sec)
            if any(BAND_OF_STATION.get(s) == p.band for s in expected_delays_s)
        ]
        # Task 16a: BPM may be READ off a peak but never registers, so
        # strip it before anything chooses a winner -- a WWV + BPM pair
        # becomes the WWV peak alone, ambiguous, waiting for the marker.
        hyps = strip_non_registering_stations(
            fit_template(best, expected_delays_s), expected_delays_s
        )
        self._open = [h for h in hyps if not h.unambiguous]
        winners = [h for h in hyps if h.unambiguous]
        # One marker search per tone band per attempt, off the SAME
        # envelopes the folds came from, over the newest buffered minute
        # whose ±MARKER_SEARCH_HALF_S window fits inside the concatenated
        # buffer.  The live ring hands us [minute, minute + 60 s), so the
        # first minute's own marker has no run-up ahead of it; from the
        # second buffered minute on there is.
        marker_cache: Dict[str, Optional[Tuple[float, float]]] = {}

        def marker_for_band(band: str) -> Optional[Tuple[float, float]]:
            if band not in marker_cache:
                found = None
                env = envelopes.get(band)
                if env is not None:
                    for _audio, _lbl, _rtp, minute_i in reversed(self._buf):
                        found = marker_in_envelope(
                            env, self.sample_rate, s0, int(minute_i)
                        )
                        if found is not None:
                            break
                marker_cache[band] = found
            return marker_cache[band]

        # Task 15 / spec §12: a shared channel folding ONE 1000 Hz peak
        # carries two hypotheses 34-37 ms apart (WWV or BPM) and no
        # sibling can choose between them when the dedicated channels hear
        # no tick -- AC0G-ND sat in BOOTSTRAP indefinitely that way on
        # 2026-09-07, starving the anchor closure.  The 800 ms minute
        # marker, which BPM does not transmit, names the station from this
        # channel's own signal.
        named = None
        if not winners and self._open:
            named = marker_names_one_hypothesis(
                self._open, expected_delays_s, marker_for_band
            )
        if not winners and named is None:
            logger.info(
                f"[{self.channel}] BOOTSTRAP: {n_sec} s folded, "
                f"{len(best)} peaks, {len(self._open)} ambiguous hypotheses "
                f"(no minute marker named one)"
            )
            return None
        h = named[0] if named is not None else winners[0]
        # Spec §10 (task-11b): the winning peak must recur in an independent
        # fold of each half of the buffer, not just the full-length fold --
        # otherwise a single-station channel promotes any lone fold-lattice
        # phantom to "unambiguous" by construction (a lone peak has no second
        # station to disagree with it), self-registering on noise with no
        # corroboration.
        if not _peak_persists_in_both_halves(envelopes, self.sample_rate, s0, n_sec, h):
            logger.info(
                f"[{self.channel}] BOOTSTRAP: winning peak at "
                f"{h.assignments[0][2] * 1000:.1f} ms did not recur in both halves"
            )
            return None
        # integer second from the minute marker, searched in the OLDEST
        # label's frame (every minute is re-labelled onto it through RTP)
        # so that a ring anchor refresh between minutes cannot shift the
        # whole-second answer
        k_int = 0
        whole_second_unresolved = False
        st0 = h.assignments[0][0]
        mk = marker_for_band(BAND_OF_STATION[st0])
        # I2: the two float32 envelopes go here, the last point that reads
        # them.  Cleared rather than `del`eted because ``marker_for_band``
        # closes over the name (and is not called again).
        envelopes.clear()
        if mk is not None:
            # Task 17b: bounded and UNWRAPPED (review W2).  The previous
            # test wrapped, so a marker a whole second from the fold
            # position "agreed" and moved the plane by a second -- and
            # nothing downstream could see it, because a plane wrong by
            # exactly 1 s leaves the 1 s-periodic tick search a residual
            # of ~0.
            minute_now = int(self._buf[-1][3])
            prev = self._marker_k_seen
            confirmed_k = (
                prev[1] if (prev is not None and minute_now - prev[0] == 60) else None
            )
            dec = whole_second_from_marker(
                mk[0],
                mk[1],
                expected_delays_s[st0],
                h.correction_s,
                confirmed_k=confirmed_k,
            )
            self._marker_k_seen = (minute_now, dec.k_raw)
            predicted_ms = (
                marker_position_s(expected_delays_s[st0], h.correction_s) * 1000.0
            )
            if dec.reason == "unconfirmed":
                # The ONE refusal a second minute can lift.  Waiting costs
                # the pre-marker behaviour for a minute; registering on one
                # minute's evidence risks a silent 1 s UTC error, which is
                # the one fault this system has no other detector for.
                if self._marker_k_holds < WHOLE_SECOND_HOLD_MAX_MINUTES:
                    self._marker_k_holds += 1
                    logger.warning(
                        f"[{self.channel}] BOOTSTRAP: minute marker at "
                        f"{mk[0] * 1000:+.1f} ms (SNR {mk[1]:.1f} dB) asks "
                        f"for a whole second {dec.k_raw:+d} s on {st0}'s "
                        f"ticks ({predicted_ms:+.1f} ms); waiting for a "
                        f"second consecutive minute to say the same "
                        f"({self._marker_k_holds}/"
                        f"{WHOLE_SECOND_HOLD_MAX_MINUTES})"
                    )
                    return None
                logger.warning(
                    f"[{self.channel}] minute marker asked for "
                    f"{dec.k_raw:+d} s and no second minute confirmed it "
                    f"in {self._marker_k_holds} attempts; registering with "
                    f"the whole second UNRESOLVED"
                )
            k_int = dec.k_int
            whole_second_unresolved = dec.unresolved
            if not dec.agrees:
                # Task 17 review I1.  Logged on DISAGREEMENT ALONE, never
                # on the whole second it happens to imply.
                #
                # 17b routed this branch through ``dec.reason``, and
                # ``whole_second_from_marker`` answers "zero" and returns
                # before it consults ``agrees`` -- so a marker anywhere
                # from about -418 ms to +580 ms of the predicted position
                # implied no whole second, took the "zero" path, and
                # logged NOTHING.  That window is exactly where review
                # C3's class (ii) lives: the ~37 ms WWV-vs-BPM misnaming
                # bounded "by the 800 ms-tone physics and by nothing
                # else".  17b closed the 1000 ms fault and blinded the
                # operator to the 37 ms one; under "expose, never
                # correct" that trade goes the wrong way.
                #
                # A marker that does not stand on this station's folded
                # ticks belongs to something else, and the disagreement is
                # the whole message -- whether it asks for a second (the
                # W2 case) or for none.
                logger.warning(
                    f"[{self.channel}] minute marker at {mk[0] * 1000:+.1f} "
                    f"ms disagrees with hypothesis {st0} (predicted "
                    f"{predicted_ms:+.1f} ms, SNR {mk[1]:.1f} dB); it "
                    f"implies {dec.k_raw:+d} s and gets none"
                    + (" — whole second UNRESOLVED" if dec.unresolved else "")
                )
            elif dec.unresolved:
                logger.warning(
                    f"[{self.channel}] minute marker implies {dec.k_raw:+d} "
                    f"s (SNR {mk[1]:.1f} dB); refused on {dec.reason} — "
                    f"whole second UNRESOLVED"
                )
            elif dec.k_int:
                logger.info(
                    f"[{self.channel}] minute marker moves the whole "
                    f"second by {dec.k_int:+d} s, confirmed on two "
                    f"consecutive minutes (SNR {mk[1]:.1f} dB)"
                )
        if mk is None and self._overruling_second is not None:
            # Task 10: no marker window fits this buffer, and the marker
            # already condemned a plane built on this label's second.  The
            # host may name the second only when no broadcast can; this
            # broadcast already did.
            k_int = self._overruling_second
            logger.info(
                f"[{self.channel}] whole second {k_int:+d} s from the minute "
                f"marker that condemned the previous plane; the raw label's "
                f"second stays overruled"
            )
        corr = h.correction_s + k_int
        if named is not None:
            # The marker RESOLVED the ambiguity: the hypotheses it did not
            # name are excluded, not still open (as on the
            # `resolve_ambiguity` path).
            self._open = []
        self._reg = Registration(
            counter_epoch_id=self._epoch or "unregistered",
            rtp_ref=rtp0,
            utc_ref=s0 + corr,
            sample_rate=self.sample_rate,
            sigma_ms=h.sigma_ms,
            method="fold+template+marker" if named is not None else "fold+template",
            channel=self.channel,
            hypotheses_open=len(self._open),
            stations=tuple(sorted({a[0] for a in h.assignments})),
            epoch_offset_s=self._epoch_offset_s,
            whole_second_unresolved=whole_second_unresolved,
        )
        self._stamped(self._reg)
        self._state = self.STATE_ACQUIRED
        self._bad_minutes = 0
        self._verify_pending = 0
        # Task 9: the minutes that built this plane must not also check it.
        self._clear_self_measure()
        self._acq_minute = max(int(m) for _a, _l, _r, m in self._buf)
        self._acq_k_int = int(k_int)
        # I2: the plane is held in the RTP frame from here on; three
        # promoted float64 minutes (35.7 MB) were kept for the life of the
        # process, and `offer_minute` short-circuits on ACQUIRED so they
        # could never be used again.  `reset` re-fills the buffer if the
        # plane is ever given up on.
        self._buf.clear()
        if named is not None:
            _h, m_band, m_offset, m_snr, m_excluded = named
            excluded = (
                f"{', '.join(m_excluded)} excluded: "
                if m_excluded
                else "no marker-less station survived the fit; "
            )
            logger.info(
                f"[{self.channel}] ACQUIRED: marker names {st0} "
                f"({excluded}800 ms tone found in the {m_band} band "
                f"at {m_offset * 1000:+.1f} ms, SNR {m_snr:.1f} dB)"
            )
        logger.info(
            f"[{self.channel}] ACQUIRED: correction {corr*1000:+.1f} ms "
            f"(int {k_int:+d} s), σ {h.sigma_ms:.2f} ms, support {h.support}, "
            f"stations {[a[0] for a in h.assignments]}, fold {n_sec} s"
        )
        return self._reg

    def resolve_ambiguity(
        self,
        sibling: Registration,
        start_rtp: int,
        label_s0: float,
        epoch_offset_s: float = float("nan"),
    ) -> Optional[Registration]:
        """A shared channel with one peak carries two or more hypotheses; a
        sibling channel's plane names the right one.  Same-site agreement
        is tight (SAME_SITE_AGREE_MS); cross-site looser (CROSS_SITE_AGREE_MS).
        The sibling also supplies the whole second."""
        if self._state == self.STATE_ACQUIRED or not self._open:
            return None
        if not math.isnan(epoch_offset_s):
            self._epoch_offset_s = float(epoch_offset_s)
        sib_s0 = sibling.sample0_utc_for(int(start_rtp))
        matches = []
        for h in self._open:
            st = h.assignments[0][0]
            tol = SAME_SITE_AGREE_MS if st in sibling.stations else CROSS_SITE_AGREE_MS
            frac = wrap_half_second((label_s0 + h.correction_s) - sib_s0)
            if abs(frac) * 1000.0 <= tol:
                matches.append((h, frac))
        if len(matches) != 1:
            return None
        h, frac = matches[0]
        # own fractional correction + the sibling's whole second
        s0 = sib_s0 + frac
        self._reg = Registration(
            counter_epoch_id=self._epoch or sibling.counter_epoch_id,
            rtp_ref=int(start_rtp),
            utc_ref=s0,
            sample_rate=self.sample_rate,
            sigma_ms=h.sigma_ms,
            channel=self.channel,
            hypotheses_open=0,
            stations=(h.assignments[0][0],),
            epoch_offset_s=(
                self._epoch_offset_s
                if not math.isnan(self._epoch_offset_s)
                else sibling.epoch_offset_s
            ),
        )
        self._stamped(self._reg)
        self._state = self.STATE_ACQUIRED
        self._open.clear()
        self._bad_minutes = 0
        self._verify_pending = 0
        self._clear_self_measure()
        if self._buf:
            # Task 9: the buffered minutes chose this hypothesis.
            self._acq_minute = max(int(m) for _a, _l, _r, m in self._buf)
        self._buf.clear()  # I2: see _try_acquire
        logger.info(
            f"[{self.channel}] ACQUIRED via sibling {sibling.channel}: "
            f"hypothesis {h.assignments[0][0]} agrees within {abs(frac)*1000:.2f} ms"
        )
        return self._reg

    # ── self-measurement (Task 9) ──────────────────────────────────
    def measure_minute(
        self,
        audio: np.ndarray,
        start_rtp: int,
        minute_utc: int,
        expected_delays_s: Dict[str, float],
    ) -> Dict[str, MinuteArrival]:
        """Measure the held plane against this channel's own ticks and
        minute marker, never against another tier's labels ("T3 stands
        alone", Task 9).

        Folds this minute's band envelopes on the held plane (``label =
        registration.sample0_utc_for(start_rtp)``), adds each band's profile
        to a rolling history of up to ``SELF_FOLD_MINUTES`` minutes, finds
        the peaks, and gives each station its peak position minus its
        expected delay, wrapped to ±500 ms.  A positive residual means the
        plane labels the tick late, the sign of
        ``TickEdgeDetector.timing_error_ms``, which ``corroborate``
        subtracts.

        Ticks repeat every second, so no fold can see a plane that is off
        by a whole second.  The minute marker, searched on the same plane,
        can.  A whole second the marker implies on two consecutive minutes,
        at ``MARKER_INT_SECOND_MIN_SNR_DB`` or better and no larger than
        ``MARKER_INT_SECOND_MAX_K``, belongs to the PLANE, not to the one
        station whose marker showed it: it becomes 1000 ms per second on
        EVERY station's residual this minute, so ``verify`` rejects and
        ``corroborate`` resets (both refuse any residual past
        ``WHOLE_SECOND_GUARD_MS`` outright).  Two marker stations that
        confirm different whole seconds leave nothing to report: the
        minute returns {} and logs one WARNING.  The marker never moves the
        plane here: expose, never correct.  The marker's sightings log at
        INFO, the confirming one included; the reset that follows in
        ``verify`` or ``corroborate`` carries the event's one WARNING
        (Task 10, ruling 3).

        The rolling fold stores each minute's profile with the UTC the
        plane gave its first sample, and rolls it onto the current plane
        before averaging, so a ``corroborate`` move shows in full on the
        next minute instead of blending with older profiles.

        Call order (Task 10): call this EVERY minute, after
        ``offer_minute``, with the same ``audio``, ``start_rtp`` and
        ``minute_utc``, the acquisition minute included.  That call returns
        {} but keeps the minute's audio tail, which the next minute's
        marker search needs as run-up ahead of its label minute.

        Returns {} while no plane is held, and for any minute at or before
        the last one the acquisition folded: those minutes built the plane
        and would only agree with it."""
        if self._reg is None or self._state != self.STATE_ACQUIRED:
            return {}
        m = int(minute_utc)
        sr = self.sample_rate
        audio = np.asarray(audio, dtype=np.float64)
        start_rtp = int(start_rtp) & 0xFFFFFFFF
        runup = self._runup_for(start_rtp)
        # Keep this minute's tail even when the minute itself goes
        # unmeasured, so the first measured minute has its marker run-up.
        keep = int(SELF_TAIL_KEEP_S * sr)
        tail = audio[-keep:].copy()
        self._self_tail = (tail, (start_rtp + len(audio) - len(tail)) & 0xFFFFFFFF)
        if self._acq_minute is not None and m <= self._acq_minute:
            return {}
        n_run = len(runup)
        full = np.concatenate([runup, audio]) if n_run else audio
        label0 = self._reg.sample0_utc_for((start_rtp - n_run) & 0xFFFFFFFF)
        envelopes = band_envelopes(full, sr)
        n_full = len(full)
        del full
        offset_s = n_run / sr
        n_seconds = len(audio) // sr

        # Fold EVERY band, as acquisition does: a 1200 Hz tick leaks into
        # the 1000 Hz band and only the comparison recognises the leak.
        by_band: Dict[str, List[FoldPeak]] = {}
        plane_utc = self._reg.sample0_utc_for(start_rtp)
        for band, env in envelopes.items():
            profile, rows = fold_envelope(env, sr, label0, n_seconds, offset_s)
            hist = [
                h
                for h in self._fold_history.get(band, [])
                if 0 < m - h[0] < SELF_FOLD_MINUTES * 60
            ]
            if rows:
                hist.append((m, profile, rows, start_rtp, plane_utc))
            self._fold_history[band] = hist
            total = sum(h[2] for h in hist)
            if total == 0:
                continue
            acc = np.zeros(sr, dtype=np.float64)
            for _m, prof, rows_h, rtp_h, utc_h in hist:
                # The plane now labels rtp_h `moved` seconds later than it
                # did when this profile was folded, so every tick in it now
                # sits that much later in the label second.
                moved = self._reg.sample0_utc_for(rtp_h) - utc_h
                acc += np.roll(prof, int(round(moved * sr))) * rows_h
            acc /= total
            by_band[band] = find_fold_peaks(acc, sr, band)
        peaks = arbitrate_bands(by_band)

        # Each peak reads as at most one station, nearest first, so a WWV
        # tick cannot also answer for BPM on a shared channel.  BPM takes
        # its own peak and then drops out (NON_REGISTERING_STATIONS).
        pairs = []
        for s, d in expected_delays_s.items():
            band = BAND_OF_STATION.get(s)
            if band is None:
                continue
            for i, p in enumerate(peaks):
                if p.band == band:
                    pairs.append((abs(wrap_half_second(p.position_s - d)), s, i))
        pairs.sort()
        chosen: Dict[str, FoldPeak] = {}
        used = set()
        for _dist, s, i in pairs:
            if s in chosen or i in used:
                continue
            chosen[s] = peaks[i]
            used.add(i)

        # The first full label second of THIS minute's audio, for arrival_rtp.
        lbl_audio0 = label0 + offset_s
        i0 = int(round((math.ceil(lbl_audio0) - lbl_audio0) * sr))
        fracs: Dict[str, float] = {}
        confirmed: Dict[str, int] = {}
        for s, p in chosen.items():
            if s in NON_REGISTERING_STATIONS:
                continue
            d = float(expected_delays_s[s])
            fracs[s] = wrap_half_second(p.position_s - d)
            k_s = self._confirmed_whole_second(
                s, envelopes.get(p.band), label0, n_full, m, d, fracs[s]
            )
            if k_s:
                confirmed[s] = k_s
        # A whole second belongs to the plane, not to one station: a
        # sibling station without a marker (WWVH at most US sites) would
        # otherwise read ~0 ms beside a +1000 ms station, ``verify`` would
        # pass on it, and ``corroborate`` would blend the two into the
        # plane (review, fix round 1, CRITICAL 1: 333 ms in one minute).
        if len(set(confirmed.values())) > 1:
            self._log_marker_once(
                "__plane__",
                "k_conflict",
                f"[{self.channel}] minute markers disagree on the whole "
                f"second: {', '.join(f'{s} {-k:+d} s' for s, k in sorted(confirmed.items()))}; "
                f"nothing measured this minute",
            )
            return {}
        k = next(iter(confirmed.values()), 0)
        if not confirmed:
            self._self_marker_logged.pop("__plane__", None)
        out: Dict[str, MinuteArrival] = {}
        for s, r_s in fracs.items():
            p = chosen[s]
            out[s] = MinuteArrival(
                station=s,
                residual_ms=r_s * 1000.0 - 1000.0 * k,
                sigma_ms=_sigma_ms_from_snr(p.snr_db),
                arrival_rtp=float((start_rtp + i0 + p.position_s * sr) % 2**32),
                fold_snr_db=p.snr_db,
            )
        return out

    def _runup_for(self, start_rtp: int) -> np.ndarray:
        """Up to ``SELF_RUNUP_S`` of the previous minute's audio that
        immediately precedes ``start_rtp`` in this channel's counter, or
        nothing when the previous minute does not reach it."""
        if self._self_tail is None:
            return np.zeros(0)
        tail, tail_start = self._self_tail
        d = (int(start_rtp) - int(tail_start)) & 0xFFFFFFFF
        if d >= 2**31:
            d -= 2**32
        if d <= 0 or d > len(tail):
            return np.zeros(0)
        return tail[max(0, d - int(SELF_RUNUP_S * self.sample_rate)) : d]

    def _confirmed_whole_second(
        self,
        station: str,
        env: Optional[np.ndarray],
        label0: float,
        n_samples: int,
        minute_utc: int,
        delay_s: float,
        residual_s: float,
    ) -> int:
        """The whole second the minute marker says the held plane sits
        off, once two consecutive minutes confirm it; else 0.

        ``whole_second_from_marker`` cannot serve here.  It guards
        ACQUISITION, where its unwrapped agreement test refuses a marker
        that stands a whole second from the fold, so that nothing moves
        the plane by a second.  On a held plane that marker is exactly
        the fault to expose.  So agreement here is fractional (the marker
        stands on this station's ticks), and the same SNR, magnitude and
        two-minute bars govern the whole second it implies."""
        if station not in MARKER_STATIONS or env is None:
            return 0
        sr = self.sample_rate
        first_min = int(math.ceil(label0 / 60.0)) * 60
        last_label = label0 + n_samples / sr
        candidates = [minute_utc] + [
            mm for mm in range(first_min, int(last_label) + 1, 60) if mm != minute_utc
        ]
        mk = None
        for mm in candidates:
            mk = marker_in_envelope(env, sr, label0, mm)
            if mk is not None:
                break
        if mk is None:
            return 0
        mk_off, mk_snr = mk
        predicted_s = delay_s + residual_s
        miss_ms = wrap_half_second(mk_off - predicted_s) * 1000.0
        if abs(miss_ms) > MARKER_HYPOTHESIS_AGREE_MS:
            self._self_marker_k.pop(station, None)
            self._log_marker_once(
                station,
                "off_ticks",
                f"[{self.channel}] minute marker at {mk_off * 1000:+.1f} ms "
                f"(SNR {mk_snr:.1f} dB) does not stand on {station}'s ticks "
                f"on the held plane ({predicted_s * 1000:+.1f} ms, "
                f"{miss_ms:+.1f} ms off); ignored, never applied",
            )
            return 0
        k = integer_second_correction(mk_off, delay_s, -residual_s)
        prev = self._self_marker_k.get(station)
        self._self_marker_k[station] = (minute_utc, k)
        if k == 0:
            self._self_marker_logged.pop(station, None)
            return 0
        confirmed = prev is not None and prev == (minute_utc - 60, k)
        if not (float(mk_snr) >= MARKER_INT_SECOND_MIN_SNR_DB):
            why = f"SNR {mk_snr:.1f} dB under {MARKER_INT_SECOND_MIN_SNR_DB:.0f} dB"
        elif abs(k) > MARKER_INT_SECOND_MAX_K:
            why = f"more than {MARKER_INT_SECOND_MAX_K} s"
        elif not confirmed:
            why = "awaiting a second consecutive minute"
        else:
            why = ""
        if why:
            # The first sighting, or a sighting the bars refuse: quiet.
            # Only the minute that condemns the plane is loud.
            self._log_marker_once(
                station,
                "whole_second_seen",
                f"[{self.channel}] minute marker at {mk_off * 1000:+.1f} ms "
                f"(SNR {mk_snr:.1f} dB) puts the held plane a whole second "
                f"off ({-k:+d} s) on {station}'s ticks; not yet reported "
                f"({why})",
                level=logging.INFO,
            )
            return 0
        # Task 10 (ruling 3): INFO.  The residual this returns trips the
        # whole-second guard in ``verify`` / ``corroborate``, whose reset
        # carries the event's one WARNING; this line gives the detail.
        self._log_marker_once(
            station,
            "whole_second",
            f"[{self.channel}] minute marker at {mk_off * 1000:+.1f} ms "
            f"(SNR {mk_snr:.1f} dB) confirms on two consecutive minutes "
            f"that the held plane sits a whole second off ({-k:+d} s) on "
            f"{station}'s ticks; reported as a {-1000 * k:+.0f} ms residual "
            f"on every station, never applied",
            level=logging.INFO,
        )
        return k

    def _log_marker_once(
        self, station: str, kind: str, msg: str, level: int = logging.WARNING
    ) -> None:
        """One message per disagreement episode (expose, loudly, once)."""
        if self._self_marker_logged.get(station) != kind:
            self._self_marker_logged[station] = kind
            logger.log(level, msg)

    def _whole_second_guard(self, residuals_ms: Dict[str, Tuple[float, float]]) -> bool:
        """Reset at once, loudly, on any registering station's residual
        past ``WHOLE_SECOND_GUARD_MS``.  No tick search or fold can produce
        one: only a whole second the minute marker confirmed can, and that
        condemns the plane
        whatever the other stations read.  Such a residual must never enter
        the mean (review, fix round 1, CRITICAL 1)."""
        if self._reg is None:
            return False
        # Task 10 (ruling 2): BPM may carry DUT1, up to 0.9 s, and never
        # registers, so its residual is no verdict on the plane.
        bad = {
            s: e
            for s, (e, _sig) in residuals_ms.items()
            if s not in NON_REGISTERING_STATIONS and abs(e) > WHOLE_SECOND_GUARD_MS
        }
        if not bad:
            return False
        # Task 10: a plane ``_try_acquire`` built on the label's second,
        # condemned by a whole second, tells what that label's second
        # should have been: the plane's own whole second less the one the
        # marker found.  ``measure_minute`` reports one whole second for
        # every station, so the stations agree; if they do not, keep none.
        fix = None
        whole = {int(round(e / 1000.0)) for e in bad.values()}
        if self._acq_k_int is not None and len(whole) == 1:
            fix = self._acq_k_int - whole.pop()
        self.reset(
            "held plane a whole second off: "
            + ", ".join(f"{s} {e:+.1f} ms" for s, e in sorted(bad.items())),
            loud=True,
        )
        self._overruling_second = fix
        return True

    # ── verification ───────────────────────────────────────────────
    def verify(self, residuals_ms: Dict[str, Tuple[float, float]]) -> str:
        """Confirm a freshly-acquired (CANDIDATE) plane against this
        channel's own ticks before it may be trusted as ACQUIRED (spec §10,
        task-11b).  Since Task 10 the service feeds it
        ``residuals_for_verify(measure_minute(...))`` only, never the tick
        detector's ensembles.

        ``residuals_ms``: same shape as ``corroborate`` -- station ->
        (timing error vs the acquired plane, per-tick sigma) -- for the
        stations measured this minute, restricted here to this
        registration's own stations.

        * A station in ``self._reg.stations`` with a tick-like ensemble
          (``sigma_single_ms <= TIMING_SIGMA_MAX_MS``) AND a small residual
          (``abs(timing_error_ms) <= VERIFY_MAX_RESIDUAL_MS``) confirms the
          plane: ``verified = True``, returns "verified".
        * A tick-like ensemble whose residual exceeds
          ``VERIFY_MAX_RESIDUAL_MS`` is itself a rejection, not "pending"
          (fix round 2, N1): a marker-anchored search sits on the signal's
          OWN grid, so its sigma stays tick-like however wrong our plane
          is -- only the residual can say the plane is wrong there, and it
          just did.  Reset to BOOTSTRAP, returns "rejected".
        * No tick-like ensemble at all for those stations: the acquired
          plane is a fold-lattice phantom, not a tick lock -- reset,
          returns "rejected".
        * No ensemble at all for those stations (host-label-anchored
          minute, filter skipped them, ...): keep waiting, returns
          "pending"; after ``VERIFY_MAX_MINUTES`` consecutive pending
          minutes, give up -- reset, returns "rejected".

        The service routes only a non-empty measurement here, so
        "pending" comes from a minute that measured some station outside
        ``self._reg.stations`` -- a sibling station heard while ours was
        not (final review, §3)."""
        if self._reg is None:
            return "pending"
        if self._whole_second_guard(residuals_ms):
            return "rejected"
        relevant = {s: r for s, r in residuals_ms.items() if s in self._reg.stations}
        if not relevant:
            self._verify_pending += 1
            if self._verify_pending >= self.VERIFY_MAX_MINUTES:
                self.reset(
                    f"acquired plane got no ensemble for {self._reg.stations} "
                    f"within {self.VERIFY_MAX_MINUTES} minutes"
                )
                return "rejected"
            return "pending"
        tick_like = [
            (err, sig)
            for err, sig in relevant.values()
            if sig <= self.TIMING_SIGMA_MAX_MS
        ]
        if not tick_like:
            self.reset("acquired plane failed fine-search verification")
            return "rejected"
        if any(abs(err) <= self.VERIFY_MAX_RESIDUAL_MS for err, _sig in tick_like):
            self._reg.verified = True
            self._verify_pending = 0
            return "verified"
        worst_err = max((err for err, _sig in tick_like), key=abs)
        self.reset(f"acquired plane failed verification: residual {worst_err:+.1f} ms")
        return "rejected"

    # ── corroboration ──────────────────────────────────────────────
    def corroborate(self, residuals_ms: Dict[str, Tuple[float, float]]) -> str:
        """``residuals_ms``: station -> (ensemble timing error vs the
        ACQUIRED plane, per-tick sigma).  Only tick-like ensembles
        (sigma ≤ TIMING_SIGMA_MAX_MS) count.

        An unverified (CANDIDATE) registration must pass ``verify`` first
        (task-11b); routed here rather than tightened blindly.

        ONE floor, ``ORIGIN_SIGMA_FLOOR_MS`` = 1 ms, governs the new
        evidence's weight, the accumulated estimate's weight, and the
        registration sigma itself (final review, C2 + I5).  That floor is
        the DELAY-MODEL ACCURACY bound, not a repeatability bound: this
        sigma comes from fold SNR and tick rise time divided by sqrt(n), so
        it measures how repeatably the ticks land, while the acquired
        origin's accuracy is bounded by ``expected_delays_s`` -- the
        great-circle/F2-hop propagation model, and mode ambiguity (1F vs 2F
        is milliseconds).  Publishing 0.17 ms for a quantity whose
        systematic floor sits several milliseconds up let the Offset
        Judge's same-tier sigma tie-break hand the PUBLISHED T3 offset to
        ``hf_acquired`` instead of ``FusionBench`` within the first hour on
        a live station -- a change of operative timing authority spec §6
        does not sanction.

        Flooring both weights equally also makes the filter what spec §5
        asks for: "on the GPSDO the true origin is constant, so the filter
        is a running weighted mean with a long memory, not a tracker".
        With ``w_new`` floored at 0.1 ms and ``w_old`` at 1.0 ms, a
        realistic 0.4 ms per-tick sigma under-weighted the history 6.25x
        per minute and the effective memory was about five minutes -- short
        enough to follow the ionosphere's path-delay wander into the
        origin, which is spec §10's last risk row."""
        if self._reg is None:
            return "held"
        if not self._reg.verified:
            return self.verify(residuals_ms)
        if self._whole_second_guard(residuals_ms):
            return "reacquire"
        # BPM never registers (Task 16a), and its ticks may carry DUT1 (Task
        # 10, ruling 2): its residual stays out of the mean as well.
        good = {
            s: r
            for s, r in residuals_ms.items()
            if r[1] <= self.TIMING_SIGMA_MAX_MS and s not in NON_REGISTERING_STATIONS
        }
        if not good:
            return "held"
        thr = self.CORRECT_K_SIGMA * max(self._reg.sigma_ms, ORIGIN_SIGMA_FLOOR_MS)
        far = [s for s, (e, _) in good.items() if abs(e) > thr]
        if len(far) >= self.CORRECT_MIN_CHANNELS or (len(good) == 1 and far):
            self._bad_minutes += 1
            if self._bad_minutes >= self.CORRECT_MINUTES:
                # Task 10 (ruling 3): the reset carries the event's one
                # WARNING; the service no longer adds its own.
                self.reset(
                    f"residual > {self.CORRECT_K_SIGMA}σ for "
                    f"{self.CORRECT_MINUTES} minutes on {far}",
                    loud=True,
                )
                return "reacquire"
            return "held"
        self._bad_minutes = 0
        # slow filter: running weighted mean with long memory (the true origin
        # is constant on the GPSDO; this is a smoother, not a tracker)
        w_new = sum(
            1.0 / max(sig, ORIGIN_SIGMA_FLOOR_MS) ** 2 for _, sig in good.values()
        )
        e_new = (
            sum(e / max(sig, ORIGIN_SIGMA_FLOOR_MS) ** 2 for e, sig in good.values())
            / w_new
        )
        n = min(self._reg.n_minutes, self.FILTER_MEMORY_MINUTES)
        w_old = (n / max(self._reg.sigma_ms, ORIGIN_SIGMA_FLOOR_MS) ** 2) if n else 0.0
        shift_ms = (w_new * e_new) / (w_new + w_old)
        # tick_edge_detector.timing_error_ms = front_edge - expected: a
        # POSITIVE residual means the plane's labels ran LATE (the edge
        # arrived after the plane said it would), so the plane must move
        # EARLIER to correct it -- subtract, don't add (T3 self-registration
        # review, C2, 2026-09-06: the prior `+=` doubled the error instead
        # of cancelling it).
        self._reg.utc_ref -= shift_ms / 1000.0
        self._reg.n_minutes += 1
        new_sigma = (
            1.0 / np.sqrt(w_new + w_old) if (w_new + w_old) > 0 else self._reg.sigma_ms
        )
        self._reg.sigma_ms = float(
            max(ORIGIN_SIGMA_FLOOR_MS, min(self._reg.sigma_ms, new_sigma))
        )
        return "tightened"
