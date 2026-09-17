"""Where the TS-1's BPSK PPS lands in the sampled spectrum.

The TS-1 transmits at a fixed RF frequency (84.225 MHz on the fleet
standard).  The RX888 samples the whole 0-fs/2 band, so what radiod can
tune is not that frequency but its **alias** under the ADC clock.  Get
the alias wrong and T6 creates a channel over empty spectrum: every step
succeeds, the detector simply never locks, and nothing says why.

Two facts decide the alias, and this module's whole job is to get both
from the machine rather than from a person:

``TS1_TX_HZ``
    The injector reports it over its USB console (``scripts/ts1-probe.sh``).

``input_samprate``
    radiod reports it in the front-end block of **every** status packet
    (``INPUT_SAMPRATE``, status tag 10, ``st.frontend.input_samprate``).

## Why radiod's status and not the config file

``setup-station.sh`` used to ask the operator for the ADC rate and fall
back to ``129600000`` unasked.  Three ways that goes wrong:

1. A 64.8 Msps site that skips the prompt silently gets the 129.6 Msps
   answer, tunes 45.375 MHz instead of 19.425 MHz, and never locks.
2. ``radiod@<instance>.conf`` is ambiguous: AC0G-B4's carries
   ``samprate = 12000`` (the channel output rate) at line 17 and
   ``samprate = 129600000`` (the RX888) at line 38.  A naive parse takes
   the first and is off by four orders of magnitude.
3. **radiod need not be on this host at all.**  Then there is no config
   file to read, while the status multicast still answers.

The status stream is authoritative, live, unambiguous about input versus
output rate, and identical whether radiod is local or across the LAN.

⚠ Status reaching you does not mean *samples* will.  A radiod running
``ttl=0`` (AC0G-B4 does) answers status while its IQ stays on loopback:
"Radiod reporting TTL=0 ... Multicast data restricted to localhost
loopback only!".  Deriving the frequency from a remote radiod is
therefore necessary but not sufficient for a remote T6 — that also needs
``ttl>0``, which is a deliberate site decision, not something to infer.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    'nyquist_alias_hz',
    'adc_rate_from_status',
    'resolve_t6_frequency_hz',
    'DEFAULT_ADC_HZ',
    'FLEET_TS1_TX_HZ',
]

#: What the RX888 runs at on the fleet standard. A fallback, never a fact:
#: 64_800_000 is equally supported and yields a different channel.
DEFAULT_ADC_HZ = 129_600_000

#: The TS-1's default transmit frequency, from the designer's own
#: documentation (P. Elliott WB6CXC, TS-1 TimeSync injector mode).
#:
#: ⛔ **Do not treat this as fixed, and do not offer to change it lightly.**
#: Elliott: "Other output frequencies can be configured to suit other sample
#: rates" — so read ``TS1_TX_HZ`` from the injector (``scripts/ts1-probe.sh``)
#: rather than assuming. This constant exists for documentation and tests.
#:
#: And 84.225 MHz is not an arbitrary default. Per the same document it was
#: "chosen to both minimize signal re-transmission through the antenna, and
#: to place any aliased harmonic frequencies as far as possible from any
#: 'interesting' bands", achieving "no closer than 0.475 MHz (including first
#: five aliased harmonics)". Re-planning that is a band-allocation exercise,
#: not a config tweak.
FLEET_TS1_TX_HZ = 84_225_000

#: The two aliases the designer publishes, keyed by ADC rate. Kept as data
#: so :func:`nyquist_alias_hz` can be checked against the source of truth
#: rather than against itself.
DESIGNER_ALIASES_HZ = {
    64_800_000: 19_425_000,
    129_600_000: 45_375_000,
}

#: Injected level in TimeSync mode, from the designer: the TS-1 emits a
#: "low-amplitude signal", bandpass-filtered at 84 MHz and attenuated to
#: "approximately -33 dBm", then "additionally attenuated by the RX-888
#: 60 MHz low-pass filter" — 84.225 MHz sits above that corner.
#:
#: Recorded because a station once chased a supposed overdrive fault at the
#: injector (AC0G/DASI-009, 2026-09-16) and added 20 dB of pad to a signal
#: that was designed weak and is filtered twice before the ADC. The level
#: hypothesis was wrong; see reference_tick / the T6 notes.
TS1_INJECT_DBM_NOMINAL = -33.0


def nyquist_alias_hz(tx_hz: int, adc_hz: int) -> int:
    """Fold ``tx_hz`` into the first Nyquist zone ``[0, adc_hz/2]``.

    Real sampling maps every input to one baseband image.  Reduce modulo
    the sample rate, then reflect anything above Nyquist:

        r = tx mod adc                 ->  r in [0, adc)
        alias = r if r <= adc/2 else adc - r

    ⛔ The shell this replaces computed ``adc - tx`` whenever
    ``tx > adc/2``, which is only the first zone.  At the *other* rate its
    own help text names it returns a NEGATIVE frequency:

        129_600_000 - 84_225_000 =  45_375_000   correct
         64_800_000 - 84_225_000 = -19_425_000   nonsense

    while ``config/timestd-config.toml.template`` documents the answer as
    19.425 MHz. Folding gives exactly that.

    Raises:
        ValueError: on a non-positive rate or a negative frequency —
            neither has an alias, and returning a plausible-looking
            number for them is how a bad channel gets created quietly.
    """
    adc = int(adc_hz)
    tx = int(tx_hz)
    if adc <= 0:
        raise ValueError(f"adc_hz must be positive, got {adc_hz!r}")
    if tx < 0:
        raise ValueError(f"tx_hz must not be negative, got {tx_hz!r}")
    r = tx % adc
    return r if r * 2 <= adc else adc - r


def adc_rate_from_status(status: object) -> Optional[int]:
    """The RX888 ADC rate from one radiod status object, or ``None``.

    Reads ``status.frontend.input_samprate`` — the front-end block rides
    on every channel's status packet, so any channel will do.

    ⚠ ``output_samprate`` sits on the same object and is the *channel*
    rate (12 kHz on B4's WSPR channels, 96 kHz on the T6 channel).
    Reading it instead is the same mistake as parsing the first
    ``samprate`` out of radiod's config, and just as quiet.

    Never raises: callers are provisioning paths that must degrade to
    asking rather than abort.
    """
    try:
        frontend = getattr(status, 'frontend', None)
        if frontend is None:
            return None
        rate = getattr(frontend, 'input_samprate', None)
        if rate is None:
            return None
        rate = int(rate)
        return rate if rate > 0 else None
    except (TypeError, ValueError):
        logger.debug("frontend.input_samprate unreadable", exc_info=True)
        return None


def resolve_t6_frequency_hz(
    tx_hz: int,
    *,
    adc_hz: Optional[int] = None,
    probe: Optional[Callable[[], Optional[int]]] = None,
    default_adc_hz: int = DEFAULT_ADC_HZ,
) -> tuple[int, int, str]:
    """The T6 channel frequency, the rate it came from, and its provenance.

    Precedence mirrors the T5 resolver in ``gpsdo_capability``: a measured
    answer beats a declared one, and a declared one beats a default.

    1. ``adc_hz`` given explicitly (operator or site profile) — obeyed.
    2. ``probe()`` — radiod's own ``input_samprate``.
    3. ``default_adc_hz`` — the fleet standard, reported as a GUESS.

    The third case is the one that used to be silent.  It still produces a
    frequency, because refusing would strand every site whose radiod is
    not up yet, but it says so in the returned provenance and the caller
    is expected to surface that.

    Returns:
        ``(frequency_hz, adc_hz, source)`` where ``source`` is one of
        ``"configured"``, ``"radiod"`` or ``"default"``.
    """
    if adc_hz is not None:
        rate, source = int(adc_hz), "configured"
    else:
        probed = None
        if probe is not None:
            try:
                probed = probe()
            except Exception:                              # noqa: BLE001
                logger.debug("radiod ADC-rate probe failed", exc_info=True)
                probed = None
        if probed:
            rate, source = int(probed), "radiod"
        else:
            rate, source = int(default_adc_hz), "default"

    return nyquist_alias_hz(tx_hz, rate), rate, source
