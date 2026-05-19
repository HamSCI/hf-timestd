"""
I/O Module for hf-timestd Data Products

Provides schema-validated writing and reading with ISO GUM uncertainty
propagation and quality filtering. Two storage backends:

- ``DataProductWriter`` / ``DataProductReader``: HDF5 (the original,
  per-product per-day file layout).
- ``SqliteDataProductWriter`` / ``SqliteDataProductReader``: SQLite
  (the HDF5 → SQLite migration; see ``docs/HDF5-TO-SQLITE-MIGRATION.md``).
  Same constructor signatures as the HDF5 pair; producers opt into
  dual-write and consumers opt into SQLite reads independently while
  the SQLite path is being verified.

Use ``make_data_product_writer`` / ``make_data_product_reader`` to get
a backend-agnostic writer/reader selected by the ``[storage]`` config.
"""

from .hdf5_writer import DataProductWriter
from .hdf5_reader import DataProductReader
from .sqlite_writer import SqliteDataProductWriter
from .sqlite_reader import SqliteDataProductReader, make_data_product_reader
from .dual_writer import DualWriter, make_data_product_writer
from .uncertainty import ISOGUMCalculator, UncertaintyBudget
from .calibration_file import CalibrationFileWriter
from .authority_snapshot_store import AuthoritySnapshotStore

__all__ = [
    'DataProductWriter',
    'DataProductReader',
    'SqliteDataProductWriter',
    'SqliteDataProductReader',
    'DualWriter',
    'make_data_product_writer',
    'make_data_product_reader',
    'ISOGUMCalculator',
    'UncertaintyBudget',
    'CalibrationFileWriter',
    'AuthoritySnapshotStore',
]
