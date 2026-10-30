"""CounterMapProbe: each channel's counter map from its own status pair."""
import logging
import types

from hf_timestd.core.counter_map_probe import CounterMapProbe

F_IN = 64_800_000
ILEN = 1_296_000
OLEN = 480
K0 = 51
EPOCH_S = 1_800_000_000


def rtp_of(b, k0=K0, olen=OLEN):
    return (2**32 - olen + (b + 1 - k0) * olen) % 2**32


def reply(b, *, k0=K0, rate=24000, epoch_s=EPOCH_S, rtp=True, gps=True,
          shift_blocks=0):
    n_in = (b + shift_blocks) * ILEN
    return types.SimpleNamespace(
        rtp_timesnap=rtp_of(b, k0) if rtp else None,
        gps_time=int((epoch_s + n_in / F_IN) * 1e9) if gps else 0,
        filter_blocksize=ILEN, output_samprate=rate,
        frontend=types.SimpleNamespace(input_samples=n_in, input_samprate=F_IN))


class FakeControl:
    """Serves reply(block) with the block advancing one per call, or a script."""

    def __init__(self, make=None):
        self.calls = 0
        self.block = 100
        self.make = make or (lambda b: reply(b))
        self.fail = False

    def poll_status(self, ssrc, timeout):
        self.calls += 1
        if self.fail:
            raise TimeoutError("no answer")
        self.block += 3000          # one minute of 20 ms blocks
        return self.make(self.block)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_first_refresh_polls_five_times_then_one():
    c = FakeControl()
    p = CounterMapProbe(c)
    assert p.refresh("SHARED_10000", 1, 24000) is not None
    assert c.calls == 5
    p.refresh("SHARED_10000", 1, 24000)
    assert c.calls == 6


def test_ghost_reply_is_not_a_poll():
    for make in (lambda b: reply(b, rtp=False),
                 lambda b: reply(b, rate=12000),
                 lambda b: reply(b, gps=False)):
        p = CounterMapProbe(FakeControl(make))
        assert p.refresh("X", 1, 24000) is None


def test_refresh_returns_a_map_rolled_to_the_reply():
    c = FakeControl()
    p = CounterMapProbe(c)
    cmap = p.refresh("X", 1, 24000)
    newest = rtp_of(c.block)
    d = (cmap.rtp_ref - newest + 2**31) % 2**32 - 2**31
    assert abs(d) <= OLEN
    assert cmap.input_index(newest) == c.block * ILEN


def test_map_step_on_the_same_run_logs_once_and_counts(caplog):
    c = FakeControl()
    p = CounterMapProbe(c)
    p.refresh("SHARED_10000", 1, 24000)
    c.make = lambda b: reply(b, shift_blocks=1)     # radiod's index slips a block
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            p.refresh("SHARED_10000", 1, 24000)
    errs = [r for r in caplog.records if r.levelno == logging.ERROR
            and "COUNTER MAP STEP SHARED_10000" in r.getMessage()]
    assert len(errs) == 1
    assert p.steps["SHARED_10000"] == 1
    caplog.clear()
    with caplog.at_level(logging.INFO):
        p.refresh("SHARED_10000", 1, 24000)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert p.steps["SHARED_10000"] == 1


def test_new_radiod_run_replaces_the_map_without_a_step(caplog):
    c = FakeControl()
    p = CounterMapProbe(c)
    first = p.refresh("X", 1, 24000)
    c.make = lambda b: reply(b - 90, k0=K0 + 7, epoch_s=EPOCH_S + 3600)   # radiod restarted
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            cmap = p.refresh("X", 1, 24000)
    assert cmap.input_epoch_gps_s > first.input_epoch_gps_s + 3000
    assert p.steps.get("X", 0) == 0
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [r for r in caplog.records if r.levelno == logging.INFO
            and "new radiod run" in r.getMessage()]


def test_failed_polls_return_the_held_map_rolled_to_the_newest_rtp():
    c = FakeControl()
    p = CounterMapProbe(c)
    p.refresh("X", 1, 24000)
    newest = rtp_of(c.block)
    c.fail = True
    cmap = p.refresh("X", 1, 24000)
    assert cmap is not None
    assert cmap.input_index(newest) == c.block * ILEN


def test_map_is_dropped_before_it_can_alias(caplog):
    c = FakeControl()
    clk = Clock()
    p = CounterMapProbe(c, mono_fn=clk)
    assert p.refresh("X", 1, 24000) is not None
    c.fail = True
    clk.t += 11 * 3600
    assert p.refresh("X", 1, 24000) is not None       # still well inside reach
    clk.t += 2 * 3600                                  # 13 h without a fresh RTP
    with caplog.at_level(logging.INFO):
        assert p.refresh("X", 1, 24000) is None
        assert p.refresh("X", 1, 24000) is None
    drops = [r for r in caplog.records if "dropped" in r.getMessage()]
    assert len(drops) == 1
    c.fail = False                                     # recovery relearns
    for _ in range(3):
        cmap = p.refresh("X", 1, 24000)
    assert cmap is not None
