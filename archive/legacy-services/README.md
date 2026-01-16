# Legacy Services

These services have been superseded by the Science-First Architecture (v5.0.0).

## Archived Files

### science_aggregator.py
**Superseded by:** `physics_fusion_service.py`

The original science aggregator read CSV files from Phase 2 analytics to compute TEC.
The new `PhysicsFusionService` reads L2 HDF5 files directly and produces L3 HDF5 output.

| Legacy | Current |
|--------|---------|
| `science_aggregator.py` | `physics_fusion_service.py` |
| CSV input | HDF5 input |
| CSV output | HDF5 output |
| `timestd-science-aggregator.service` | `timestd-physics.service` |

### timestd-science-aggregator.service
The systemd service file for the legacy science aggregator.

## Do Not Use

These files are preserved for reference only. The active pipeline uses:
- `physics_fusion_service.py` for L2 → L3 physics fusion
- `multi_broadcast_fusion.py` for L3 → D_clock fusion

Archived: 2026-01-16
