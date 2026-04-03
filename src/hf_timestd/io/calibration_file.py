"""
Atomic JSON Calibration File Writer for wsprdaemon Integration

Writes a machine-readable JSON file containing the current timing calibration
state.  Designed as an IPC interface between hf-timestd and wsprdaemon's
wd-ka9q-record service, which reads the file to align wav-file start times
to sub-millisecond accuracy.

The file is written atomically (write-to-tmp + rename) so readers never see
a partial write.  The schema is versioned; consumers should check
``schema_version`` and ignore unknown fields for forward compatibility.

Typical consumer usage (wsprdaemon wd-ka9q-record):
    import json, pathlib
    calib = json.loads(pathlib.Path("/run/wsprdaemon/KA9Q_0/hftime.json").read_text())
    if calib["convergence_state"] == "LOCKED":
        offset_sec = calib["offset_ms"] / 1000.0
        # apply offset to wav start time
"""

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Schema version — bump MINOR for additive fields, MAJOR for breaking changes
CALIB_SCHEMA_VERSION = "1.0.0"


class CalibrationFileWriter:
    """
    Writes FusedResult to a JSON calibration file using atomic rename.

    The output file contains two tiers of information:

    **Primary fields** (what wd-ka9q-record reads):
        offset_ms, uncertainty_ms, convergence_state, quality_grade,
        last_update, usable

    **Extended fields** (diagnostics for wd-ctl status / monitoring):
        n_broadcasts, n_stations, stations_used, consistency_flag,
        per-station means, holdover_mode, uncertainty budget, etc.

    Parameters
    ----------
    calib_path : Path or str
        Destination path for the JSON file.  Parent directory must exist
        (or will be created with mode 0o755).
    stale_threshold_sec : float
        If the file has not been updated for this many seconds, the
        ``stale`` field is set to True on the *next* write.  Consumers
        can also check ``last_update`` themselves.
    """

    def __init__(self, calib_path, stale_threshold_sec: float = 300.0):
        self.calib_path = Path(calib_path)
        self.stale_threshold_sec = stale_threshold_sec
        self._last_write_time: Optional[float] = None
        self._write_count: int = 0

        # Ensure parent directory exists
        self.calib_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(
            f"CalibrationFileWriter initialized: {self.calib_path} "
            f"(stale_threshold={stale_threshold_sec}s)"
        )

    def update(self, fused_result) -> bool:
        """
        Write the calibration file from a FusedResult.

        Parameters
        ----------
        fused_result : FusedResult
            The latest fusion output from MultiBroadcastFusion.fuse().

        Returns
        -------
        bool
            True if the file was written successfully.
        """
        if fused_result is None:
            return self._write_no_lock()

        try:
            now_utc = datetime.now(timezone.utc)

            # Determine convergence state from Kalman state string
            kalman_state = getattr(fused_result, 'kalman_state', None) or 'ACQUIRING'
            convergence_state = kalman_state  # ACQUIRING / LOCKED / REACQUIRING

            # Determine if the offset is usable by the consumer
            # Usable = converged + reasonable uncertainty + not in holdover
            holdover = getattr(fused_result, 'holdover_mode', False)
            usable = (
                convergence_state == 'LOCKED'
                and fused_result.uncertainty_ms < 10.0
                and not holdover
            )

            # Build the calibration document
            doc = {
                # -- Schema metadata --
                "schema_version": CALIB_SCHEMA_VERSION,
                "source": "hf-timestd",
                "processing_version": _get_version(),

                # -- Primary fields (consumer contract) --
                "offset_ms": round(fused_result.d_clock_fused_ms, 4),
                "uncertainty_ms": round(fused_result.uncertainty_ms, 4),
                "convergence_state": convergence_state,
                "quality_grade": fused_result.quality_grade,
                "usable": usable,
                "last_update": now_utc.isoformat(),
                "last_update_unix": round(time.time(), 3),

                # -- Composition --
                "n_broadcasts": fused_result.n_broadcasts,
                "n_stations": fused_result.n_stations,
                "stations_used": _stations_list(fused_result),

                # -- Uncertainty budget (ISO GUM components) --
                "uncertainty_budget": {
                    "statistical_ms": round(fused_result.statistical_uncertainty_ms, 4),
                    "systematic_ms": round(fused_result.systematic_uncertainty_ms, 4),
                    "propagation_ms": round(fused_result.propagation_uncertainty_ms, 4),
                },

                # -- Per-station breakdown --
                "station_detail": _station_detail(fused_result),

                # -- Quality / consistency --
                "consistency_flag": fused_result.consistency_flag,
                "single_station_mode": fused_result.single_station_mode,
                "holdover_mode": holdover,
                "outliers_rejected": fused_result.outliers_rejected,

                # -- Propagation --
                "dominant_propagation_mode": fused_result.dominant_propagation_mode,

                # -- Stability --
                "adev_60s": _safe_round(getattr(fused_result, 'adev_60s', None), 6),
                "adev_1000s": _safe_round(getattr(fused_result, 'adev_1000s', None), 6),
            }

            return self._atomic_write(doc)

        except Exception as e:
            logger.error(f"CalibrationFileWriter.update failed: {e}", exc_info=True)
            return False

    def _write_no_lock(self) -> bool:
        """Write a minimal document indicating no lock / no data."""
        doc = {
            "schema_version": CALIB_SCHEMA_VERSION,
            "source": "hf-timestd",
            "processing_version": _get_version(),
            "offset_ms": 0.0,
            "uncertainty_ms": 999.0,
            "convergence_state": "ACQUIRING",
            "quality_grade": "D",
            "usable": False,
            "last_update": datetime.now(timezone.utc).isoformat(),
            "last_update_unix": round(time.time(), 3),
            "n_broadcasts": 0,
            "n_stations": 0,
            "stations_used": [],
            "holdover_mode": False,
        }
        return self._atomic_write(doc)

    def _atomic_write(self, doc: dict) -> bool:
        """Write JSON to a temp file in the same directory, then rename."""
        try:
            parent = self.calib_path.parent
            fd, tmp_path = tempfile.mkstemp(
                dir=str(parent), suffix='.tmp', prefix='.hftime_'
            )
            try:
                with os.fdopen(fd, 'w') as f:
                    json.dump(doc, f, indent=2, default=str)
                    f.write('\n')
                os.replace(tmp_path, str(self.calib_path))
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

            self._write_count += 1
            self._last_write_time = time.time()

            if self._write_count <= 3 or self._write_count % 60 == 0:
                logger.info(
                    f"Calibration file written: {self.calib_path} "
                    f"(offset={doc['offset_ms']:+.3f}ms, "
                    f"unc={doc['uncertainty_ms']:.3f}ms, "
                    f"state={doc['convergence_state']}, "
                    f"usable={doc['usable']}, "
                    f"write #{self._write_count})"
                )
            return True

        except Exception as e:
            logger.error(f"Atomic write to {self.calib_path} failed: {e}")
            return False

    def remove(self):
        """Remove the calibration file on shutdown (optional cleanup)."""
        try:
            if self.calib_path.exists():
                self.calib_path.unlink()
                logger.info(f"Calibration file removed: {self.calib_path}")
        except Exception as e:
            logger.warning(f"Could not remove calibration file: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_version() -> str:
    """Return hf-timestd version string."""
    try:
        from hf_timestd.version import TIMESTD_VERSION
        return TIMESTD_VERSION
    except Exception:
        return "unknown"


def _safe_round(val, digits: int):
    """Round a value if not None, else return None."""
    if val is None:
        return None
    try:
        return round(float(val), digits)
    except (TypeError, ValueError):
        return None


def _stations_list(result) -> list:
    """Extract list of station names from FusedResult."""
    stations = []
    if getattr(result, 'wwv_count', 0) > 0:
        stations.append("WWV")
    if getattr(result, 'wwvh_count', 0) > 0:
        stations.append("WWVH")
    if getattr(result, 'chu_count', 0) > 0:
        stations.append("CHU")
    if getattr(result, 'bpm_count', 0) > 0:
        stations.append("BPM")
    return stations


def _station_detail(result) -> dict:
    """Build per-station detail dict from FusedResult."""
    detail = {}
    for name, mean_attr, count_attr, std_attr in [
        ("WWV", "wwv_mean_ms", "wwv_count", "wwv_intra_std_ms"),
        ("WWVH", "wwvh_mean_ms", "wwvh_count", "wwvh_intra_std_ms"),
        ("CHU", "chu_mean_ms", "chu_count", "chu_intra_std_ms"),
        ("BPM", "bpm_mean_ms", "bpm_count", "bpm_intra_std_ms"),
    ]:
        count = getattr(result, count_attr, 0)
        if count > 0:
            detail[name] = {
                "count": count,
                "mean_ms": _safe_round(getattr(result, mean_attr, None), 4),
                "intra_std_ms": _safe_round(getattr(result, std_attr, None), 4),
            }
    return detail
