"""Expected delay carries its propagation mode and ionospheric source."""
from hf_timestd.core.metrology_engine import MetrologyEngine

MINUTE = 1_790_000_040  # a whole minute, Unix seconds


def _engine(table):
    eng = object.__new__(MetrologyEngine)
    eng.frequency_mhz = 10.0
    eng.frequency_hz = 10.0e6
    calls = []

    def predict(station, utc_time=None):
        calls.append((station, utc_time))
        delay_ms, sigma_ms, mode, src = table.get(station, (0.0, 500.0, None, None))
        if mode is not None:
            eng._last_prediction_meta = {
                "data_source": src, "model_confidence": 0.9, "propagation_mode": mode,
            }
        return delay_ms, 1000.0, sigma_ms

    eng._predict_geometric_delay = predict
    eng._calls = calls
    return eng


def test_details_carry_mode_and_source():
    eng = _engine({"WWV": (12.5, 0.5, "1F", "giro"),
                   "WWVH": (60.0, 0.9, "2F", "iri2020")})
    d = eng.expected_delay_details(float(MINUTE), MINUTE)
    assert d["WWV"] == {"delay_s": 0.0125, "sigma_ms": 0.5,
                        "mode": "1F", "iono_source": "giro"}
    if "WWVH" in d:
        assert d["WWVH"]["mode"] == "2F" and d["WWVH"]["iono_source"] == "iri2020"
    # each station keeps its own meta, not the last one's
    assert d["WWV"]["iono_source"] == "giro"


def test_delays_are_the_details_delays():
    eng = _engine({"WWV": (12.5, 0.5, "1F", "giro"),
                   "WWVH": (60.0, 0.9, "2F", "iri2020")})
    d = eng.expected_delay_details(float(MINUTE), MINUTE)
    assert eng.expected_delays_s(float(MINUTE), MINUTE) == {
        s: v["delay_s"] for s, v in d.items()}
    assert "WWV" in d


def test_uses_the_given_time_only():
    eng = _engine({"WWV": (12.5, 0.5, "1F", "giro")})
    eng.expected_delay_details(123.0, MINUTE)
    assert eng._calls and all(t == 123.0 for _, t in eng._calls)
