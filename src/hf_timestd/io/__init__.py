"""
I/O Module for hf-timestd Data Products

Provides schema-validated writing and reading with ISO GUM uncertainty
propagation and quality filtering.

Post-Phase-4 SQLite is the sole storage backend.  Construct writers /
readers via ``make_data_product_writer`` / ``make_data_product_reader``
(or the concrete classes ``SqliteDataProductWriter`` /
``SqliteDataProductReader``).  The factory's ``storage_config`` kwarg
honours only ``sqlite_path``; pre-Phase-4 keys (``write_hdf5`` /
``write_sqlite`` / ``read_sqlite``) are accepted-but-ignored for
caller compatibility.
"""

from .sqlite_writer import SqliteDataProductWriter
from .sqlite_reader import SqliteDataProductReader, make_data_product_reader
from .dual_writer import make_data_product_writer
from .uncertainty import ISOGUMCalculator, UncertaintyBudget
from .calibration_file import CalibrationFileWriter
from .authority_snapshot_store import AuthoritySnapshotStore

__all__ = [
    'SqliteDataProductWriter',
    'SqliteDataProductReader',
    'make_data_product_writer',
    'make_data_product_reader',
    'ISOGUMCalculator',
    'UncertaintyBudget',
    'CalibrationFileWriter',
    'AuthoritySnapshotStore',
]
