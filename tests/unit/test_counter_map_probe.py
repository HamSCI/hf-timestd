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


def test_restart_never_publishes_the_old_map_on_the_new_counter():
    c = FakeControl()
    p = CounterMapProbe(c)
    old = p.refresh("X", 1, 24000)
    assert old is not None
    c.make = lambda b: reply(b - 90, k0=K0 + 7, epoch_s=EPOCH_S + 3600)
    for _ in range(4):
        cmap = p.refresh("X", 1, 24000)
        assert cmap is None or abs(cmap.input_epoch_gps_s - (EPOCH_S + 3600)) < 1
    assert cmap is not None


def test_new_run_starts_a_full_burst_at_once():
    c = FakeControl()
    p = CounterMapProbe(c)
    p.refresh("X", 1, 24000)
    c.make = lambda b: reply(b - 90, k0=K0 + 7, epoch_s=EPOCH_S + 3600)
    before = c.calls
    cmap = p.refresh("X", 1, 24000)
    assert c.calls - before == 5
    assert cmap is not None and cmap.input_epoch_gps_s > EPOCH_S + 3000


def test_dead_channel_polls_once_per_tick_after_a_drop():
    c = FakeControl()
    clk = Clock()
    p = CounterMapProbe(c, mono_fn=clk)
    p.refresh("X", 1, 24000)
    c.fail = True
    clk.t += 13 * 3600
    assert p.refresh("X", 1, 24000) is None
    before = c.calls
    for _ in range(3):
        clk.t += 60
        assert p.refresh("X", 1, 24000) is None
    assert c.calls - before == 3


def test_failed_polls_log_once_per_streak_and_once_on_recovery(caplog):
    c = FakeControl()
    p = CounterMapProbe(c)
    p.refresh("X", 1, 24000)
    c.fail = True
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            p.refresh("X", 1, 24000)
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1
    caplog.clear()
    c.fail = False
    with caplog.at_level(logging.INFO):
        p.refresh("X", 1, 24000)
        p.refresh("X", 1, 24000)
    rec = [r for r in caplog.records if "recovered" in r.getMessage()]
    assert len(rec) == 1
    c.fail = True
    caplog.clear()
    with caplog.at_level(logging.INFO):
        p.refresh("X", 1, 24000)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_refresh_counter_maps_caps_its_time_and_rotates():
    from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
    clk = Clock()
    seen = []

    class Probe:
        def refresh(self, ch, ssrc, rate, time_left_s=None):
            seen.append((ch, rate))
            clk.t += 2.0
            return None

    def rec(rate):
        return types.SimpleNamespace(
            channel_info=types.SimpleNamespace(ssrc=1, sample_rate=rate),
            config=types.SimpleNamespace(sample_rate=rate + 1),
            publish_counter_map=lambda m: None)

    core = CoreRecorderV2.__new__(CoreRecorderV2)
    core.recorders = {f"C{i}": rec(24000) for i in range(5)}
    core._t6_channel_info = None
    core._counter_probe = Probe()
    core._counter_maps = {}
    core._t6_counter_map = None
    core._counter_rr = 0
    core._counter_pub_failing = set()
    core._counter_mono = clk
    core._refresh_counter_maps()
    assert [c for c, _ in seen] == ["C0", "C1", "C2"]      # 2 s each, 5 s cap
    assert all(r == 24000 for _, r in seen)                # granted rate
    seen.clear()
    core._refresh_counter_maps()
    assert [c for c, _ in seen][0] == "C3"                  # skipped ones go first


def late(r, seconds):
    r.gps_time += int(seconds * 1e9)
    return r


def test_a_late_poll_is_not_a_new_run(caplog):
    c = FakeControl()
    p = CounterMapProbe(c)
    held = p.refresh("X", 1, 24000)
    c.make = lambda b: late(reply(b), 0.8)
    before = c.calls
    with caplog.at_level(logging.INFO):
        cmap = p.refresh("X", 1, 24000)
    assert c.calls - before == 1
    assert not [r for r in caplog.records if "new radiod run" in r.getMessage()]
    assert cmap is not None
    assert cmap.input_epoch_gps_s == held.input_epoch_gps_s
    assert cmap.input_index(rtp_of(c.block)) == c.block * ILEN


def test_a_late_poll_with_no_map_is_not_a_new_run(caplog):
    q = CounterMapProbe(FakeControl(lambda b: late(reply(b), 0.8)
                                    if (b // 3000) % 2 else reply(b)))
    with caplog.at_level(logging.INFO):
        cmap = q.refresh("Y", 1, 24000)
    assert not [r for r in caplog.records if "new radiod run" in r.getMessage()]
    assert cmap is not None and cmap.n_agree == 5


def test_a_real_step_with_a_late_poll_still_counts(caplog):
    c = FakeControl()
    p = CounterMapProbe(c)
    p.refresh("S", 1, 24000)
    n = {"i": 0}

    def make(b):
        n["i"] += 1
        r = reply(b, shift_blocks=1)
        return late(r, 0.8) if n["i"] == 2 else r
    c.make = make
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            p.refresh("S", 1, 24000)
    assert p.steps["S"] == 1
    assert len([r for r in caplog.records if r.levelno == logging.ERROR
                and "COUNTER MAP STEP S" in r.getMessage()]) == 1


def test_a_burst_stops_when_the_time_runs_out():
    clk = Clock()

    class SlowControl(FakeControl):
        def poll_status(self, ssrc, timeout):
            clk.t += 2.0
            return super().poll_status(ssrc, timeout)

    c = SlowControl()
    p = CounterMapProbe(c, mono_fn=clk)
    p.refresh("X", 1, 24000, time_left_s=5.0)
    assert c.calls == 3                      # polls at 0, 2, 4 s; stops at 6 s
    p.refresh("X", 1, 24000)
    p.refresh("X", 1, 24000)
    assert p.refresh("X", 1, 24000) is not None   # partial history completes


def test_recovery_logs_only_on_an_accepted_poll(caplog):
    c = FakeControl()
    p = CounterMapProbe(c)
    p.refresh("X", 1, 24000)
    c.fail = True
    p.refresh("X", 1, 24000)
    c.fail = False
    good = c.make
    c.make = lambda b: reply(b, rtp=False)           # a ghost answers
    with caplog.at_level(logging.INFO):
        p.refresh("X", 1, 24000)
    assert not [r for r in caplog.records if "recovered" in r.getMessage()]
    c.make = good
    with caplog.at_level(logging.INFO):
        p.refresh("X", 1, 24000)
    assert len([r for r in caplog.records if "recovered" in r.getMessage()]) == 1


def test_a_publish_error_does_not_starve_later_channels(caplog):
    from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
    seen = []

    class Probe:
        def refresh(self, ch, ssrc, rate, time_left_s=None):
            seen.append(ch)
            return None

    def boom(m):
        raise OSError("disk full")

    def rec(pub):
        return types.SimpleNamespace(
            channel_info=types.SimpleNamespace(ssrc=1, sample_rate=24000),
            config=types.SimpleNamespace(sample_rate=24000),
            publish_counter_map=pub)

    core = CoreRecorderV2.__new__(CoreRecorderV2)
    core.recorders = {"A": rec(boom), "B": rec(lambda m: None)}
    core._t6_channel_info = None
    core._counter_probe = Probe()
    core._counter_maps = {}
    core._t6_counter_map = None
    core._counter_rr = 0
    core._counter_pub_failing = set()
    core._counter_mono = Clock()
    with caplog.at_level(logging.INFO):
        core._refresh_counter_maps()
        core._refresh_counter_maps()
    assert seen == ["A", "B", "A", "B"]
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1
