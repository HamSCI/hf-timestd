"""
I/O Module for hf-timestd Data Products

Provides schema-validated writing and reading with ISO GUM uncertainty
propagation and quality filtering. Two storage backends:

- ``DataProductWriter`` / ``DataProductReader``: HDF5 (the original,
  per-product per-day file layout).
- ``SqliteDataProductWriter``: SQLite (Phase 1 of the migration; see
  ``docs/HDF5-TO-SQLITE-MIGRATION.md``). Same constructor signature
  as the HDF5 writer; producers can opt into dual-write while the
  SQLite path is being verified.
"""

from .hdf5_writer import DataProductWriter
from .hdf5_reader import DataProductReader
from .sqlite_writer import SqliteDataProductWriter
from .dual_writer import DualWriter, make_data_product_writer
from .uncertainty import ISOGUMCalculator, UncertaintyBudget
from .calibration_file import CalibrationFileWriter

__all__ = [
    'DataProductWriter',
    'DataProductReader',
    'SqliteDataProductWriter',
    'DualWriter',
    'make_data_product_writer',
    'ISOGUMCalculator',
    'UncertaintyBudget',
    'CalibrationFileWriter',
]
