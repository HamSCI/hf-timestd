"""Constructing an AuthorityManager must not touch the filesystem.

`__init__` used to `mkdir(parents=True)` the output path's parent, so
merely *building* a manager tried to create `/run/hf-timestd`.  Anything
that only wanted to inspect the object — which probes registered, what a
config resolves to — needed write access to a production runtime
directory, and off-station that raises PermissionError before a single
assertion runs.  It cost ten tests in this suite.

The directory belongs to the write, which already treats OSError as a
logged warning rather than a crash: a station that cannot write its
authority file should say so and keep running, not die building the
object.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hf_timestd.core.authority_manager import AuthorityManager, AuthorityState


def _state():
    return AuthorityManager.tick.__globals__["AuthorityState"](
        a_level="A1", t_level_active="T4", t_level_available=["T4"],
        t_level_witnesses={}, rtp_to_utc_offset_ns=0, sigma_ns=1.0,
        stations_contributing=[], last_transition_utc=None,
        disagreement_flags=[],
    )


def _manager(output_path):
    return AuthorityManager(
        probes=[],
        output_path=output_path,
        a_level_provider=lambda: "A1",
    )


def test_construction_creates_nothing(tmp_path):
    target = tmp_path / "not-yet" / "authority.json"
    _manager(target)
    assert not target.parent.exists(), "construction created its output directory"


def test_construction_survives_an_unwritable_parent():
    """The off-station case: /run/hf-timestd is root-owned or absent."""
    _manager(Path("/run/hf-timestd-nonexistent-xyz/authority.json"))


def test_the_write_creates_the_directory(tmp_path):
    target = tmp_path / "made-on-demand" / "authority.json"
    m = _manager(target)
    m._write_state(_state())
    assert target.exists(), "write did not create its own directory"


def test_an_unwritable_destination_warns_rather_than_raises(caplog):
    m = _manager(Path("/proc/definitely-not-writable/authority.json"))
    with caplog.at_level("WARNING"):
        m._write_state(_state())   # must not raise
    assert any("failed to write" in r.message or "failed to write" in str(r)
               for r in caplog.records), caplog.text
