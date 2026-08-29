"""Receiver operating-point probe for the timing provenance record.

radiod's RX888 driver runs its own AGC (``agc_rx888`` in ``rx888.c``),
re-adjusting the AD8370 analog gain once per second from the **total**
0-64.8 MHz A/D power.  That total is dominated by shortwave broadcast
below ~15 MHz — nowhere near a timing pilot — so the pilot's analog
operating point is set by unrelated spectrum, on a diurnal cycle.

radiod digitally undoes the level change (``scale_AD``), so the recorded
signal level is unchanged; the noise floor beneath it is not.  Measured
on B4 2026-08-28 across one evening as the AGC walked +11.9 dB down to
-4.2 dB: the T6 channel's baseband power held within 0.6 dB while its
noise density rose 3.6 dB — **0.52 dB of C/N0 lost per dB of gain**.
Per-edge timing scatter goes as 1/sqrt(SNR), so that is a real and
unrecorded term in the T6 uncertainty budget.

This probe samples the operating point once per authority tick and hands
it to the snapshot, so a T6 residual can be attributed to it afterwards.

Contract: **best effort, never raises.**  Any failure returns ``{}`` and
the affected columns simply stay NULL.  A radiod hiccup must never stall
or fail a timing-authority tick.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = ['FrontendProbe', 'SsrcByFrequency']


class FrontendProbe:
    """Samples the front-end operating point via one radiod status poll.

    Args:
        control_factory: zero-arg callable returning a ka9q
            ``RadiodControl``-alike exposing
            ``poll_status(ssrc, timeout=...) -> ChannelStatus | None``.
            Called once, on first sample, and cached — ``RadiodControl``
            resolves the radiod mDNS name in its constructor, so building
            one eagerly would make merely *constructing* an
            AuthorityManager touch the network.
        resolve_ssrc: zero-arg callable returning the T6 channel's SSRC,
            or ``None`` if it cannot be resolved yet.  Resolved lazily and
            cached on first success — discovery is a multi-second
            multicast listen, too costly to repeat every tick.  The
            frontend state rides on *every* channel's status packet, but
            polling the T6 channel returns the pilot's own levels in the
            same exchange.
        poll_timeout: seconds to wait for the status reply.
    """

    def __init__(
        self,
        control_factory: Callable[[], object],
        resolve_ssrc: Callable[[], Optional[int]],
        poll_timeout: float = 2.0,
    ):
        self._control_factory = control_factory
        self._resolve_ssrc = resolve_ssrc
        self._poll_timeout = float(poll_timeout)
        self._control = None
        self._ssrc: Optional[int] = None

    def sample(self) -> Dict[str, float]:
        """Return the operating point as snapshot columns, or ``{}``.

        Absent fields are omitted rather than set to ``None`` so a
        partial status still records what did arrive.
        """
        try:
            ssrc = self._ssrc
            if ssrc is None:
                resolved = self._resolve_ssrc()
                if resolved is None:
                    # Not cached: the channel may simply not exist yet.
                    return {}
                ssrc = self._ssrc = int(resolved)

            if self._control is None:
                self._control = self._control_factory()

            status = self._control.poll_status(ssrc, timeout=self._poll_timeout)
            if status is None:
                return {}

            out: Dict[str, float] = {}

            def put(column, value, cast=float):
                if value is not None:
                    out[column] = cast(value)

            frontend = getattr(status, 'frontend', None)
            if frontend is not None:
                put('rf_gain', getattr(frontend, 'rf_gain', None))
                put('rf_agc', getattr(frontend, 'rf_agc', None), int)
                put('if_power', getattr(frontend, 'if_power', None))
            # ka9q-python calls radiod's [47] N0 tag ``noise_density``.
            put('t6_baseband_power', getattr(status, 'baseband_power', None))
            put('t6_n0', getattr(status, 'noise_density', None))
            return out
        except Exception:
            logger.debug("front-end probe failed", exc_info=True)
            return {}


class SsrcByFrequency:
    """Resolve a channel's SSRC by its tuned frequency, via discovery.

    ⛔ Fleet invariant: radiod **hash-assigns** SSRCs — an SSRC is never
    computable from a frequency, and a poll of a nonexistent SSRC still
    answers (with defaults plus live frontend state), so a wrong guess
    fails open and looks like it worked.  Resolution is by enumeration
    only.

    Args:
        status_address: radiod status multicast name or address.
        frequency_hz: the channel's tuned frequency.
        discover: enumeration callable, for tests; defaults to
            ``ka9q.discover_channels``.
        tolerance_hz: match window, to absorb float round-tripping.
    """

    def __init__(
        self,
        status_address: str,
        frequency_hz: float,
        discover: Optional[Callable[[str], dict]] = None,
        tolerance_hz: float = 1.0,
    ):
        self._status_address = str(status_address)
        self._frequency_hz = float(frequency_hz)
        self._discover = discover
        self._tolerance_hz = float(tolerance_hz)

    def __call__(self) -> Optional[int]:
        discover = self._discover
        if discover is None:
            from ka9q import discover_channels
            discover = discover_channels
        try:
            channels = discover(self._status_address) or {}
        except Exception:
            logger.debug("channel discovery failed", exc_info=True)
            return None
        for ssrc, info in channels.items():
            freq = getattr(info, 'frequency', None)
            if freq is None:
                continue
            if abs(float(freq) - self._frequency_hz) <= self._tolerance_hz:
                return int(ssrc)
        logger.debug(
            "no channel at %.0f Hz among %d discovered",
            self._frequency_hz, len(channels),
        )
        return None
