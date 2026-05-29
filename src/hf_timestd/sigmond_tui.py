"""sigmond Receiver Channels TUI parser for hf-timestd.

Loaded by sigmond at TUI time via ``[client_features.receiver_channels]``
in ``deploy.toml``.  hf-timestd is a singleton (one PSWS station_id
per host) — the deploy.toml block declares per_instance=false and
config_path=/etc/hf-timestd/timestd-config.toml.  See
[[hf_timestd_singleton]] in the wider sigmond context for why
templated per-station units are NOT the right model here.
"""

from __future__ import annotations

from typing import Optional

from sigmond.ka9q_encoding import encoding_to_int


def parse_receiver_channels(
    cfg: dict,
) -> tuple[str, set[int], Optional[int]]:
    """Return ``(status_dns, configured_freqs_hz, encoding_int)`` from
    the hf-timestd singleton config.

    Status address is under [ka9q].status (note: ``status``, not
    ``status_address`` — the ka9q-radio fragment convention used by
    hf-timestd predates the newer naming hf-gps-tec uses).
    Frequencies are flattened from every channel inside every
    [recorder.channel_group.*].  Encoding may be set per-group; we
    take the first group's encoding and fall back to
    [recorder.channel_defaults].encoding.  Returning None for encoding
    means "match any encoding" and is a legitimate outcome on a
    minimally-configured host.
    """
    ka9q = cfg.get("ka9q") or {}
    status = str(ka9q.get("status") or "")
    freqs: set[int] = set()
    recorder = cfg.get("recorder") or {}
    encoding: Optional[int] = None
    for group in (recorder.get("channel_group") or {}).values():
        if encoding is None:
            encoding = encoding_to_int(group.get("encoding"))
        for ch in (group.get("channels") or []):
            hz = ch.get("frequency_hz")
            if hz is None:
                continue
            try:
                freqs.add(int(hz))
            except (TypeError, ValueError):
                continue
    if encoding is None:
        encoding = encoding_to_int(
            (recorder.get("channel_defaults") or {}).get("encoding")
        )
    return status, freqs, encoding
