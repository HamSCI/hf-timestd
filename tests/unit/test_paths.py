"""
Unit tests for hf_timestd.paths

Covers:
- Channel name normalization (key, dir, display, round-trip)
- TimeStdPaths Phase 1 / Phase 2 directory layout
- Channel discovery across phase1, phase2, and legacy archives
- TOML config loading (production vs. test mode, error paths)
"""

import tempfile
from pathlib import Path

import pytest

from hf_timestd.paths import (
    TimeStdPaths,
    channel_name_to_dir,
    channel_name_to_key,
    channel_to_display_name,
    dir_to_channel_name,
    get_paths,
    load_paths_from_config,
)


# =============================================================================
# Channel name conversion helpers
# =============================================================================


class TestChannelNameToKey:
    @pytest.mark.parametrize("channel,expected", [
        ("SHARED_10000", "shared10000"),
        ("CHU_3330", "chu3330"),
        ("WWV_5000", "wwv5000"),
        ("WWV_25000", "wwv25000"),
    ])
    def test_canonical_format(self, channel, expected):
        assert channel_name_to_key(channel) == expected

    def test_non_canonical_falls_back_to_lowercase(self):
        # Unknown station prefix → fallback path strips spaces/underscores
        assert channel_name_to_key("Foo Bar") == "foobar"
        assert channel_name_to_key("Foo_Bar") == "foobar"


class TestChannelNameToDir:
    @pytest.mark.parametrize("channel", [
        "SHARED_10000", "CHU_3330", "WWV_5000", "WWV_25000",
    ])
    def test_canonical_passthrough(self, channel):
        assert channel_name_to_dir(channel) == channel

    def test_space_to_underscore_for_non_canonical(self):
        assert channel_name_to_dir("Foo Bar") == "Foo_Bar"

    def test_canonical_round_trip_with_dir_to_channel(self):
        for channel in ["SHARED_10000", "CHU_3330", "WWV_5000"]:
            assert dir_to_channel_name(channel_name_to_dir(channel)) == channel


class TestChannelToDisplayName:
    @pytest.mark.parametrize("channel,expected", [
        ("SHARED_10000", "SHARED 10 MHz"),
        ("WWV_5000", "WWV 5 MHz"),
        ("WWV_25000", "WWV 25 MHz"),
        ("CHU_3330", "CHU 3.33 MHz"),
        ("CHU_7850", "CHU 7.85 MHz"),
    ])
    def test_canonical_format(self, channel, expected):
        assert channel_to_display_name(channel) == expected

    def test_non_canonical_replaces_underscores(self):
        # Falls through when format doesn't match STATION_KILOHERTZ
        assert channel_to_display_name("Foo_Bar_Baz") == "Foo Bar Baz"


# =============================================================================
# TimeStdPaths layout
# =============================================================================


@pytest.fixture
def tmp_paths(tmp_path):
    """TimeStdPaths rooted at a fresh temp dir."""
    return TimeStdPaths(tmp_path)


class TestTimeStdPathsPhase1:
    def test_data_root_accepts_str_and_path(self, tmp_path):
        # str path
        p1 = TimeStdPaths(str(tmp_path))
        assert p1.data_root == tmp_path
        # Path object
        p2 = TimeStdPaths(tmp_path)
        assert p2.data_root == tmp_path

    def test_raw_buffer_layout(self, tmp_paths):
        root = tmp_paths.get_raw_buffer_root()
        assert root == tmp_paths.data_root / "raw_buffer"

        chan = tmp_paths.get_raw_buffer_dir("WWV_10000")
        assert chan == root / "WWV_10000"

        date_dir = tmp_paths.get_raw_buffer_date_dir("WWV_10000", "20260426")
        assert date_dir == chan / "20260426"

        meta = tmp_paths.get_raw_buffer_metadata_dir("WWV_10000", "20260426")
        assert meta == date_dir / "metadata"


class TestTimeStdPathsPhase2:
    def test_phase2_layout_root_and_channel(self, tmp_paths):
        assert tmp_paths.get_phase2_root() == tmp_paths.data_root / "phase2"
        assert tmp_paths.get_phase2_dir("CHU_3330") == \
            tmp_paths.data_root / "phase2" / "CHU_3330"

    @pytest.mark.parametrize("method,subdir", [
        ("get_clock_offset_dir", "clock_offset"),
        ("get_carrier_analysis_dir", "carrier_analysis"),
        ("get_channel_quality_dir", "channel_quality"),
        ("get_discrimination_dir", "discrimination"),
        ("get_bcd_correlation_dir", "bcd_correlation"),
        ("get_tone_detections_dir", "tone_detections"),
        ("get_ground_truth_dir", "ground_truth"),
        ("get_doppler_dir", "doppler"),
        ("get_phase2_state_dir", "state"),
        ("get_timing_dir", "timing"),
    ])
    def test_phase2_subdirectories(self, tmp_paths, method, subdir):
        chan = "WWV_10000"
        path = getattr(tmp_paths, method)(chan)
        assert path == tmp_paths.data_root / "phase2" / chan / subdir


class TestTimeStdPathsLegacy:
    def test_archive_dir(self, tmp_paths):
        assert tmp_paths.get_archive_dir("WWV_10000") == \
            tmp_paths.data_root / "archives" / "WWV_10000"

    def test_archive_file_format(self, tmp_paths):
        f = tmp_paths.get_archive_file("WWV_10000", "20260426T120000Z", 10_000_000)
        assert f.name == "20260426T120000Z_10000000_iq.npz"
        assert f.parent == tmp_paths.get_archive_dir("WWV_10000")

    def test_analytics_and_drf_dirs(self, tmp_paths):
        assert tmp_paths.get_analytics_dir("WWV_10000") == \
            tmp_paths.data_root / "analytics" / "WWV_10000"
        assert tmp_paths.get_digital_rf_dir("WWV_10000") == \
            tmp_paths.data_root / "drf" / "WWV_10000"


# =============================================================================
# Channel discovery
# =============================================================================


class TestDiscoverChannels:
    def test_empty_root_returns_empty(self, tmp_paths):
        assert tmp_paths.discover_channels() == []
        assert tmp_paths.discover_phase2_channels() == []
        # discover_products_channels is currently a stub
        assert tmp_paths.discover_products_channels() == []

    def test_phase1_only(self, tmp_paths):
        for chan in ["WWV_10000", "CHU_3330", "WWV_5000"]:
            tmp_paths.get_raw_buffer_dir(chan).mkdir(parents=True)

        # Discovered, sorted
        assert tmp_paths.discover_channels() == ["CHU_3330", "WWV_10000", "WWV_5000"]
        # Phase 2 is empty
        assert tmp_paths.discover_phase2_channels() == []

    def test_phase2_only(self, tmp_paths):
        for chan in ["WWV_10000", "CHU_3330"]:
            tmp_paths.get_phase2_dir(chan).mkdir(parents=True)

        assert tmp_paths.discover_channels() == ["CHU_3330", "WWV_10000"]
        assert tmp_paths.discover_phase2_channels() == ["CHU_3330", "WWV_10000"]

    def test_phase1_and_phase2_dedup(self, tmp_paths):
        tmp_paths.get_raw_buffer_dir("WWV_10000").mkdir(parents=True)
        tmp_paths.get_phase2_dir("WWV_10000").mkdir(parents=True)
        tmp_paths.get_phase2_dir("CHU_3330").mkdir(parents=True)

        assert tmp_paths.discover_channels() == ["CHU_3330", "WWV_10000"]

    def test_excluded_directories_ignored(self, tmp_paths):
        # Real channel
        tmp_paths.get_raw_buffer_dir("WWV_10000").mkdir(parents=True)
        # Excluded directory names — must not appear as "channels"
        for name in TimeStdPaths._EXCLUDE_DIRS:
            (tmp_paths.get_raw_buffer_root() / name).mkdir(parents=True, exist_ok=True)

        assert tmp_paths.discover_channels() == ["WWV_10000"]

    def test_files_are_not_channels(self, tmp_paths):
        # Files in raw_buffer/ should be ignored — only directories are channels
        tmp_paths.get_raw_buffer_root().mkdir(parents=True)
        (tmp_paths.get_raw_buffer_root() / "stray_file.txt").write_text("noise")
        tmp_paths.get_raw_buffer_dir("WWV_10000").mkdir()

        assert tmp_paths.discover_channels() == ["WWV_10000"]

    def test_legacy_archives_fallback(self, tmp_paths):
        # No phase1/phase2 data, but archives/ exists — fall back to it
        (tmp_paths.data_root / "archives" / "WWV_10000").mkdir(parents=True)
        (tmp_paths.data_root / "archives" / "CHU_3330").mkdir()

        assert tmp_paths.discover_channels() == ["CHU_3330", "WWV_10000"]

    def test_legacy_fallback_only_when_phase1_phase2_empty(self, tmp_paths):
        # If phase1 has data, archives is NOT consulted
        tmp_paths.get_raw_buffer_dir("WWV_10000").mkdir(parents=True)
        (tmp_paths.data_root / "archives" / "CHU_3330").mkdir(parents=True)

        assert tmp_paths.discover_channels() == ["WWV_10000"]


# =============================================================================
# Config loading
# =============================================================================


def _write_config(tmp_path: Path, prod: str = "/var/lib/timestd") -> Path:
    # The legacy recorder.mode test/production toggle was removed; a config
    # now carries a single production_data_root (see load_paths_from_config).
    cfg = tmp_path / "timestd-config.toml"
    cfg.write_text(f'[recorder]\nproduction_data_root = "{prod}"\n')
    return cfg


class TestLoadPathsFromConfig:
    def test_uses_production_data_root(self, tmp_path):
        cfg = _write_config(tmp_path, prod="/srv/timestd")
        paths = load_paths_from_config(cfg)
        assert paths.data_root == Path("/srv/timestd")

    def test_missing_config_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_paths_from_config(tmp_path / "does-not-exist.toml")

    def test_missing_recorder_section_falls_back_to_default(self, tmp_path):
        cfg = tmp_path / "empty.toml"
        cfg.write_text("# empty config\n")
        paths = load_paths_from_config(cfg)
        # No recorder.production_data_root -> the single built-in default.
        assert paths.data_root == Path("/var/lib/timestd")


class TestGetPaths:
    def test_explicit_data_root_short_circuits(self, tmp_path):
        paths = get_paths(data_root=tmp_path)
        assert paths.data_root == tmp_path

    def test_explicit_data_root_overrides_config(self, tmp_path):
        # Config exists pointing somewhere else, but explicit root wins
        cfg = _write_config(tmp_path, prod="/should-not-be-used")
        paths = get_paths(data_root=tmp_path, config_path=cfg)
        assert paths.data_root == tmp_path

    def test_uses_config_when_no_data_root(self, tmp_path):
        cfg = _write_config(tmp_path, prod=str(tmp_path / "via-cfg"))
        paths = get_paths(config_path=cfg)
        assert paths.data_root == tmp_path / "via-cfg"
