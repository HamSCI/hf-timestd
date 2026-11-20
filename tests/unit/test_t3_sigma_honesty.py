import math

import pytest

from hf_timestd.core.t3_reference_errors import ReferencePair
from hf_timestd.core.t3_sigma_honesty import (
    CALIBRATION_MAX_AGE_S, HourlyHonesty, SigmaHonesty)

DAY = 1_700_000_000 // 86400 * 86400  # a UTC midnight


def pair(err):
    return ReferencePair("T6", err, 0.0)


def fill(h, hour, errs, sigma, start=None):
    base = DAY + hour * 3600 if start is None else start
    for i, e in enumerate(errs):
        h.add(base + 60 * i, pair(e), sigma)


def test_an_honest_day_hour():
    h = SigmaHonesty()
    fill(h, 14, [1.0 if i % 2 else -1.0 for i in range(60)], 0.5)
    r = h.by_hour()[14]
    assert (r.n, r.rms_z, r.floor_fraction, r.honest) == (60, 1.0, 1.0, True)
    assert type(r.rms_z) is float and type(r.floor_fraction) is float


def test_an_overconfident_night_hour():
    h = SigmaHonesty()
    fill(h, 4, [3.0] * 60, 1.5)
    r = h.by_hour()[4]
    assert r.rms_z == pytest.approx(2.0) and r.honest is True
    assert r.floor_fraction == 0.0
    h2 = SigmaHonesty()
    fill(h2, 4, [3.2] * 60, 1.5)
    r2 = h2.by_hour()[4]
    assert r2.rms_z == pytest.approx(2.1333, abs=1e-3) and r2.honest is False


def test_an_underconfident_hour_is_dishonest():
    h = SigmaHonesty()
    fill(h, 9, [0.5] * 10, 2.0)  # z = 0.25
    assert h.by_hour()[9].honest is False


def test_only_hours_with_data_are_listed():
    h = SigmaHonesty()
    fill(h, 3, [1.0] * 5, 1.0)
    assert list(h.by_hour()) == [3]


def test_bad_sigma_or_error_skips_the_minute():
    h = SigmaHonesty()
    base = DAY + 5 * 3600
    h.add(base, pair(1.0), None)
    h.add(base + 60, pair(1.0), float("nan"))
    h.add(base + 120, pair(1.0), 0.0)
    h.add(base + 180, pair(1.0), -1.0)
    h.add(base + 240, pair(math.nan), 1.0)
    assert h.by_hour() == {}


def test_a_repeated_minute_adds_nothing():
    h = SigmaHonesty()
    base = DAY + 5 * 3600
    h.add(base, pair(1.0), 1.0)
    h.add(base, pair(9.0), 1.0)
    h.add(base - 60, pair(9.0), 1.0)
    assert h.by_hour()[5].n == 1


def test_minutes_older_than_72_hours_drop_out():
    h = SigmaHonesty()
    assert CALIBRATION_MAX_AGE_S == 259200
    fill(h, 2, [1.0] * 10, 1.0)
    h.add(DAY + 2 * 3600 + CALIBRATION_MAX_AGE_S + 3600, pair(1.0), 1.0)
    out = h.by_hour()
    assert out[3].n == 1 and 2 not in out


def test_seed_restores_and_skips_malformed_rows():
    h = SigmaHonesty()
    base = DAY + 7 * 3600
    rows = [
        {"utc_minute": base + 60 * i, "t3": {"sigma_ms": 2.0},
         "t3_shadow": {"t3_minus_t6_ms": 2.0}} for i in range(3)]
    rows += [{}, None, "x", {"utc_minute": "a"}, {"utc_minute": base + 999},
             {"utc_minute": base + 600, "t3": None,
              "t3_shadow": {"t3_minus_t6_ms": 1.0}},
             {"utc_minute": base + 660, "t3": {"sigma_ms": 2.0}}]
    h.seed(rows)
    r = h.by_hour()[7]
    assert r.n == 3 and r.rms_z == pytest.approx(1.0)
