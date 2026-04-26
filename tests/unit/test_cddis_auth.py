"""
Unit tests for hf_timestd.cddis_auth

Centralized helpers for locating, validating, and using NASA Earthdata
credentials from a netrc file. Tests cover the credential search ladder,
permission checks, parse-error paths, and the requests.Session factory.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from hf_timestd import cddis_auth
from hf_timestd.cddis_auth import (
    EARTHDATA_HOST,
    SYSTEM_NETRC,
    check_earthdata_credentials,
    find_netrc_path,
    get_cddis_session,
    validate_netrc,
)


# =============================================================================
# Helpers
# =============================================================================


def _write_netrc(path: Path, content: str = None, mode: int = 0o600):
    if content is None:
        content = (
            f"machine {EARTHDATA_HOST}\n"
            "  login alice\n"
            "  password s3cr3t\n"
        )
    path.write_text(content)
    path.chmod(mode)
    return path


# =============================================================================
# find_netrc_path
# =============================================================================


class TestFindNetrcPath:
    def test_returns_none_when_no_sources(self, monkeypatch, tmp_path):
        monkeypatch.delenv('NETRC', raising=False)
        # Point SYSTEM_NETRC and home to non-existent paths
        monkeypatch.setattr(cddis_auth, 'SYSTEM_NETRC', tmp_path / 'absent_sys')
        monkeypatch.setattr(Path, 'home', staticmethod(lambda: tmp_path / 'absent_home'))
        assert find_netrc_path() is None

    def test_env_variable_takes_priority(self, monkeypatch, tmp_path):
        env = _write_netrc(tmp_path / 'env_netrc')
        monkeypatch.setenv('NETRC', str(env))
        # System-wide also exists but should be skipped
        sys_p = _write_netrc(tmp_path / 'sys_netrc')
        monkeypatch.setattr(cddis_auth, 'SYSTEM_NETRC', sys_p)
        assert find_netrc_path() == env

    def test_warns_when_env_var_points_to_missing_file(
            self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv('NETRC', str(tmp_path / 'absent'))
        # System-wide path also absent → expect None and a warning
        monkeypatch.setattr(cddis_auth, 'SYSTEM_NETRC', tmp_path / 'absent_sys')
        monkeypatch.setattr(Path, 'home', staticmethod(lambda: tmp_path / 'absent_home'))
        result = find_netrc_path()
        assert result is None
        assert any('non-existent' in r.message for r in caplog.records)

    def test_falls_back_to_system_netrc(self, monkeypatch, tmp_path):
        monkeypatch.delenv('NETRC', raising=False)
        sys_p = _write_netrc(tmp_path / 'sys_netrc')
        monkeypatch.setattr(cddis_auth, 'SYSTEM_NETRC', sys_p)
        # Make ~/.netrc absent
        monkeypatch.setattr(Path, 'home', staticmethod(lambda: tmp_path / 'absent_home'))
        assert find_netrc_path() == sys_p

    def test_falls_back_to_home_netrc(self, monkeypatch, tmp_path):
        monkeypatch.delenv('NETRC', raising=False)
        home = tmp_path / 'home'
        home.mkdir()
        home_netrc = _write_netrc(home / '.netrc')
        monkeypatch.setattr(Path, 'home', staticmethod(lambda: home))
        # System netrc absent
        monkeypatch.setattr(cddis_auth, 'SYSTEM_NETRC',
                            tmp_path / 'sys_absent')
        assert find_netrc_path() == home_netrc


# =============================================================================
# validate_netrc
# =============================================================================


class TestValidateNetrc:
    def test_missing_file(self, tmp_path):
        ok, msg = validate_netrc(tmp_path / 'absent')
        assert ok is False
        assert 'not found' in msg

    def test_wrong_permissions_rejected(self, tmp_path):
        f = _write_netrc(tmp_path / 'netrc', mode=0o644)
        ok, msg = validate_netrc(f)
        assert ok is False
        assert '0o644' in msg or 'permissions' in msg

    def test_no_earthdata_entry(self, tmp_path):
        f = _write_netrc(tmp_path / 'netrc', content=(
            "machine other.example.com\n"
            "  login foo\n"
            "  password bar\n"
        ))
        ok, msg = validate_netrc(f)
        assert ok is False
        assert EARTHDATA_HOST in msg

    def test_incomplete_credentials(self, tmp_path):
        f = _write_netrc(tmp_path / 'netrc', content=(
            f"machine {EARTHDATA_HOST}\n"
            "  login \n"
            "  password \n"
        ))
        ok, msg = validate_netrc(f)
        assert ok is False
        assert 'Incomplete' in msg

    def test_unparseable_netrc(self, tmp_path):
        f = tmp_path / 'netrc'
        f.write_text("garbage that doesn't parse")
        f.chmod(0o600)
        ok, msg = validate_netrc(f)
        assert ok is False
        assert 'parse' in msg

    def test_valid_netrc(self, tmp_path):
        f = _write_netrc(tmp_path / 'netrc')
        ok, msg = validate_netrc(f)
        assert ok is True
        assert 'OK' in msg


# =============================================================================
# get_cddis_session
# =============================================================================


class TestGetCDDISSession:
    def test_raises_when_no_netrc_found(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cddis_auth, 'find_netrc_path', lambda: None)
        with pytest.raises(FileNotFoundError, match="Earthdata"):
            get_cddis_session()

    def test_raises_when_netrc_invalid(self, monkeypatch, tmp_path):
        f = _write_netrc(tmp_path / 'netrc', mode=0o644)
        monkeypatch.setattr(cddis_auth, 'find_netrc_path', lambda: f)
        with pytest.raises(ValueError):
            get_cddis_session()

    def test_session_uses_basic_auth_from_netrc(self, monkeypatch, tmp_path):
        f = _write_netrc(tmp_path / 'netrc')
        monkeypatch.setattr(cddis_auth, 'find_netrc_path', lambda: f)
        session = get_cddis_session()
        assert session.auth == ('alice', 's3cr3t')


# =============================================================================
# check_earthdata_credentials
# =============================================================================


class TestCheckEarthdataCredentials:
    def test_missing_netrc_returns_false(self, monkeypatch):
        monkeypatch.setattr(cddis_auth, 'find_netrc_path', lambda: None)
        ok, msg = check_earthdata_credentials()
        assert ok is False
        assert 'not found' in msg

    def test_valid_netrc_returns_true(self, monkeypatch, tmp_path):
        f = _write_netrc(tmp_path / 'netrc')
        monkeypatch.setattr(cddis_auth, 'find_netrc_path', lambda: f)
        ok, msg = check_earthdata_credentials()
        assert ok is True
        assert 'OK' in msg
