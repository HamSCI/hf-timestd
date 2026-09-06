"""Phase 5 of the split: the web UI lives in station-web now."""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_web_api_directory_gone():
    assert not (ROOT / "web-api").exists()


def test_no_fastapi_dependency():
    with open(ROOT / "pyproject.toml", "rb") as fh:
        deps = tomllib.load(fh)["project"]["dependencies"]
    for banned in ("fastapi", "uvicorn", "jinja2", "python-multipart", "aiofiles"):
        assert not any(d.startswith(banned) for d in deps), banned


def test_manifest_no_longer_ships_the_web_unit():
    with open(ROOT / "deploy.toml", "rb") as fh:
        d = tomllib.load(fh)
    assert "timestd-web-api.service" not in d["systemd"]["units"]
    assert not any("web-api" in s.get("src", "") for s in d["install"]["steps"])
    assert not (ROOT / "systemd" / "timestd-web-api.service").exists()


def test_every_install_step_src_exists():
    with open(ROOT / "deploy.toml", "rb") as fh:
        d = tomllib.load(fh)
    # Only repo-relative srcs are checkable here.  An absolute src names a
    # path on the target host (e.g. venv/bin/hf-timestd, a build product of
    # the venv step) and is legitimately absent from a checkout.
    missing = [
        s["src"] for s in d["install"]["steps"]
        if "src" in s and not s["src"].startswith("/")
        and not (ROOT / s["src"]).exists()
    ]
    assert not missing, missing


def test_no_web_ui_residue_in_scripts():
    import re
    hits = []
    for p in (ROOT / "scripts").iterdir():
        if p.is_file() and p.suffix in {".sh", ".py"}:
            for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                if re.search(r"timestd-web-api|timestd-web-ui|web-api/|WEBUI", line):
                    hits.append(f"{p.name}:{i}")
    assert not hits, hits
