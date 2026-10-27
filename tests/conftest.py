import sys
from pathlib import Path

import pytest

# pytest puts tests/ itself on sys.path, so a sibling test module is
# importable by bare name but "tests" is not importable as a package.
# Three test files use `from tests.<sibling> import ...`; two of them
# failed collection outright, and the third carried a hand-rolled
# sys.path insertion with a comment calling the other two "a known
# pre-existing environment issue".  Putting the repo root on the path
# once, here, makes "tests" resolve as an implicit namespace package for
# every test rather than for whichever file remembered the workaround.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def _chain_delay_path_in_tmp(tmp_path, monkeypatch):
    """Keep every test's judge off /var/lib.

    OffsetJudge reads offset_judge.DEFAULT_CHAIN_DELAY_PATH at
    construction time when ``chain_delay_path`` is not configured; a
    judge built with defaults would otherwise load (and could write) the
    production record.  Covers tests/unit too.
    """
    from hf_timestd.core import offset_judge
    monkeypatch.setattr(offset_judge, "DEFAULT_CHAIN_DELAY_PATH",
                        str(tmp_path / "t6_chain_delay.json"))
