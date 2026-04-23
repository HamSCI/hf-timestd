#!/usr/bin/env python3
"""Unit tests for MdnsFusionAdvertiser."""

import subprocess
import unittest
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from hf_timestd.core.authority_manager import AuthorityState
from hf_timestd.core.mdns_fusion_advertiser import (
    ENABLED_T_LEVELS,
    MdnsFusionAdvertiser,
    TXT_SCHEMA,
)


def _state(
    t_level_active: Optional[str] = "T3",
    a_level: str = "A1",
    sigma_ns: Optional[int] = 800_000,
    stations: Optional[List[str]] = None,
    disagreement: Optional[List[str]] = None,
) -> AuthorityState:
    return AuthorityState(
        a_level=a_level,
        t_level_active=t_level_active,
        t_level_available=["T3", "T2"] if t_level_active else [],
        t_level_witnesses=["T2"] if t_level_active else [],
        rtp_to_utc_offset_ns=800_000 if t_level_active else None,
        sigma_ns=sigma_ns,
        stations_contributing=stations or ["WWV", "CHU"],
        last_transition_utc="2026-04-23T12:00:00.000000Z",
        disagreement_flags=disagreement or [],
    )


@dataclass
class _FakeProc:
    """Minimal subprocess.Popen-like stand-in for tests."""
    cmd: Tuple[str, ...]
    terminated: bool = False
    killed: bool = False

    def poll(self) -> Optional[int]:
        return None if not self.terminated else 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.terminated = True

    def wait(self, timeout=None) -> int:
        return 0


@dataclass
class _PopenRecorder:
    """Callable that records Popen invocations and returns _FakeProc."""
    calls: List[Tuple[str, ...]] = field(default_factory=list)
    raise_on_call: Optional[Exception] = None

    def __call__(self, cmd, **kw) -> _FakeProc:
        if self.raise_on_call is not None:
            raise self.raise_on_call
        self.calls.append(tuple(cmd))
        return _FakeProc(cmd=tuple(cmd))


class TestMdnsFusionAdvertiser(unittest.TestCase):
    def _adv(self, popen=None, hostname="testhost", **kw) -> MdnsFusionAdvertiser:
        return MdnsFusionAdvertiser(hostname=hostname, popen=popen, **kw)

    def test_t3_active_starts_subprocess_with_txt(self) -> None:
        popen = _PopenRecorder()
        adv = self._adv(popen=popen)
        result = adv.apply(_state(t_level_active="T3"))
        self.assertTrue(result.applied)
        self.assertEqual(result.target_state, "advertising")
        self.assertEqual(len(popen.calls), 1)
        cmd = popen.calls[0]
        # Basic command shape
        self.assertIn("_ntp._udp", cmd)
        self.assertIn("123", cmd)
        self.assertTrue(any("hf-timestd Fusion" in s for s in cmd))
        # TXT fields present
        self.assertIn(f"schema={TXT_SCHEMA}", cmd)
        self.assertIn("source=fusion", cmd)
        self.assertIn("host=testhost", cmd)
        self.assertIn("A=A1", cmd)
        self.assertIn("T=T3", cmd)
        self.assertIn("stations=WWV,CHU", cmd)
        self.assertIn("disagreement=none", cmd)

    def test_t6_active_also_starts(self) -> None:
        popen = _PopenRecorder()
        adv = self._adv(popen=popen)
        result = adv.apply(_state(t_level_active="T6"))
        self.assertTrue(result.applied)
        self.assertIn("T=T6", popen.calls[0])

    def test_t4_active_does_not_publish(self) -> None:
        popen = _PopenRecorder()
        adv = self._adv(popen=popen)
        result = adv.apply(_state(t_level_active="T4"))
        self.assertEqual(result.target_state, "withdrawn")
        self.assertEqual(popen.calls, [])

    def test_no_active_level_is_withdrawn(self) -> None:
        popen = _PopenRecorder()
        adv = self._adv(popen=popen)
        result = adv.apply(_state(t_level_active=None))
        self.assertEqual(result.target_state, "withdrawn")
        self.assertEqual(popen.calls, [])

    def test_steady_state_makes_no_new_subprocess_calls(self) -> None:
        popen = _PopenRecorder()
        adv = self._adv(popen=popen)
        state = _state(t_level_active="T3")
        adv.apply(state)  # applied: start
        for _ in range(4):
            r = adv.apply(state)
            self.assertFalse(r.applied)
            self.assertEqual(r.reason, "no change")
        self.assertEqual(len(popen.calls), 1)

    def test_txt_change_restarts_subprocess(self) -> None:
        popen = _PopenRecorder()
        adv = self._adv(popen=popen)
        adv.apply(_state(sigma_ns=800_000))
        adv.apply(_state(sigma_ns=2_500_000))  # different q95_ms → restart
        self.assertEqual(len(popen.calls), 2)
        # second invocation has the new q95_ms
        cmd2 = popen.calls[1]
        self.assertTrue(any(s.startswith("q95_ms=") and "2.5" in s for s in cmd2))

    def test_transition_to_withdrawn_terminates_subprocess(self) -> None:
        popen = _PopenRecorder()
        adv = self._adv(popen=popen)
        adv.apply(_state(t_level_active="T3"))
        result = adv.apply(_state(t_level_active="T4"))
        self.assertTrue(result.applied)
        self.assertEqual(result.target_state, "withdrawn")

    def test_governor_radiod_surfaced_in_txt_when_provided(self) -> None:
        popen = _PopenRecorder()
        adv = self._adv(popen=popen)
        adv.apply(_state(t_level_active="T3"), governor_radiod="bee1-hf-status.local")
        cmd = popen.calls[0]
        self.assertIn("radiod=bee1-hf-status.local", cmd)

    def test_governor_radiod_change_restarts_subprocess(self) -> None:
        popen = _PopenRecorder()
        adv = self._adv(popen=popen)
        state = _state(t_level_active="T3")
        adv.apply(state, governor_radiod="radiod-a.local")
        adv.apply(state, governor_radiod="radiod-b.local")
        self.assertEqual(len(popen.calls), 2)

    def test_governor_absent_no_radiod_field(self) -> None:
        popen = _PopenRecorder()
        adv = self._adv(popen=popen)
        adv.apply(_state(t_level_active="T3"))
        cmd = popen.calls[0]
        self.assertFalse(any(s.startswith("radiod=") for s in cmd))

    def test_dry_run_does_not_spawn(self) -> None:
        def _boom(*a, **kw):
            raise AssertionError("should not spawn in dry_run")
        adv = self._adv(popen=_boom, dry_run=True)
        result = adv.apply(_state(t_level_active="T3"))
        self.assertTrue(result.applied)
        self.assertEqual(result.reason, "dry_run")

    def test_avahi_missing_surfaces_reason(self) -> None:
        popen = _PopenRecorder(raise_on_call=FileNotFoundError())
        adv = self._adv(popen=popen)
        result = adv.apply(_state(t_level_active="T3"))
        self.assertFalse(result.applied)
        self.assertIn("not found", result.reason)

    def test_close_terminates_subprocess(self) -> None:
        popen = _PopenRecorder()
        adv = self._adv(popen=popen)
        adv.apply(_state(t_level_active="T3"))
        proc = adv._proc  # type: ignore[attr-defined]
        self.assertIsNotNone(proc)
        adv.close()
        self.assertIsNone(adv._proc)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
