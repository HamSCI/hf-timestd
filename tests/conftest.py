import sys
from pathlib import Path

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
