"""Shim: canonical home is hamsci_dsp.io.authority_snapshot_store (split design §5.2).

Re-exports the moved data layer until the last hf-timestd consumer
imports hamsci-dsp directly, then dies.
"""
from hamsci_dsp.io.authority_snapshot_store import *  # noqa: F401,F403
