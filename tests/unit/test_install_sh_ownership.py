"""`scripts/install.sh` must not re-own the git checkout.

Why this exists (2026-09-17).  `INSTALL_DIR` is the checkout itself
(``INSTALL_DIR="$PROJECT_DIR"``).  install.sh used to finish with:

    chown -R "$INSTALL_USER:$INSTALL_USER" \\
        "$INSTALL_DIR/pyproject.toml" "$INSTALL_DIR/src" \\
        "$INSTALL_DIR/scripts" "$INSTALL_DIR/docs"

That list omits ``.git``.  sigmond's installer clones the repo, runs
``chown -R sigmond:sigmond`` over the whole tree, and *then* runs this
script — so the worktree ended up ``timestd:timestd`` while ``.git``
stayed ``sigmond:sigmond``.  Split ownership is exactly what
``sigmond.gitowner`` REFUSES (sigmond#43/#44).

Observed on DASI-009.AI6VN across three images (v3.39 2026-09-16, v3.40
and v3.42 2026-09-17).  No other component on that host was ever split,
because no other component re-owns its own checkout.

⚠ Correction to an earlier claim of mine: ``smd doctor`` is NOT blind to
ownership.  It reports it per component and offers ``--fix``.  What it
does is infer the EXPECTED owner from the checkout's top-level directory
node, which is why the second defect below (``ensure_dir`` on the
checkout) is worse than it looks rather than merely untidy.

The chown bought nothing: ``timestd`` is in the ``sigmond`` group and the
installer leaves the tree 2775 (setgid, group-writable).  Verified on
AC0G-B4 and AC0G-ND — both ``sigmond:sigmond`` 2775, ``timestd`` in group
``sigmond``, reading the source fine while running T6.

⚠ These assert on the SHELL SOURCE, not on behaviour.  A shell installer
that wants root and a real filesystem cannot be run in a unit test, and a
guard that only reads the file is still worth more than no guard: the
defect was a single line, and a single line is what this catches.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).parents[2] / "scripts" / "install.sh"

#: Paths that live inside the git checkout. Re-owning any of them splits the
#: tree against `.git`, which the installer leaves to the cloning account.
CHECKOUT_PATHS = ("pyproject.toml", "src", "scripts", "docs")


def _live_lines() -> list[tuple[int, str]]:
    """Numbered logical lines: comments dropped, continuations JOINED.

    Two things this has to get right, and the first cut got neither:

    1. The retired chown is quoted verbatim in a comment block right above
       where it was removed — that is the point of the comment — so a plain
       substring search over the whole file always matches.  Comments out.

    2. ⚠ The defect spanned FIVE lines via ``\\`` continuations, so ``chown``
       and ``$INSTALL_DIR/src`` never shared a physical line.  A per-physical
       -line scan therefore passed against the real defect — caught by
       mutation-testing this module, not by reading it.  Continuations are
       joined into one logical line before anything is matched.
    """
    out: list[tuple[int, str]] = []
    pending: str | None = None
    start = 0
    for n, raw in enumerate(INSTALL_SH.read_text().splitlines(), 1):
        line = raw.strip()
        if pending is None:
            if not line or line.startswith("#"):
                continue
            pending, start = line, n
        else:
            pending += " " + line
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        out.append((start, pending))
        pending = None
    if pending is not None:
        out.append((start, pending))
    return out


def test_install_sh_exists():
    """Guard the guard: a renamed script must not silently pass every test."""
    assert INSTALL_SH.is_file(), f"{INSTALL_SH} is missing"


@pytest.mark.parametrize("path", CHECKOUT_PATHS)
def test_no_chown_of_any_checkout_path(path):
    """No live `chown` may name a path inside the checkout."""
    offenders = [
        (n, line) for n, line in _live_lines()
        if "chown" in line and f"$INSTALL_DIR/{path}" in line
    ]
    assert not offenders, (
        f"scripts/install.sh chowns $INSTALL_DIR/{path}, which is inside the "
        f"git checkout: {offenders}. That splits the worktree against .git and "
        f"sigmond.gitowner then refuses every update (sigmond#43/#44)."
    )


def test_no_chown_of_the_install_dir_itself():
    """`chown ... "$INSTALL_DIR"` (no trailing path) is the same defect."""
    pat = re.compile(r'chown\b.*"\$INSTALL_DIR"(\s|$)')
    offenders = [(n, l) for n, l in _live_lines() if pat.search(l)]
    assert not offenders, (
        f"scripts/install.sh chowns $INSTALL_DIR itself: {offenders}. "
        f"INSTALL_DIR is the git checkout (INSTALL_DIR=\"$PROJECT_DIR\")."
    )


def test_install_dir_is_still_the_checkout():
    """The premise above, asserted rather than assumed.

    If a future refactor makes INSTALL_DIR a copy target separate from the
    checkout, re-owning it stops being a defect and these tests become
    wrong rather than merely redundant. Fail loudly at that point instead
    of quietly forbidding something that is now fine.
    """
    assert any(
        'INSTALL_DIR="$PROJECT_DIR"' in line for _, line in _live_lines()
    ), (
        "INSTALL_DIR is no longer $PROJECT_DIR. Re-read this module's "
        "docstring: these guards assume INSTALL_DIR IS the git checkout."
    )


@pytest.mark.parametrize("runtime_path", ("/dev/shm/timestd", "$VENV_DIR"))
def test_the_writable_runtime_paths_are_still_chowned(runtime_path):
    """Removing the checkout chown must not remove the ones that matter.

    The daemon genuinely needs to own its runtime state. If a later edit
    strips these too, the service breaks in a way this file's change would
    be blamed for.

    ⚠ The first cut asserted ``"chown" in live and needed in live`` over the
    whole file, which passes whenever the path is mentioned ANYWHERE — and
    ``$VENV_DIR`` is mentioned in its own assignment, so the test could not
    fail. Mutation-testing caught it. Match a chown that names the path.
    """
    hits = [
        (n, line) for n, line in _live_lines()
        if line.startswith("chown") and runtime_path in line
    ]
    assert hits, (
        f"install.sh no longer chowns {runtime_path}; the service account "
        f"must own its writable paths even though it must not own the checkout."
    )


@pytest.mark.parametrize("path", ("", "/scripts", "/config", "/docs"))
def test_ensure_dir_is_not_called_on_the_checkout(path):
    """`ensure_dir` chowns what it makes — so never point it at the checkout.

    The loop used to contain `$INSTALL_DIR` plus its `scripts`, `config` and
    `docs`. All four are TRACKED in git, so `mkdir -p` was a no-op and the
    chown was the whole effect.

    ⚠ That looks cosmetic and is not. `smd doctor` infers a component's
    expected owner from its checkout's top-level directory node, so a
    `timestd`-owned node with `sigmond`-owned contents makes doctor demand
    the opposite of every working station — measured on v3.42, 2026-09-17:

        AC0G-B4   node sigmond -> "1 path(s) not owned by sigmond"
        DASI-009  node timestd -> "1643 path(s) not owned by timestd"

    and `--fix` acts on that inference.
    """
    target = f'"$INSTALL_DIR{path}"'
    in_loop = []
    seen_loop = False
    for n, line in _live_lines():
        if line.startswith("for d in") or seen_loop:
            seen_loop = True
            in_loop.append((n, line))
            if line == "done":
                break
    body = " ".join(l for _, l in in_loop)
    assert target not in body, (
        f'ensure_dir loop includes {target}, which is inside the git '
        f'checkout (INSTALL_DIR="$PROJECT_DIR"). ensure_dir chowns it to '
        f'timestd, which flips what `smd doctor` expects for the whole '
        f'component. Ownership belongs to whoever cloned the repo.'
    )
