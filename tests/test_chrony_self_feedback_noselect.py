#!/usr/bin/env python3
"""hf-timestd#49 — a `noselect` refclock is not a chrony rejection.

§7.1.1 ships FUSE and HPPS as `noselect` in
config/chrony-timestd-refclocks.conf. chrony never selects a noselect
source, so it reports state `?` forever. Before this fix,
``_check_chrony_self_feedback`` read `?` as outside
``CHRONY_HEALTHY_STATES`` and returned a permanent
``chrony-rejected-<refid>:state=?`` flag, rolling every station's
fleet-health verdict to INVALID even with T6 AUTHORITATIVE and zero
real violations (seen live on AC0G-B4).

The fix reads chrony's own config (the `refclock` lines in
`/etc/chrony/chrony.conf` and `/etc/chrony/conf.d/*.conf`) to learn
whether the refid is configured `noselect`; when it is, state says
nothing about our source and the check stands down (except the
"missing from chronyc sources entirely" case, which stays worth
saying).
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hf_timestd.core.authority_manager import AuthorityManager


def _chrony_runner(sources_csv: str):
    def runner(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, sources_csv, "")
    return runner


class ChronySelfFeedbackNoselectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.out = self.tmp / "authority.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mgr(self) -> AuthorityManager:
        return AuthorityManager(
            probes=[],
            output_path=self.out,
            a_level_provider=lambda: "A1",
        )

    def _conf(self, name: str, text: str) -> Path:
        p = self.tmp / name
        p.write_text(text)
        return p

    def test_noselect_refid_with_unselected_state_is_not_rejected(self) -> None:
        # §7.1.1: HPPS is shipped `noselect`; chrony reports it `?`
        # forever. That must NOT be read as a rejection.
        conf = self._conf(
            "timestd-refclocks.conf",
            "refclock SHM 2 refid HPPS poll 0 noselect precision 1e-4\n",
        )
        mgr = self._mgr()
        with mock.patch(
            "hf_timestd.core.authority_manager.subprocess.run",
            side_effect=_chrony_runner("#,?,HPPS,0,4,0,5,0.000000,0.000001\n"),
        ):
            flag = mgr._check_chrony_self_feedback("T6", conf_paths=[conf])
        self.assertIsNone(
            flag,
            "a noselect refid's state says nothing about our source "
            "(chrony never selects it by configuration) -- must not "
            "read as a rejection")

    def test_selectable_refid_with_unselected_state_is_still_rejected(self) -> None:
        # Today's behaviour, kept: a refid that CAN be selected but
        # chrony still marks unselectable/falseticker is worth flagging.
        conf = self._conf(
            "timestd-refclocks.conf",
            "refclock SHM 2 refid HPPS poll 0 precision 1e-4\n",
        )
        mgr = self._mgr()
        with mock.patch(
            "hf_timestd.core.authority_manager.subprocess.run",
            side_effect=_chrony_runner("#,?,HPPS,0,4,0,5,0.000000,0.000001\n"),
        ):
            flag = mgr._check_chrony_self_feedback("T6", conf_paths=[conf])
        self.assertEqual(flag, "chrony-rejected-HPPS:state=?")

    def test_noselect_refid_absent_from_sources_is_still_missing(self) -> None:
        # Exception to the noselect exemption: chrony not consuming the
        # segment at all is still worth surfacing.
        conf = self._conf(
            "timestd-refclocks.conf",
            "refclock SHM 2 refid HPPS poll 0 noselect precision 1e-4\n",
        )
        mgr = self._mgr()
        with mock.patch(
            "hf_timestd.core.authority_manager.subprocess.run",
            side_effect=_chrony_runner("^,*,192.168.0.203,1,6,377,10,0.000001,0.000050\n"),
        ):
            flag = mgr._check_chrony_self_feedback("T6", conf_paths=[conf])
        self.assertEqual(flag, "chrony-missing-HPPS")

    def test_noselect_in_a_comment_does_not_count(self) -> None:
        # `# noselect later` is a comment, not the option.
        conf = self._conf(
            "timestd-refclocks.conf",
            "refclock SHM 2 refid HPPS poll 0 # noselect later\n",
        )
        mgr = self._mgr()
        with mock.patch(
            "hf_timestd.core.authority_manager.subprocess.run",
            side_effect=_chrony_runner("#,?,HPPS,0,4,0,5,0.000000,0.000001\n"),
        ):
            flag = mgr._check_chrony_self_feedback("T6", conf_paths=[conf])
        self.assertEqual(
            flag, "chrony-rejected-HPPS:state=?",
            "a commented-out `noselect` must not be read as the option")

    def test_unreadable_config_falls_back_to_todays_behaviour(self) -> None:
        # Missing/unreadable config counts as "no noselect known" --
        # never raises, and the check behaves as it did before this fix.
        missing = self.tmp / "does-not-exist.conf"
        mgr = self._mgr()
        with mock.patch(
            "hf_timestd.core.authority_manager.subprocess.run",
            side_effect=_chrony_runner("#,?,HPPS,0,4,0,5,0.000000,0.000001\n"),
        ):
            flag = mgr._check_chrony_self_feedback("T6", conf_paths=[missing])
        self.assertEqual(flag, "chrony-rejected-HPPS:state=?")


if __name__ == "__main__":
    unittest.main()
