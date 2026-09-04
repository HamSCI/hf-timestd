"""CHU (NRC Ottawa) ceased transmitting.  Michael, 2026-09-04: "CHU is gone.
Remove it from the code."  The FSK decoder, the coarse-time producer it fed
and the bootstrap coordinator that consumed coarse time — the only path by
which hf-timestd ever stepped a host clock — are gone.  These tests keep
them gone and make `validate` name the retired keys."""
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
