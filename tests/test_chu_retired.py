"""CHU (NRC Ottawa) ceased transmitting.  Michael, 2026-09-04: "CHU is gone.
Remove it from the code."  The FSK decoder, the coarse-time producer it fed
and the bootstrap coordinator that consumed coarse time — the only path by
which hf-timestd ever stepped a host clock — are gone (step 1, 8b33ed5).
Step 2 took CHU out as a station: enums, templates, constants, the fusion
record's per-CHU statistics and its vestigial ``reference_station`` label.
These tests keep them gone and make `validate` name the retired keys."""
import importlib
import unittest

from hf_timestd.cli import retired_key_issues


class ChuChainIsGone(unittest.TestCase):

    def test_the_chu_only_modules_do_not_exist(self):
        for mod in ("chu_fsk_decoder", "coarse_time_source", "coarse_time_writer",
                    "bootstrap_coordinator", "chrony_stepper"):
            with self.assertRaises(ModuleNotFoundError, msg=mod):
                importlib.import_module(f"hf_timestd.core.{mod}")

    def test_the_authority_manager_takes_no_bootstrap_coordinator(self):
        import inspect
        from hf_timestd.core.authority_manager import AuthorityManager
        self.assertNotIn("bootstrap_coordinator", inspect.signature(AuthorityManager).parameters)

    def test_validate_names_the_retired_coarse_time_and_bootstrap_keys(self):
        cfg = {"timing": {"coarse_time": {"enabled": True},
                          "authority_manager": {"bootstrap": {"enabled": True, "threshold_sec": 5.0}}}}
        msgs = [i["message"] for i in retired_key_issues(cfg)]
        self.assertEqual(len(msgs), 3, msgs)
        self.assertTrue(any("[timing.coarse_time] enabled" in m and "CHU" in m for m in msgs))
        self.assertTrue(any("[timing.authority_manager.bootstrap] enabled" in m and "chrony steps" in m for m in msgs))


class ChuIsNotAStation(unittest.TestCase):
    """Step 2: no enum, template, constant or record field names CHU."""

    def test_station_enums_have_no_chu(self):
        from hf_timestd.core.broadcast_specs import Station
        from hf_timestd.core.tick_matched_filter import StationType
        from hf_timestd.core.station_model import StationID
        from hf_timestd.models.broadcast_measurement import StationID as L1StationID
        from hf_timestd.models.measurement import StationID as L2StationID
        from hf_timestd.models.tone_detection import AnchorStation
        from hf_timestd.interfaces.data_models import StationType as IfStationType
        for enum in (Station, StationType, StationID, L1StationID, L2StationID, AnchorStation, IfStationType):
            self.assertNotIn("CHU", [m.value for m in enum], enum.__name__)

    def test_fourteen_broadcasts_and_no_chu_templates(self):
        from hf_timestd.core import broadcast_specs, tick_matched_filter, signal_templates, wwv_constants
        self.assertEqual(len(broadcast_specs.BROADCAST_SPECS), 14)
        self.assertFalse(any(k.startswith("CHU") for k in broadcast_specs.BROADCAST_SPECS))
        for mod, name in ((tick_matched_filter, "CHU_TEMPLATE"),
                          (signal_templates, "CHUAFSKTemplateGenerator"),
                          (signal_templates, "create_afsk_generator"),
                          (wwv_constants, "CHU_FREQUENCIES"),
                          (wwv_constants, "CHU_FSK_MARK_FREQ"),
                          (wwv_constants, "GEOGRAPHIC_CONSISTENCY_PAIRS")):
            self.assertFalse(hasattr(mod, name), name)
        self.assertEqual(set(wwv_constants.UNAMBIGUOUS_BOOTSTRAP_CHANNELS.values()), {"WWV"})

    def test_signal_generator_knows_three_stations(self):
        from hf_timestd.core.standard_signal_generator import StandardTimeSignalGenerator
        self.assertEqual(set(StandardTimeSignalGenerator.STATION_CONFIGS), {"WWV", "WWVH", "BPM"})

    def test_fusion_record_has_no_reference_station_or_chu_statistics(self):
        import inspect
        from hf_timestd.models import fusion as fusion_models
        from hf_timestd.models.fusion import L3FusionTiming
        from hf_timestd.core.multi_broadcast_fusion import (
            MultiBroadcastFusion, FusedResult, BroadcastCalibration,
        )
        self.assertFalse(hasattr(fusion_models, "ReferenceStation"))
        for field in ("reference_station", "chu_mean_ms", "chu_count", "chu_intra_std_ms"):
            self.assertNotIn(field, L3FusionTiming.model_fields, field)
            self.assertNotIn(field, FusedResult.__dataclass_fields__, field)
        self.assertNotIn("reference_station", BroadcastCalibration.__dataclass_fields__)
        self.assertNotIn("reference_station", inspect.signature(MultiBroadcastFusion).parameters)

    def test_legacy_calibration_file_naming_a_reference_station_still_loads(self):
        """Files written before step 2 carry 'reference_station'; the loader
        ignores the key rather than refusing the file."""
        import json, tempfile, time
        from pathlib import Path
        from hf_timestd.core.multi_broadcast_fusion import MultiBroadcastFusion
        with tempfile.TemporaryDirectory() as td:
            cal = Path(td) / "calibration.json"
            cal.write_text(json.dumps({"WWV_10.00": {
                "frequency_mhz": 10.0, "offset_ms": 0.1, "uncertainty_ms": 0.5,
                "n_samples": 100, "last_updated": time.time(),
                "reference_station": "CHU"}}))
            fusion = MultiBroadcastFusion(data_root=Path(td), calibration_file=cal)
            self.assertIn("WWV_10.00", fusion.calibration)
            self.assertFalse(hasattr(fusion.calibration["WWV_10.00"], "reference_station"))
