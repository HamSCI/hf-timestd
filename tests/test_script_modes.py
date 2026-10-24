"""Every shell script under scripts/ is tracked as executable (100755).

install.sh runs `chmod +x scripts/*.sh` inside the git checkout itself.  A
script committed as 100644 then reads as modified after any install, and a
dirty tree makes `smd admin manifest restore` and `smd align` refuse and
`smd component update` skip the checkout.  lib-chrony-refclocks.sh shipped
that way in bd0ac4a and failed the v3.80 update rig's rollback (Phase G,
2026-10-24).
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_every_tracked_script_is_executable():
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-s", "--", "scripts/*.sh"],
        capture_output=True, text=True, check=True,
    ).stdout
    rows = [ln.split(None, 3) for ln in out.splitlines() if ln.strip()]
    assert rows, "git ls-files found no scripts/*.sh: the test measured nothing"
    wrong = [r[3] for r in rows if r[0] != "100755"]
    assert not wrong, f"tracked without the executable bit: {wrong}"
