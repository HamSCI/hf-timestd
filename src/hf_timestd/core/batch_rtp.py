"""RTP label for a batch of samples delivered by ka9q-python.

Shared by the T6/anchor path (core_recorder_v2) and the raw archive path
(stream_recorder_v2), so both name a batch by the same sample.
"""
import logging


def resolve_batch_rtp(quality, _warned=[False]):
    """Truthful RTP label for a delivered sample batch.

    Prefer ``quality.delivered_rtp_start`` (ka9q-python >= 3.21.0: the
    resequencer's true first-sample timestamp, gap fills included).
    Fall back to ``last_rtp_timestamp`` — the last RECEIVED packet's
    header — with a one-time warning: that label desynchronizes from
    the delivered stream under loss (the T6 origin-slip root cause,
    docs/T6-BLOCK-SLIP-ROOT-CAUSE-2026-08-10.md), and it names a packet
    late in the batch, not its first sample (the raw archive sat 60 ms
    late on B4 for that reason, 2026-11-22).
    """
    rtp = getattr(quality, 'delivered_rtp_start', None)
    if rtp is not None:
        return rtp
    if not _warned[0]:
        _warned[0] = True
        logging.getLogger(__name__).warning(
            "quality.delivered_rtp_start unavailable (ka9q-python < 3.21.0?) — "
            "falling back to last_rtp_timestamp; batch labels will carry "
            "packet-boundary wobble and slip under stream loss")
    return getattr(quality, 'last_rtp_timestamp', None)
