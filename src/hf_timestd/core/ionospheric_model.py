"""Shim: canonical home is hamsci_dsp.ionosphere.model (split design §5.2).

Re-exports the moved engine (IonosphericModel and friends) until the last
hf-timestd consumer imports hamsci-dsp directly, then dies.  The library
takes ``ionex_dir`` by injection; hf-timestd's default lives here.
"""
from pathlib import Path

from hamsci_dsp.ionosphere.model import *  # noqa: F401,F403
from hamsci_dsp.ionosphere.model import IonosphericModel as _LibIonosphericModel

#: hf-timestd's operational IONEX directory (the library default is None).
DEFAULT_IONEX_DIR = Path("/var/lib/timestd/ionex")


class IonosphericModel(_LibIonosphericModel):
    """hf-timestd-flavored model: /var/lib/timestd IONEX dir by default."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("ionex_dir", DEFAULT_IONEX_DIR)
        super().__init__(*args, **kwargs)
