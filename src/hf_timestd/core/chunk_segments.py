"""Chunk file names and the segments of a reopened chunk.

The archive writer never replaces a chunk already on disk.  A chunk reopened at
the same boundary (a recorder restart mid-chunk, a pair step over 0.25 s) is
written as a segment, ``<boundary>.seg<N>`` (``BinaryArchiveWriter._free_chunk_stem``).
Each segment spans the whole chunk from its boundary and places every sample
by its own pair, so position i in any segment means boundary + i/rate under
that segment's labels.  ``join_segments`` lays them over one another by
position.  Where two segments both wrote a position it keeps the earlier one,
as the writer does inside a chunk.  When the segments' labels disagree it
reports the disagreement (``start_rtp_offset_samples``) and corrects nothing.
"""
import re
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

_NAME = re.compile(r"^(\d+)(?:\.seg(\d+))?(\.bin(?:\.zst|\.lz4)?|\.json)$")


def parse_chunk_name(name: str) -> Optional[Tuple[int, int, str]]:
    """(boundary, segment, extension) for a chunk file name, else None.

    The first file at a boundary is segment 1 and has no ``.seg`` part."""
    m = _NAME.match(name)
    if m is None:
        return None
    return int(m.group(1)), int(m.group(2) or 1), m.group(3)


def chunk_stem(boundary: int, segment: int) -> str:
    """The file stem of a boundary's segment, as the writer names it."""
    return str(boundary) if segment == 1 else f"{boundary}.seg{segment}"


def covered_mask(samples: np.ndarray, sidecar: Optional[Dict]) -> np.ndarray:
    """The positions a segment wrote.

    The sidecar's ``covered_runs`` name them.  A sidecar written before the
    field existed leaves only the samples themselves: unwritten positions in a
    chunk hold zeros, so the nonzero samples stand in for the written ones."""
    runs = (sidecar or {}).get('covered_runs')
    if runs is None:
        return samples != 0
    mask = np.zeros(len(samples), dtype=bool)
    for a, b in runs:
        mask[max(0, int(a)):min(len(samples), int(b))] = True
    return mask


def _rtp_offset(rtp: Optional[int], first: Optional[int]) -> Optional[int]:
    if rtp is None or first is None:
        return None
    d = (int(rtp) - int(first)) & 0xFFFFFFFF
    return d - (1 << 32) if d >= (1 << 31) else d


def segment_summary(segment: int, sidecar: Optional[Dict],
                    first_rtp: Optional[int]) -> Dict:
    """What a joined chunk's metadata says about one of its segments."""
    side = sidecar or {}
    return {
        'segment': segment,
        'samples_written': side.get('samples_written'),
        'samples_covered': side.get('samples_covered'),
        'start_rtp_timestamp': side.get('start_rtp_timestamp'),
        'start_rtp_offset_samples': _rtp_offset(side.get('start_rtp_timestamp'), first_rtp),
    }


def join_metadata(sidecars: Sequence[Tuple[int, Optional[Dict]]]) -> Optional[Dict]:
    """The earliest segment's sidecar, plus a ``segments`` list when there are more."""
    if not sidecars:
        return None
    sidecars = sorted(sidecars, key=lambda p: p[0])
    first = sidecars[0][1]
    if len(sidecars) == 1:
        return first
    meta = dict(first or {})
    first_rtp = meta.get('start_rtp_timestamp')
    meta['segments'] = [segment_summary(n, s, first_rtp) for n, s in sidecars]
    return meta


class SegmentJoiner:
    """Join one boundary's segments, added one at a time in segment order.

    It holds the joined chunk and nothing else: the first segment's array
    becomes the joined chunk when it can be written to, so a caller that
    drops each array after ``add`` holds about two segments at the peak.
    ``result`` gives one segment back as it is.  For more, the result runs to
    the end of the last position taken; ``samples_written``, ``samples_covered``
    and ``hole_samples`` describe the joined chunk, and ``segments`` lists each
    one with the samples taken from it and refused from it.  A segment at a
    different sample rate from the earliest is skipped, and its entry says why.
    """

    def __init__(self) -> None:
        self._first: Optional[Tuple[int, np.ndarray, Optional[Dict]]] = None
        self._out: Optional[np.ndarray] = None
        self._filled: Optional[np.ndarray] = None
        self._meta: Optional[Dict] = None
        self._first_len = 0

    def add(self, segment: int, samples: np.ndarray, sidecar: Optional[Dict]) -> None:
        if self._first is None and self._meta is None:
            self._first = (segment, samples, sidecar)
            return
        if self._meta is None:
            fseg, fx, fside = self._first
            self._first = None
            self._meta = dict(fside or {})
            self._meta['segments'] = []
            self._first_len = len(fx)
            self._out = fx if fx.flags.writeable else fx.copy()
            self._filled = np.zeros(len(fx), dtype=bool)
            self._merge(fseg, fx, fside)
            del fx
        self._merge(segment, samples, sidecar)

    def _merge(self, segment: int, x: np.ndarray, side: Optional[Dict]) -> None:
        meta = self._meta
        summary = segment_summary(segment, side, meta.get('start_rtp_timestamp'))
        meta['segments'].append(summary)
        rate, seg_rate = meta.get('sample_rate'), (side or {}).get('sample_rate')
        if rate is not None and seg_rate is not None and int(seg_rate) != int(rate):
            summary['skipped'] = f"sample_rate {seg_rate} != {rate}"
            summary['samples_taken'] = summary['samples_refused'] = 0
            return
        if len(x) > len(self._out):
            grow = len(x) - len(self._out)
            self._out = np.concatenate([self._out, np.zeros(grow, dtype=np.complex64)])
            self._filled = np.concatenate([self._filled, np.zeros(grow, dtype=bool)])
        wrote = covered_mask(x, side)
        take = wrote & ~self._filled[:len(x)]
        if x is not self._out:
            self._out[:len(x)][take] = x[take]
        self._filled[:len(x)] |= take
        summary['samples_taken'] = int(take.sum())
        summary['samples_refused'] = int(wrote.sum()) - summary['samples_taken']

    def result(self) -> Tuple[Optional[np.ndarray], Optional[Dict]]:
        if self._meta is None:
            if self._first is None:
                return None, None
            return self._first[1], self._first[2]
        filled = self._filled
        last = int(np.flatnonzero(filled)[-1]) + 1 if filled.any() else 0
        # The longest segment sets the length only where something was taken from it.
        out = self._out[:max(last, self._first_len)]
        covered = int(filled[:len(out)].sum())
        meta = self._meta
        meta['samples_written'] = len(out)
        meta['samples_covered'] = covered
        meta['hole_samples'] = len(out) - covered
        return out, meta


def join_segments(parts: Sequence[Tuple[int, np.ndarray, Optional[Dict]]]
                  ) -> Tuple[Optional[np.ndarray], Optional[Dict]]:
    """Join one boundary's segments, given as (segment, samples, sidecar).

    See SegmentJoiner, which this feeds in segment order."""
    joiner = SegmentJoiner()
    for segment, samples, sidecar in sorted(parts, key=lambda p: p[0]):
        joiner.add(segment, samples, sidecar)
    return joiner.result()
