"""hf-timestd-native RTP→UTC anchor.

The matched-filter detection of the BPSK-injected GPS PPS in the HF
stream tells us the RTP sample at which a 1-PPS edge fired, to
sub-µs precision.  The LB-1421 USB-NMEA tells us which UTC second
that PPS belongs to, to USB-jitter precision.  Pairing the two ONCE
at first lock yields an anchor

    (anchor_rtp, anchor_utc_ns, sample_rate_hz)

from which every subsequent sample's UTC label is pure arithmetic:

    utc_ns(rtp) = anchor_utc_ns + (rtp − anchor_rtp) × 10⁹ / sample_rate_hz

The GPSDO disciplines ``sample_rate_hz`` exactly, so the arithmetic
stays accurate for as long as GPSDO lock holds — no continuous
"drift correction" against a host-clock-derived projection, no
re-anchoring against radiod's (gps_time, rtp_timesnap), no chrony
state in the loop.  This is the substrate position the architecture
docs describe (see ``docs/ARCHITECTURE-FIRST-PRINCIPLES.md`` §1 and
``docs/TIMING-PIPELINE-WIRING.md`` §1).

The 32-bit RTP-wrap disambiguation logic is lifted from
``ka9q.rtp_recorder.rtp_to_wallclock`` (the math is correct there;
only the *anchor* needs to come from a non-host-clock-poisoned
source).  Unlike ``rtp_to_wallclock`` we deliberately do NOT fall
back to ``time.time()`` when the wrap epoch is ambiguous — the
caller passes an explicit hint or accepts the anchor's own captured
moment as the disambiguator.  Host clock is never consulted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Mirror ka9q's constants so the wrap-epoch math is bit-identical to
# the existing rtp_to_wallclock implementation.
_BILLION = 1_000_000_000
_WRAP_PERIOD_SAMPLES = 0x1_0000_0000  # 2**32


@dataclass(frozen=True)
class NativeAnchor:
    """A captured (RTP, UTC) pairing for one channel.

    The anchor labels a *specific* RTP sample with its absolute UTC
    instant.  ``anchor_utc_ns`` is the UTC of the SAMPLE at
    ``anchor_rtp`` — i.e. the PPS firing UTC PLUS the analog RF
    chain delay (``chain_delay_ns``), so the projection from
    ``anchor_rtp`` reaches arbitrary RTP samples by pure sample-
    counting and the chrony-facing reference_time can be derived as
    ``anchor_utc_ns − chain_delay_ns`` on the same edge.

    Frozen by construction.  An anchor is either valid as-is or
    invalidated and re-captured at the next first-lock.  No mutate-
    in-place "drift correction".
    """

    anchor_rtp: int                # 32-bit RTP timestamp of the captured sample
    anchor_utc_ns: int             # UTC ns of that sample (Unix epoch)
    sample_rate_hz: int            # GPSDO-disciplined sample rate
    chain_delay_ns: int            # RF path delay at capture (PPS_UTC = anchor_utc_ns − chain_delay_ns)
    captured_at_utc_ns: int        # Provenance: the integer UTC second of the paired NMEA RMC × 1e9
    captured_via_tier: str         # "T5" | "T4" | "T3" — which tier authorised the integer-second

    def wrap_period_ns(self) -> int:
        """One full 32-bit RTP wrap period, in ns at this sample rate.

        At typical sample rates this is many hours, so a single anchor
        survives indefinitely without ever crossing the wrap boundary
        in normal operation.
        """
        return _BILLION * _WRAP_PERIOD_SAMPLES // self.sample_rate_hz

    def to_json(self) -> dict:
        """Serialise to the schema-v2 ChainDelayStore overlay."""
        return {
            "anchor_rtp": int(self.anchor_rtp) & 0xFFFFFFFF,
            "anchor_utc_ns": int(self.anchor_utc_ns),
            "sample_rate_hz": int(self.sample_rate_hz),
            "chain_delay_ns": int(self.chain_delay_ns),
            "captured_at_utc_ns": int(self.captured_at_utc_ns),
            "captured_via_tier": str(self.captured_via_tier),
        }

    @classmethod
    def from_json(cls, data: dict) -> "NativeAnchor":
        return cls(
            anchor_rtp=int(data["anchor_rtp"]) & 0xFFFFFFFF,
            anchor_utc_ns=int(data["anchor_utc_ns"]),
            sample_rate_hz=int(data["sample_rate_hz"]),
            chain_delay_ns=int(data["chain_delay_ns"]),
            captured_at_utc_ns=int(data["captured_at_utc_ns"]),
            captured_via_tier=str(data["captured_via_tier"]),
        )


def utc_ns_at_rtp(
    rtp: int,
    anchor: NativeAnchor,
    *,
    hint_utc_ns: Optional[int] = None,
) -> int:
    """Convert an RTP timestamp to UTC nanoseconds against ``anchor``.

    Pure function.  Does NOT consult the host wall clock.

    The 32-bit RTP-wrap disambiguation picks the wrap-epoch count
    ``k`` (full 2**32-sample periods elapsed since the anchor) that
    places the projected UTC closest to ``hint_utc_ns``.  If no
    hint is given the anchor's own ``captured_at_utc_ns`` is used,
    which is correct so long as the caller is within ±wrap_period/2
    of the anchor (many hours at typical sample rates).
    """
    sr = anchor.sample_rate_hz
    period_ns = anchor.wrap_period_ns()
    # Signed 32-bit delta, identical to ka9q.rtp_to_wallclock:201.
    rtp_delta_unsigned = (int(rtp) - int(anchor.anchor_rtp)) & 0xFFFFFFFF
    if rtp_delta_unsigned > 0x7FFFFFFF:
        rtp_delta_signed = rtp_delta_unsigned - 0x1_0000_0000
    else:
        rtp_delta_signed = rtp_delta_unsigned
    base_utc_ns = anchor.anchor_utc_ns + _BILLION * rtp_delta_signed // sr

    # Wrap-epoch picker — same logic as ka9q.rtp_to_wallclock:213-223,
    # but with the hint coming from the anchor's own provenance rather
    # than time.time().  The hint only matters when the caller has
    # been alive long enough to cross a wrap boundary (≥6 h at 96 kHz);
    # the default keeps the science path purely substrate-anchored.
    if hint_utc_ns is None:
        ref_utc_ns = anchor.captured_at_utc_ns
    else:
        ref_utc_ns = int(hint_utc_ns)
    diff_ns = ref_utc_ns - base_utc_ns
    if period_ns > 0:
        # Round-to-nearest of diff_ns / period_ns; biased-floor.
        if diff_ns >= 0:
            k = (diff_ns + period_ns // 2) // period_ns
        else:
            k = -(((-diff_ns) + period_ns // 2) // period_ns)
    else:
        k = 0
    return base_utc_ns + k * period_ns


def pps_firing_utc_ns(anchor: NativeAnchor) -> int:
    """Convenience: the UTC of the PPS edge that the anchor was
    captured against (= integer GPS second), back-derived by
    subtracting the analog chain delay.

    Used by the chrony SHM push to populate ``reference_time``
    consistently with the historical ``round(wall_time_sec)``
    semantics.
    """
    return anchor.anchor_utc_ns - anchor.chain_delay_ns
