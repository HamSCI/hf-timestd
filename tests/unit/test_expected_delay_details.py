"""Expected delay carries its propagation mode and ionospheric source."""
import types

import pytest

from hf_timestd.core.metrology_engine import MetrologyEngine

MINUTE = 1_790_000_040  # a whole minute, Unix seconds


def _engine(table):
    """Stub the predictor; real raw strings as hamsci-dsp emits them."""
    eng = object.__new__(MetrologyEngine)
    eng.frequency_mhz = 10.0
    eng.frequency_hz = 10.0e6
    calls = []

    def predict(station, utc_time=None):
        calls.append((station, utc_time))
        delay_ms, sigma_ms, mode, src = table.get(station, (0.0, 500.0, None, None))
        if mode is not None or src is not None:
            eng._last_prediction_meta = {
                "data_source": src, "model_confidence": 0.9, "propagation_mode": mode,
            }
        return delay_ms, 1000.0, sigma_ms

    eng._predict_geometric_delay = predict
    eng._calls = calls
    return eng


def test_details_carry_mode_and_source():
    eng = _engine({"WWV": (12.5, 0.5, "1F", "wamipe+giro"),
                   "WWVH": (60.0, 0.9, "2F", "iri")})
    d = eng.expected_delay_details(float(MINUTE), MINUTE)
    assert d["WWV"] == {"delay_s": 0.0125, "sigma_ms": 0.5, "mode": "1F",
                        "iono_source": "wamipe+giro", "iono_source_raw": "wamipe+giro"}
    assert "WWVH" in d
    assert d["WWVH"]["mode"] == "2F" and d["WWVH"]["iono_source"] == "iri"


def test_delays_are_the_details_delays():
    eng = _engine({"WWV": (12.5, 0.5, "1F", "giro"),
                   "WWVH": (60.0, 0.9, "2F", "iri")})
    d = eng.expected_delay_details(float(MINUTE), MINUTE)
    assert eng.expected_delays_s(float(MINUTE), MINUTE) == {
        s: v["delay_s"] for s, v in d.items()}
    assert "WWV" in d


def test_uses_the_given_time_only():
    eng = _engine({"WWV": (12.5, 0.5, "1F", "giro")})
    eng.expected_delay_details(123.0, MINUTE)
    assert eng._calls and all(t == 123.0 for _, t in eng._calls)


def test_meta_does_not_leak_between_stations():
    # WWV has meta; WWVH (later in catalogue order) has a delay but no meta.
    eng = _engine({"WWV": (12.5, 0.5, "1F", "giro")})
    order = []
    real = eng._predict_geometric_delay

    def predict(station, utc_time=None):
        order.append(station)
        if station == "WWVH":
            return 60.0, 1000.0, 0.9  # positive delay, sets no meta
        return real(station, utc_time)

    eng._predict_geometric_delay = predict
    d = eng.expected_delay_details(float(MINUTE), MINUTE)
    assert order.index("WWV") < order.index("WWVH")
    assert d["WWVH"]["mode"] == "unknown"
    assert d["WWVH"]["iono_source"] == "unknown"
    assert d["WWVH"]["iono_source_raw"] is None


@pytest.mark.parametrize("raw,norm", [
    ("wamipe", "wamipe"), ("wamipe+giro", "wamipe+giro"), ("giro", "giro"),
    ("iri", "iri"), ("parametric", "parametric"), ("static", "static"),
    ("fallback", "static"), ("legacy", "static"),
    ("climatological_fallback", "parametric"),
    ("vacuum_fallback", "vacuum"), ("matrix", "unknown"), (None, "unknown"),
    ("brand-new-source", "unknown"),
])
def test_iono_source_vocabulary(raw, norm):
    assert MetrologyEngine._normalize_iono_source(raw) == norm
    d = _engine({"WWV": (12.5, 0.5, "1F", raw)}).expected_delay_details(float(MINUTE), MINUTE)
    assert d["WWV"]["iono_source"] == norm
    assert d["WWV"]["iono_source_raw"] == raw


def test_unrecognised_source_logs_once(caplog):
    MetrologyEngine._iono_source_logged.discard("zzz-new")
    with caplog.at_level("WARNING"):
        MetrologyEngine._normalize_iono_source("zzz-new")
        MetrologyEngine._normalize_iono_source("zzz-new")
    msgs = [r for r in caplog.records if "zzz-new" in r.getMessage()]
    assert len(msgs) == 1


def _real_engine():
    eng = object.__new__(MetrologyEngine)
    eng.frequency_mhz = 10.0
    eng.frequency_hz = 10.0e6
    eng.arrival_matrix = None
    eng.precise_lat, eng.precise_lon = 40.0, -75.0
    return eng


def _details(eng):
    return eng.expected_delay_details(float(MINUTE), MINUTE)


def test_matrix_path_meta():
    eng = _real_engine()
    arrival = types.SimpleNamespace(
        expected_delay_ms=12.5, great_circle_km=1900.0, uncertainty_3sigma_ms=3.0,
        data_source="wamipe+giro", model_confidence=0.8, propagation_mode="2F")
    eng.arrival_matrix = types.SimpleNamespace(
        get_expected_arrivals=lambda dt: types.SimpleNamespace(
            get_arrival=lambda st, f: arrival))
    w = _details(eng)["WWV"]
    assert (w["mode"], w["iono_source"], w["iono_source_raw"]) == ("2F", "wamipe+giro", "wamipe+giro")
    assert w["sigma_ms"] == 1.0


def test_matrix_path_default_source_is_unknown():
    eng = _real_engine()
    arrival = types.SimpleNamespace(
        expected_delay_ms=12.5, great_circle_km=1900.0, uncertainty_3sigma_ms=3.0)
    eng.arrival_matrix = types.SimpleNamespace(
        get_expected_arrivals=lambda dt: types.SimpleNamespace(
            get_arrival=lambda st, f: arrival))
    w = _details(eng)["WWV"]
    assert (w["mode"], w["iono_source"], w["iono_source_raw"]) == ("1F", "unknown", "matrix")


def test_propagation_model_path_meta():
    eng = _real_engine()
    pred = types.SimpleNamespace(
        primary_delay_ms=14.0, distance_km=1900.0, primary_uncertainty_1sigma_ms=0.7,
        data_source="fallback", model_confidence=0.0, primary_mode="1E")
    eng._prop_model_fallback = types.SimpleNamespace(predict=lambda st, f, dt: pred)
    w = _details(eng)["WWV"]
    assert (w["mode"], w["iono_source"], w["iono_source_raw"]) == ("1E", "static", "fallback")


def test_vacuum_path_meta():
    eng = _real_engine()
    eng._prop_model_fallback = types.SimpleNamespace(
        predict=lambda st, f, dt: types.SimpleNamespace(primary_delay_ms=0.0))
    w = _details(eng)["WWV"]
    assert (w["mode"], w["iono_source"], w["iono_source_raw"]) == (
        "vacuum", "vacuum", "vacuum_fallback")
