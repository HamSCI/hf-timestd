"""resolve_batch_rtp: truthful-label preference with warned fallback."""
import types
from hf_timestd.core.core_recorder_v2 import resolve_batch_rtp


def test_prefers_delivered_rtp_start():
    q = types.SimpleNamespace(delivered_rtp_start=12345, last_rtp_timestamp=99999)
    assert resolve_batch_rtp(q, _warned=[False]) == 12345


def test_falls_back_with_one_warning(caplog):
    q = types.SimpleNamespace(delivered_rtp_start=None, last_rtp_timestamp=777)
    warned = [False]
    with caplog.at_level("WARNING"):
        assert resolve_batch_rtp(q, _warned=warned) == 777
        assert resolve_batch_rtp(q, _warned=warned) == 777
    assert sum("delivered_rtp_start unavailable" in r.message
               for r in caplog.records) == 1


def test_absent_field_entirely():
    q = types.SimpleNamespace(last_rtp_timestamp=42)
    assert resolve_batch_rtp(q, _warned=[True]) == 42
