"""Bootstrap edge locator for a folded BPSK PPS second.

The fine stage's coherent fold produces a second that is ``-A`` on
``[0, e)`` and ``+A`` on ``[e, p)`` — one interior transition, at the
edge (measured; spec §3.2).  The 2A wrap discontinuity lies between
index ``p-1`` and ``0``, outside the linear array, and is never an
interior feature.

With one free parameter the matched filter has a closed form::

    T(e) = sum_{j>=e} x[j] - sum_{j<e} x[j] = C[p-1] - 2*C[e-1]

where ``C = cumsum(x)``.  ``argmax |T|`` is the estimate; the magnitude
because the global polarity is set by an arbitrary sign-alternation
phase.  O(p), no window parameter, nothing to tune.

⛔ Do NOT substitute a plain CUSUM (``argmax |cumsum(x - mean)|``).  It
is exact for a mid-second edge but structurally biased by 1-40 ms when
the edge lands near the fold origin — its tent collapses when the two
segments are unbalanced.  The bias is identical at every C/N0 and does
not self-correct: ``registration`` advances by exactly
``fold_seconds * p`` per block, so a stream that registers into that
zone stays there for the life of the lock.

⛔ Do NOT roll the array to "centre" the edge.  Rolling moves the wrap
discontinuity into the interior and creates a second, equally strong
candidate.
"""
from __future__ import annotations

import numpy as np

__all__ = ['bootstrap_edge_index']


def bootstrap_edge_index(in_phase: np.ndarray) -> int:
    """Index of the polarity transition in a derotated folded second.

    Args:
        in_phase: real folded second, length >= 2.

    Returns:
        Index in ``[0, len(in_phase))``.

    Raises:
        ValueError: if the input is too short to contain an interior edge.
    """
    x = np.asarray(in_phase, dtype=np.float64)
    if x.ndim != 1 or x.size < 2:
        raise ValueError(
            f"in_phase must be a 1-D array of at least 2 samples, "
            f"got shape {x.shape}"
        )
    cumulative = np.cumsum(x)
    # T[e] for e in [0, p): total minus twice the sum strictly below e.
    t = cumulative[-1] - 2.0 * np.concatenate(([0.0], cumulative[:-1]))
    return int(np.argmax(np.abs(t)))
