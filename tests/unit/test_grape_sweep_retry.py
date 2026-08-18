"""The grape-daily catch-up sweep must actually be able to spawn its child.

Regression test for hf-timestd#26.  The sweep re-invoked itself with
``subprocess.run([sys.argv[0], ...])``.  Under ``python3 -m hf_timestd.cli``
— which is how grape-daily.service runs it — ``sys.argv[0]`` is the module
FILE path, and that file is not executable (and its ``#!/usr/bin/env python3``
shebang would resolve to system Python, which cannot import hf_timestd).

Spawning therefore raised PermissionError.  ``check=False`` only suppresses a
non-zero exit *status*, not a failure to spawn at all, so the exception
escaped and killed an otherwise fully successful run with exit 1 — and the
backfill never ran once since it was added on 2026-08-05.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hf_timestd.cli import _grape_sweep_retry


class _Recorder:
    """Stand-in for subprocess.run that records the call."""

    def __init__(self, exc=None, returncode=0):
        self.exc = exc
        self.returncode = returncode
        self.cmd = None
        self.env = None
        self.calls = 0

    def __call__(self, cmd, *args, **kwargs):
        self.calls += 1
        self.cmd = cmd
        self.env = kwargs.get('env')
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.returncode)


def _run(monkeypatch, rec):
    monkeypatch.setattr(subprocess, 'run', rec)
    return _grape_sweep_retry('20260814', Path('/var/lib/timestd'),
                              Path('/etc/hf-timestd/timestd-config.toml'))


def test_spawns_the_interpreter_not_a_module_file(monkeypatch):
    """argv[0] must be an executable interpreter, never a .py path."""
    rec = _Recorder()
    _run(monkeypatch, rec)
    assert rec.cmd[0] == sys.executable
    assert not rec.cmd[0].endswith('.py')
    assert rec.cmd[1:3] == ['-m', 'hf_timestd.cli']


def test_passes_the_day_and_paths_through(monkeypatch):
    rec = _Recorder()
    _run(monkeypatch, rec)
    assert 'grape' in rec.cmd and 'daily' in rec.cmd
    assert '--date' in rec.cmd
    assert rec.cmd[rec.cmd.index('--date') + 1] == '20260814'
    assert '/var/lib/timestd' in rec.cmd
    assert '/etc/hf-timestd/timestd-config.toml' in rec.cmd


def test_keeps_the_recursion_guard(monkeypatch):
    """GRAPE_SWEEP=1 stops the child from launching its own sweep."""
    rec = _Recorder()
    _run(monkeypatch, rec)
    assert rec.env is not None
    assert rec.env.get('GRAPE_SWEEP') == '1'


def test_spawn_failure_is_not_fatal(monkeypatch):
    """The bug: a PermissionError here used to kill the whole run."""
    rec = _Recorder(exc=PermissionError(13, 'Permission denied'))
    _run(monkeypatch, rec)          # must not raise
    assert rec.calls == 1


def test_missing_interpreter_is_not_fatal(monkeypatch):
    rec = _Recorder(exc=FileNotFoundError(2, 'No such file or directory'))
    _run(monkeypatch, rec)          # must not raise


def test_child_nonzero_exit_is_not_fatal(monkeypatch):
    """Per-day failures are non-fatal by design; only that day is lost."""
    rec = _Recorder(returncode=1)
    _run(monkeypatch, rec)          # must not raise
