"""
Unit tests for hf_timestd.venv_check

Helpers that detect whether the current process is running inside a Python
virtual environment, and either warn or exit when it isn't.
"""

import sys
from unittest.mock import patch

import pytest

from hf_timestd import venv_check
from hf_timestd.venv_check import in_venv, require_venv, warn_if_not_venv


# =============================================================================
# in_venv()
# =============================================================================


class TestInVenv:
    def test_real_prefix_is_venv(self, monkeypatch):
        # Some virtualenv versions set sys.real_prefix
        monkeypatch.delenv('VIRTUAL_ENV', raising=False)
        monkeypatch.setattr(sys, 'real_prefix', '/some/system/prefix',
                            raising=False)
        # Make sure base_prefix==prefix so only real_prefix triggers
        monkeypatch.setattr(sys, 'base_prefix', sys.prefix)
        assert in_venv() is True

    def test_base_prefix_differs_is_venv(self, monkeypatch):
        # Modern venv: sys.base_prefix differs from sys.prefix
        monkeypatch.delenv('VIRTUAL_ENV', raising=False)
        if hasattr(sys, 'real_prefix'):
            monkeypatch.delattr(sys, 'real_prefix', raising=False)
        monkeypatch.setattr(sys, 'base_prefix', '/system/prefix')
        monkeypatch.setattr(sys, 'prefix', '/venv/prefix')
        assert in_venv() is True

    def test_virtual_env_envvar_is_venv(self, monkeypatch):
        # Activated venv sets VIRTUAL_ENV
        monkeypatch.setenv('VIRTUAL_ENV', '/some/venv')
        if hasattr(sys, 'real_prefix'):
            monkeypatch.delattr(sys, 'real_prefix', raising=False)
        monkeypatch.setattr(sys, 'base_prefix', sys.prefix)
        assert in_venv() is True

    def test_no_venv_indicators_returns_false(self, monkeypatch):
        monkeypatch.delenv('VIRTUAL_ENV', raising=False)
        if hasattr(sys, 'real_prefix'):
            monkeypatch.delattr(sys, 'real_prefix', raising=False)
        monkeypatch.setattr(sys, 'base_prefix', sys.prefix)
        assert in_venv() is False


# =============================================================================
# require_venv() and warn_if_not_venv()
# =============================================================================


class TestRequireVenv:
    def test_returns_true_when_in_venv(self):
        with patch.object(venv_check, 'in_venv', return_value=True):
            assert require_venv() is True
            assert require_venv(exit_on_fail=False) is True

    def test_exits_with_one_when_not_in_venv_default(self, capsys):
        with patch.object(venv_check, 'in_venv', return_value=False):
            with pytest.raises(SystemExit) as exc_info:
                require_venv()
            assert exc_info.value.code == 1
        # The error message lands on stderr
        err = capsys.readouterr().err
        assert 'virtual environment' in err

    def test_returns_false_when_exit_disabled(self, capsys):
        with patch.object(venv_check, 'in_venv', return_value=False):
            assert require_venv(exit_on_fail=False) is False
        err = capsys.readouterr().err
        assert 'ERROR' in err


class TestWarnIfNotVenv:
    def test_no_exit_when_not_in_venv(self):
        # warn_if_not_venv must never raise SystemExit
        with patch.object(venv_check, 'in_venv', return_value=False):
            warn_if_not_venv()  # should return cleanly

    def test_silent_when_in_venv(self, capsys):
        with patch.object(venv_check, 'in_venv', return_value=True):
            warn_if_not_venv()
        # No stderr output when we're already in a venv
        assert capsys.readouterr().err == ''
