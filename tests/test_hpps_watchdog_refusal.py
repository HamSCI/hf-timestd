"""v3.85 item 1, Fix B: hpps-watchdog defers to a refusal it cannot cure.

AC0G-ND, 2026-10-27 to 10-31.  T6 refused every acquisition because the
edge's raw chain delay read radiod's RTP counter, drawn 0.3-0.5 s off UTC.
HPPS stayed dark.  The recorder restarts that followed opened data gaps and
cured nothing, because the counter belongs to radiod and outlives the
recorder.

The recorder publishes the refusal (t6_acquisition_refusal.py).  The
watchdog never restarts the recorder: it files a request with
sigmond-recorder-watchdog.  While a refusal stays current, it files none,
and says so once on entry and once on exit.  While sigmond's compaction
marker stands, it checks nothing.  The tests run the real script against
stub chronyc / systemctl / logger binaries and a status file built by the
recorder's own key builder, so the producer and the grep agree by
construction.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.core_recorder_v2 import (
    CoreRecorderV2, t6_refusal_status_keys)
from hf_timestd.core.t6_acquisition_refusal import (
    REFUSAL_PLACEMENT_IMPLAUSIBLE, T6AcquisitionRefusal)

SCRIPT = (Path(__file__).resolve().parent.parent / "scripts"
          / "hpps-watchdog.sh")


# ── the record ────────────────────────────────────────────────────────

class TestTheRefusalRecord(unittest.TestCase):

    def test_entry_is_reported_once(self):
        rec = T6AcquisitionRefusal()
        self.assertTrue(rec.note(REFUSAL_PLACEMENT_IMPLAUSIBLE,
                                 -462_600_000, 0.0, 1.7e9))
        for t in (5.0, 10.0, 15.0):
            self.assertFalse(rec.note(REFUSAL_PLACEMENT_IMPLAUSIBLE,
                                      -462_600_000, t, 1.7e9 + t))
        cur = rec.current(15.0)
        self.assertEqual(cur["refusals"], 4)
        self.assertEqual(cur["since_utc"], 1.7e9)
        self.assertEqual(cur["placement_ms"], -462.6)

    def test_a_chain_delay_refusal_is_not_cured_by_a_restart(self):
        rec = T6AcquisitionRefusal()
        rec.note(REFUSAL_PLACEMENT_IMPLAUSIBLE, 1, 0.0, 0.0)
        self.assertIs(rec.current(0.0)["restart_cures"], False)

    def test_an_unknown_reason_claims_nothing(self):
        """Only a reason someone has argued through earns a skip."""
        rec = T6AcquisitionRefusal()
        rec.note("something_else", 1, 0.0, 0.0)
        self.assertIs(rec.current(0.0)["restart_cures"], True)

    def test_a_refusal_nothing_refreshes_stops_being_current(self):
        rec = T6AcquisitionRefusal()
        rec.note(REFUSAL_PLACEMENT_IMPLAUSIBLE, 1, 0.0, 0.0)
        self.assertIsNotNone(rec.current(rec.CURRENT_FOR_S))
        self.assertIsNone(rec.current(rec.CURRENT_FOR_S + 1.0))

    def test_exit_is_reported_once(self):
        rec = T6AcquisitionRefusal()
        rec.note(REFUSAL_PLACEMENT_IMPLAUSIBLE, 1, 0.0, 0.0)
        self.assertIsNone(rec.expire(10.0))
        self.assertIsNotNone(rec.expire(rec.CURRENT_FOR_S + 1.0))
        self.assertIsNone(rec.expire(rec.CURRENT_FOR_S + 2.0))
        self.assertIsNone(rec.clear(rec.CURRENT_FOR_S + 3.0))


class TestTheRecorderLogsEachEdgeOnce(unittest.TestCase):

    def _cr(self):
        return CoreRecorderV2.__new__(CoreRecorderV2)

    def test_many_refusals_one_entry_line_one_exit_line(self):
        cr = self._cr()
        with self.assertLogs("hf_timestd.core.core_recorder_v2",
                             level=logging.WARNING) as logs:
            for _ in range(50):
                cr._t6_note_acquisition_refusal(-462_600_000, "host is T2")
            cr._t6_clear_acquisition_refusal("T6 acquired")
            cr._t6_clear_acquisition_refusal("T6 acquired")
        entry = [m for m in logs.output if "cannot cure" in m]
        exits = [m for m in logs.output if "CLEARED" in m]
        self.assertEqual((len(entry), len(exits)), (1, 1), logs.output)
        self.assertIn("host is T2", entry[0])

    def test_the_status_keys_are_null_when_nothing_is_refused(self):
        keys = t6_refusal_status_keys(self._cr()._t6_acquisition_refusal_status())
        self.assertTrue(keys)
        self.assertTrue(all(v is None for v in keys.values()))


# ── the script ────────────────────────────────────────────────────────

class _Station:
    """A temp dir standing in for one station: stubs, config, state."""

    def __init__(self, root: Path):
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.state = root / "state"
        self.calls = root / "systemctl.calls"
        self.lastrx = root / "lastrx"
        self.status = root / "core-recorder-status.json"
        self.config = root / "timestd-config.toml"
        self.config.write_text("[timing.t6_pps]\nenabled = true\n")
        self.request_dir = root / "recorder-restart"
        self.set_lastrx(9999)
        self._stub("chronyc", f"""#!/bin/bash
case "$*" in
  *-c*) printf '#,?,HPPS,0,4,0,%s,0,0,0\\n' "$(cat {self.lastrx})" ;;
  *)    echo "MS Name" ;;
esac
""")
        self._stub("systemctl", f'#!/bin/bash\necho "$*" >> {self.calls}\n')
        self._stub("logger", "#!/bin/bash\nexit 0\n")

    def _stub(self, name, body):
        p = self.bin / name
        p.write_text(body)
        p.chmod(0o755)

    def set_lastrx(self, s):
        self.lastrx.write_text(str(s))

    def publish(self, refusal, age_s=0.0):
        """Write the recorder status file the way _write_status does."""
        doc = {"t6_pps": {"enabled": True,
                          **t6_refusal_status_keys(refusal)}}
        self.status.write_text(json.dumps(doc, indent=2))
        if age_s:
            t = time.time() - age_s
            os.utime(self.status, (t, t))

    def run(self):
        env = dict(os.environ,
                   PATH=f"{self.bin}:{os.environ['PATH']}",
                   HPPS_CONFIG_FILE=str(self.config),
                   HPPS_STATE_DIR=str(self.state),
                   HPPS_STATUS_FILE=str(self.root / "authority.json"),
                   HPPS_RECORDER_STATUS_FILE=str(self.status),
                   HPPS_LASTRX_THRESHOLD_S="600",
                   HPPS_RESTART_COOLDOWN_S="1800",
                   HPPS_REQUEST_DIR=str(self.request_dir),
                   HPPS_COMPACT_MARKER=str(self.root / "compact-in-progress.json"),
                   # Tests do not run as root.
                   REQUEST_DIR_OWNER_UID=str(os.getuid()),
                   REQUEST_DIR_INSTALL_OWNER="")
        out = subprocess.run(["bash", str(SCRIPT)], env=env,
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stdout + out.stderr
        return out.stdout.splitlines()

    @property
    def restarts(self):
        if not self.calls.exists():
            return 0
        return self.calls.read_text().count("restart")

    @property
    def requests(self):
        if not self.request_dir.exists():
            return 0
        return len(list(self.request_dir.glob("*.json")))

    def request(self):
        return json.loads(
            (self.request_dir / "hpps-watchdog.json").read_text())


def _nd_refusal():
    rec = T6AcquisitionRefusal()
    rec.note(REFUSAL_PLACEMENT_IMPLAUSIBLE, -462_600_000, 0.0, 1.7e9,
             "host-clock sigma 55.000 ms exceeds 1.000 ms")
    return rec.current(0.0)


class TestTheWatchdogScript(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.st = _Station(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_dark_hpps_with_no_refusal_files_a_request(self):
        """The recorder is restarted by sigmond alone; this script reports."""
        self.st.publish(None)
        out = self.st.run()
        self.assertEqual(self.st.restarts, 0)
        self.assertEqual(self.st.requests, 1)
        self.assertTrue(any("requested a recorder restart from "
                            "sigmond-recorder-watchdog" in l for l in out), out)
        req = self.st.request()
        self.assertEqual(req["cause"], "hpps-stale")
        self.assertEqual(req["reporter"], "hpps-watchdog")
        self.assertEqual(req["schema"], 1)
        self.assertIsInstance(req["requested_utc"], float)
        self.assertIs(req["stopped_recorder"], False)
        self.assertIn("LastRx=9999s", req["evidence"])

    def test_a_compaction_in_progress_files_no_request(self):
        # Mutation check: drop the marker check in main().
        self.st.publish(None)
        (self.st.root / "compact-in-progress.json").write_text("{}")
        out = self.st.run()
        self.assertEqual(self.st.requests, 0)
        self.assertEqual(self.st.restarts, 0)
        self.assertTrue(any("compaction in progress" in l for l in out), out)

    def test_the_request_cooldown_limits_requests(self):
        self.st.publish(None)
        self.st.run()
        (self.st.request_dir / "hpps-watchdog.json").unlink()
        self.st.run()
        self.assertEqual(self.st.requests, 0)

    def test_an_unsafe_request_dir_is_refused(self):
        self.st.request_dir.mkdir(mode=0o755)
        self.st.request_dir.chmod(0o777)
        self.st.publish(None)
        env = dict(os.environ, PATH=f"{self.st.bin}:{os.environ['PATH']}",
                   HPPS_CONFIG_FILE=str(self.st.config),
                   HPPS_STATE_DIR=str(self.st.state),
                   HPPS_STATUS_FILE=str(self.st.root / "authority.json"),
                   HPPS_RECORDER_STATUS_FILE=str(self.st.status),
                   HPPS_LASTRX_THRESHOLD_S="600",
                   HPPS_REQUEST_DIR=str(self.st.request_dir),
                   REQUEST_DIR_OWNER_UID=str(os.getuid()))
        r = subprocess.run(["bash", str(SCRIPT)], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.st.requests, 0)
        self.assertEqual(self.st.restarts, 0)

    def test_a_symlinked_request_dir_is_refused(self):
        target = self.st.root / "elsewhere"
        target.mkdir(mode=0o755)
        self.st.request_dir.symlink_to(target)
        self.st.publish(None)
        env = dict(os.environ, PATH=f"{self.st.bin}:{os.environ['PATH']}",
                   HPPS_CONFIG_FILE=str(self.st.config),
                   HPPS_STATE_DIR=str(self.st.state),
                   HPPS_STATUS_FILE=str(self.st.root / "authority.json"),
                   HPPS_RECORDER_STATUS_FILE=str(self.st.status),
                   HPPS_LASTRX_THRESHOLD_S="600",
                   HPPS_REQUEST_DIR=str(self.st.request_dir),
                   REQUEST_DIR_OWNER_UID=str(os.getuid()))
        r = subprocess.run(["bash", str(SCRIPT)], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(list(target.iterdir()), [])

    def test_an_incurable_refusal_holds_the_request(self):
        self.st.publish(_nd_refusal())
        self.st.run()
        self.assertEqual(self.st.restarts, 0)
        self.assertEqual(self.st.requests, 0)

    def test_the_hold_is_logged_once_and_then_nothing(self):
        self.st.publish(_nd_refusal())
        first = self.st.run()
        later = [self.st.run() for _ in range(5)]
        self.assertEqual(len(first), 1, first)
        self.assertIn("cannot cure", first[0])
        self.assertIn("placement_implausible", first[0])
        self.assertIn("-462.6", first[0])
        self.assertEqual(later, [[]] * 5, "the hold logged on every run")

    def test_a_stale_status_file_holds_off_nothing(self):
        """A dead or wedged recorder stops refreshing its status; the
        last refusal it wrote must not shield it from a restart."""
        self.st.publish(_nd_refusal(), age_s=600)
        self.st.run()
        self.assertEqual(self.st.restarts, 0)
        self.assertEqual(self.st.requests, 1)

    def test_a_refusal_a_restart_can_cure_does_not_hold(self):
        rec = dict(_nd_refusal(), restart_cures=True)
        self.st.publish(rec)
        self.st.run()
        self.assertEqual(self.st.restarts, 0)
        self.assertEqual(self.st.requests, 1)

    def test_when_the_refusal_ends_the_exit_is_logged_and_policy_resumes(self):
        self.st.publish(_nd_refusal())
        self.st.run()
        self.st.publish(None)
        out = self.st.run()
        self.assertTrue(any("no longer reports" in line for line in out),
                        out)
        self.assertEqual(self.st.restarts, 0)
        self.assertEqual(self.st.requests, 1)
        again = self.st.run()
        self.assertFalse(any("no longer reports" in line for line in again))

    def test_hpps_returning_closes_the_hold_quietly_once(self):
        self.st.publish(_nd_refusal())
        self.st.run()
        self.st.set_lastrx(3)
        out = self.st.run()
        self.assertEqual(len(out), 1, out)
        self.assertIn("HPPS sampled again", out[0])
        self.assertEqual(self.st.run(), [])
        self.assertEqual(self.st.restarts, 0)


if __name__ == "__main__":
    unittest.main()
