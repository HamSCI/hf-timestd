"""archive_totals: the recorder status sums what the archive writers placed and lost."""
from hf_timestd.core.core_recorder_v2 import archive_totals


def test_archive_totals_sum_channels_and_skip_unarchived():
    ch = {"a": {"archive": {"samples_placed": 10, "samples_lost": 2}},
          "b": {"archive": {"samples_placed": 5, "samples_lost": 0}},
          "c": {"archive": None},
          "d": {}}
    assert archive_totals(ch) == {"total_archive_samples_placed": 15,
                                  "total_archive_samples_lost": 2}
