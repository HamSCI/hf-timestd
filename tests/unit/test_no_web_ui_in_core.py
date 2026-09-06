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
