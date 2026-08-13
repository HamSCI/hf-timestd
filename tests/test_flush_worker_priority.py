"""Archive compression must yield to capture, not outrank it.

The flush worker inherited the unit's Nice=-10, so zstd ran at *elevated*
priority. Every channel's chunk closes on the same 300 s wall-clock epoch,
so six 57.6 MB compressions fired at once, phase-locked to minute-aligned
metrology and 2-minute WSPR cycles, outranking the capture path.

Compression has seconds of slack; capture has none. This is the "run
between the bursts" idea expressed as a priority, which needs no model of
when the bursts are.
"""
import os
import threading

import pytest

from hf_timestd.core.binary_archive_writer import (
    _FLUSH_WORKER_NICE, _renice_current_thread,
)


def test_nice_increment_is_positive():
    """Positive means lower priority. A negative value here would restore
    exactly the bug: compression outranking capture."""
    assert _FLUSH_WORKER_NICE > 0


def test_renice_lowers_only_the_calling_thread():
    """Nice is per-task on Linux. If this leaked to the process, it would
    deprioritise the capture threads — the opposite of the intent."""
    main_before = os.getpriority(os.PRIO_PROCESS, 0)
    seen = {}

    def worker():
        _renice_current_thread(_FLUSH_WORKER_NICE)
        seen["worker"] = os.getpriority(os.PRIO_PROCESS, 0)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert seen["worker"] == main_before + _FLUSH_WORKER_NICE, (
        "worker thread did not take the lower priority")
    assert os.getpriority(os.PRIO_PROCESS, 0) == main_before, (
        "renice leaked to the calling thread")


def test_renice_failure_is_survivable():
    """A daemon worker must not die because it could not renice; the flush
    still has to happen, just at the old priority."""
    _renice_current_thread(0)          # no-op, must not raise
    _renice_current_thread(1)


def test_worker_loop_renices_before_doing_work():
    """Structural: the renice must happen before the queue loop, or the
    first compression after every restart runs at the wrong priority."""
    import inspect
    from hf_timestd.core import binary_archive_writer as mod

    src = inspect.getsource(mod.BinaryArchiveWriter._flush_worker_loop)
    assert "_renice_current_thread" in src
    assert src.index("_renice_current_thread") < src.index("while not self._flush_stop")


def test_not_sched_idle():
    """SCHED_IDLE would starve the worker on a busy host, and the bounded
    queue's overflow policy is to DROP archive chunks rather than
    backpressure. Nice yields without that cliff."""
    import inspect
    from hf_timestd.core import binary_archive_writer as mod

    src = inspect.getsource(mod)
    # Check for the CALL, not the word — the docstring explains at length
    # why SCHED_IDLE is wrong here, and an earlier version of this test
    # matched its own rationale.
    assert "sched_setscheduler" not in src, (
        "scheduling class must stay SCHED_OTHER; nice is the lever")
