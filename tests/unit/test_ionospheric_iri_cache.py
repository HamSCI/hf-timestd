"""
Tests for the IRI layer-height cache in `IonosphericModel` (review P-M11).

The cache key encodes the query's 5-minute slot and IRI is deterministic
for a fixed (slot, lat, lon), so a cache hit is always the exact value
for that slot. P-M11 removed the wall-clock TTL that was forcing needless
recomputes (and was incoherent under reanalysis); eviction is LRU.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hf_timestd.core.ionospheric_model import IonosphericModel


class _FakeIRIModule:
    """Minimal stand-in for the iri2020 module — counts IRI() calls."""

    def __init__(self):
        self.calls = 0

    def IRI(self, time, altkmrange, glat, glon):  # noqa: N802 (IRI API name)
        self.calls += 1
        return {"hmF2": 300.0, "hmF1": 200.0, "hmE": 110.0, "foF2": 8.0}


def _model_with_fake_iri():
    """An IonosphericModel wired to a call-counting fake IRI module."""
    model = IonosphericModel(enable_iri=False)  # skip the real availability probe
    fake = _FakeIRIModule()
    model._iri_available = True
    model._iri_module = fake
    model._iri_version = "2020"
    return model, fake


def test_same_slot_query_is_a_cache_hit():
    """A repeat query for the same slot reuses the cached result."""
    model, fake = _model_with_fake_iri()
    ts = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)

    first = model._get_iri_heights(ts, 40.0, -105.0)
    second = model._get_iri_heights(ts, 40.0, -105.0)

    assert fake.calls == 1
    assert model.stats["iri_cache_hits"] == 1
    assert first is second


def test_aged_cache_entry_is_still_valid():
    """P-M11: no wall-clock TTL — an entry created hours ago is still a
    valid hit for its slot. On the pre-fix code the adaptive 300-1800 s
    TTL would have rejected this and recomputed."""
    model, fake = _model_with_fake_iri()
    ts = datetime(2026, 5, 19, 3, 0, 0, tzinfo=timezone.utc)  # night hour

    model._get_iri_heights(ts, 40.0, -105.0)
    assert fake.calls == 1

    # Backdate the cached entry's creation time by 6 hours — far past any
    # TTL the old adaptive scheme would have used.
    (key,) = list(model._iri_cache)
    model._iri_cache[key].timestamp -= timedelta(hours=6)

    model._get_iri_heights(ts, 40.0, -105.0)
    assert fake.calls == 1  # still a hit — not recomputed
    assert model.stats["iri_cache_hits"] == 1


def test_distinct_slots_each_compute_once():
    """Different 5-minute slots are distinct keys — one IRI call each."""
    model, fake = _model_with_fake_iri()
    base = datetime(2026, 5, 19, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(3):
        model._get_iri_heights(base + timedelta(minutes=5 * i), 40.0, -105.0)
    assert fake.calls == 3
    assert len(model._iri_cache) == 3


def test_lru_eviction_keeps_recently_used():
    """At capacity, the least-recently-used entry is evicted — a touched
    entry survives an insertion that a FIFO cache would have dropped it for."""
    model, fake = _model_with_fake_iri()
    model._iri_cache_max_size = 3
    base = datetime(2026, 5, 19, 0, 0, 0, tzinfo=timezone.utc)
    slots = [base + timedelta(minutes=5 * i) for i in range(3)]

    for ts in slots:
        model._get_iri_heights(ts, 40.0, -105.0)
    assert len(model._iri_cache) == 3

    # Touch slot 0 so it becomes most-recently-used (slot 1 is now the LRU).
    model._get_iri_heights(slots[0], 40.0, -105.0)

    # A fourth slot pushes the cache over capacity.
    model._get_iri_heights(base + timedelta(minutes=15), 40.0, -105.0)
    assert len(model._iri_cache) == 3

    key0 = model._location_key(40.0, -105.0, slots[0])
    key1 = model._location_key(40.0, -105.0, slots[1])
    assert key0 in model._iri_cache  # kept — recently touched
    assert key1 not in model._iri_cache  # evicted — least recently used


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
