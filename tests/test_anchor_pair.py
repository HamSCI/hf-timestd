"""read_anchor_pair: one atomic (gps_time, rtp_timesnap) read at every site."""

import pathlib
import re
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'src'))

from ka9q.discovery import ChannelInfo
from hf_timestd.core.anchor_pair import read_anchor_pair


def _torn_ci():
    ci = ChannelInfo.__new__(ChannelInfo)
    ci._anchor_pair = (2_000_000_000, 48_000)   # consistent pair
    ci.gps_time, ci.rtp_timesnap = 3_000_000_000, 48_000   # torn legacy fields
    return ci


def test_read_anchor_pair_prefers_atomic_pair():
    assert read_anchor_pair(_torn_ci()) == (2_000_000_000, 48_000)


def test_read_anchor_pair_legacy_fallback():
    assert read_anchor_pair(SimpleNamespace(gps_time=5, rtp_timesnap=7)) == (5, 7)


def test_read_anchor_pair_none_cases():
    assert read_anchor_pair(None) is None
    assert read_anchor_pair(SimpleNamespace(gps_time=None, rtp_timesnap=7)) is None


def test_read_anchor_pair_mock_without_get_anchor():
    m = MagicMock(spec=["gps_time", "rtp_timesnap"])
    m.gps_time, m.rtp_timesnap = 1, 2
    assert read_anchor_pair(m) == (1, 2)


def test_read_anchor_pair_unspecced_mock_falls_back_to_legacy():
    m = MagicMock()   # get_anchor() returns a MagicMock, not a tuple
    m.gps_time, m.rtp_timesnap = 1, 2
    assert read_anchor_pair(m) == (1, 2)


def test_no_paired_legacy_reads_left_in_src():
    # Every (gps_time, rtp_timesnap) pair read goes through read_anchor_pair.
    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "hf_timestd"
    pat = re.compile(r"getattr\([^)]*'rtp_timesnap'|\b(ci|_ci|src_ci|dst_ci|channel_info)\.rtp_timesnap\b")
    hits = [f"{p}:{i}" for p in src.rglob("*.py") if p.name != "anchor_pair.py"
            for i, line in enumerate(p.read_text().splitlines(), 1)
            if pat.search(line) and not line.lstrip().startswith("#")]
    assert hits == []
