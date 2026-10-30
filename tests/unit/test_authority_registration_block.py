from hf_timestd.core.authority_manager import registration_block
from hf_timestd.core.registration_acquirer import Registration
from hf_timestd.core.registration_store import RegistrationStore


def test_block_from_acquired_summary(tmp_path):
    st = RegistrationStore(
        tmp_path / "reg", tmp_path / "registration.json", time_fn=lambda: 1000.0
    )
    st.write_summary(
        Registration("ep-1", 1000, 100.0, 24000, 0.7, channel="fused"),
        ["SHARED_10000", "WWV_20000"],
        "ACQUIRED",
        {"raw_pair_residual_ms": 16.7},
    )
    b = registration_block(st, admitted=True)
    assert b == {
        "source": "hf_acquired",
        "state": "ACQUIRED",
        "sigma_ms": 0.7,
        "counter_epoch_id": "ep-1",
        "raw_pair_residual_ms": 16.7,
        "contributing": ["SHARED_10000", "WWV_20000"],
        "stations": [],
        "age_s": 0.0,
    }


def test_block_from_acquired_summary_withholds_the_source_unadmitted(tmp_path):
    """Ruling B (2026-10-30): until T3 is admitted the block keeps the
    record but claims no source."""
    st = RegistrationStore(
        tmp_path / "reg", tmp_path / "registration.json", time_fn=lambda: 1000.0
    )
    st.write_summary(
        Registration("ep-1", 1000, 100.0, 24000, 0.7, channel="fused"),
        ["SHARED_10000", "WWV_20000"],
        "ACQUIRED",
        {"raw_pair_residual_ms": 16.7},
    )
    b = registration_block(st)
    assert b["source"] == "label" and b["state"] == "ACQUIRED"
    assert b["admission"] == "not admitted (shadow run pending)"


def test_block_without_summary_is_label(tmp_path):
    st = RegistrationStore(tmp_path / "reg", tmp_path / "registration.json")
    assert registration_block(st) == {
        "source": "label",
        "state": "UNKNOWN",
        "sigma_ms": None,
        "counter_epoch_id": None,
        "raw_pair_residual_ms": None,
        "contributing": [],
        "stations": [],
        "age_s": None,
    }


def test_block_witness_state_carries_t6_residual(tmp_path):
    """WITNESS: the plane in force is T6's -- the acquired plane only
    witnesses it, so source stays "label" but the state and the residual
    against T6 surface for provenance (controller ruling, task 10)."""
    st = RegistrationStore(
        tmp_path / "reg", tmp_path / "registration.json", time_fn=lambda: 1000.0
    )
    st.write_summary(
        Registration("ep-1", 1000, 100.0, 24000, 0.7, channel="fused"),
        ["SHARED_10000", "WWV_20000"],
        "WITNESS",
        {"witness_of": "T6", "residual_vs_t6_ms": 2.4},
    )
    b = registration_block(st)
    assert b == {
        "source": "label",
        "state": "WITNESS",
        "sigma_ms": 0.7,
        "counter_epoch_id": "ep-1",
        "raw_pair_residual_ms": None,
        "contributing": ["SHARED_10000", "WWV_20000"],
        "stations": [],
        "age_s": 0.0,
        "residual_vs_t6_ms": 2.4,
    }


def test_block_reports_stale_summary_as_stale(tmp_path):
    """A dead metrology writer must not leave the block reporting its last
    live state forever (review F1/F2, task 10): once the summary ages past
    the store's own stale_s window, the block degrades to STALE/label but
    keeps the last-known counter_epoch_id/sigma_ms/contributing/stations
    so a reader can see what died."""
    clock = {"t": 1000.0}
    st = RegistrationStore(
        tmp_path / "reg",
        tmp_path / "registration.json",
        stale_s=300,
        time_fn=lambda: clock["t"],
    )
    st.write_summary(
        Registration("ep-1", 1000, 100.0, 24000, 0.7, channel="fused"),
        ["SHARED_10000"],
        "ACQUIRED",
        {"raw_pair_residual_ms": 1.0},
    )
    clock["t"] = 1400.0  # 400 s later, past stale_s=300
    b = registration_block(st)
    assert b == {
        "source": "label",
        "state": "STALE",
        "sigma_ms": 0.7,
        "counter_epoch_id": "ep-1",
        "raw_pair_residual_ms": 1.0,
        "contributing": ["SHARED_10000"],
        "stations": [],
        "age_s": 400.0,
    }


def test_block_conflict_and_bootstrap_are_label(tmp_path):
    """CONFLICT and BOOTSTRAP are both named explicitly by the controller
    ruling's mapping (-> source="label"); each gets its own case rather
    than relying on sharing the no-summary branch's code path."""
    for state in ("CONFLICT", "BOOTSTRAP"):
        st = RegistrationStore(
            tmp_path / f"reg-{state}",
            tmp_path / f"registration-{state}.json",
            time_fn=lambda: 1000.0,
        )
        st.write_summary(None, [], state, {})
        b = registration_block(st)
        assert b["source"] == "label"
        assert b["state"] == state
        assert b["age_s"] == 0.0
