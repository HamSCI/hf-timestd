"""Every place that decodes raw_buffer bytes takes F16; every size estimate follows the dtype."""
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from hf_timestd.core.binary_archive_writer import BinaryArchiveReader
from hf_timestd.core.iq_codec import DTYPE_F16, DTYPE_F32, bytes_per_sample
from hf_timestd.core.resource_guardian import ResourceGuardian

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "f16_archive"
DAY = FIXTURE / "TESTCHAN" / "20260906"
F16_T, F32_T, OVER_T = 1788729000, 1788729060, 1788729120


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def expected():
    gen = _load(FIXTURE / "generate.py", "f16_fixture_generate")
    return {b: x for b, _, x in gen.chunks()}


def _paths(t):
    return DAY / f"{t}.bin.zst", DAY / f"{t}.json"


def test_fixture_holds_the_three_chunk_kinds():
    sides = {t: json.loads(_paths(t)[1].read_text()) for t in (F16_T, F32_T, OVER_T)}
    assert sides[F16_T]["dtype"] == DTYPE_F16 and sides[F16_T]["scale"] == 4096.0
    assert sides[F32_T]["dtype"] == DTYPE_F32 and "scale" not in sides[F32_T]
    assert sides[OVER_T]["dtype"] == DTYPE_F32 and sides[OVER_T]["f16_fallback"] == "overflow"
    assert all(s["sample_rate"] == 24000 and s["samples_written"] == 24000 * 60 for s in sides.values())


def _readers():
    """Each decoding site as (name, fn(t) -> complex64 samples)."""
    reader = BinaryArchiveReader(FIXTURE, "TESTCHAN")
    diag = _load(REPO / "scripts" / "diagnose_real_iq.py", "diag_real_iq")
    corpus = _load(REPO / "scripts" / "build_estimator_corpus.py", "est_corpus")
    return [
        ("BinaryArchiveReader.read_chunk", lambda t: reader.read_chunk(t)[0]),
        ("BinaryArchiveReader.read_minute", lambda t: reader.read_minute(t)),
        ("scripts/diagnose_real_iq.load_raw_buffer",
         lambda t: diag.load_raw_buffer(str(_paths(t)[0]), str(_paths(t)[1]))[0]),
        ("scripts/build_estimator_corpus.read_iq",
         lambda t: corpus.read_iq(_paths(t)[0], json.loads(_paths(t)[1].read_text()))),
    ]


@pytest.mark.parametrize("t", [F16_T, F32_T, OVER_T])
def test_every_reader_takes_f16(t, expected):
    for name, read in _readers():
        got = read(t)
        assert got.dtype == np.complex64, name
        np.testing.assert_array_equal(got, expected[t], err_msg=name)


def test_f16_amplitude_is_real_not_scaled(expected):
    got = BinaryArchiveReader(FIXTURE, "TESTCHAN").read_chunk(F16_T)[0]
    rms = np.sqrt(np.mean(np.abs(got) ** 2))
    assert 2e-6 < rms < 6e-6


# --- size prediction ---------------------------------------------------------

def _guardian(tmp_path, **kw):
    return ResourceGuardian(data_root=str(tmp_path), n_channels=1, sample_rate=24000, **kw)


def test_size_prediction_uses_configured_dtype(tmp_path):
    f32 = _guardian(tmp_path)
    f16 = _guardian(tmp_path, archive_dtype="f16")
    assert f32.raw_per_ch_per_day == 24000 * 86400 * bytes_per_sample(DTYPE_F32)
    assert f16.raw_per_ch_per_day * 2 == f32.raw_per_ch_per_day
    assert f16.raw_per_ch_per_day == 24000 * 86400 * bytes_per_sample(DTYPE_F16)
    assert f16.total_per_ch_per_day < f32.total_per_ch_per_day
    assert f16.daily_disk_budget < f32.daily_disk_budget


def test_from_config_reads_archive_dtype(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[recorder]\nproduction_data_root = "%s"\narchive_dtype = "f16"\n'
                   '[[channels]]\nname = "a"\n' % tmp_path)
    assert ResourceGuardian.from_config(str(cfg)).raw_per_ch_per_day == 24000 * 86400 * 4


def test_bad_dtype_is_refused(tmp_path):
    with pytest.raises(ValueError):
        _guardian(tmp_path, archive_dtype="int8")


def test_measured_usage_comes_from_bytes_on_disk(tmp_path):
    (tmp_path / "raw_buffer" / "CH" / "20260906").mkdir(parents=True)
    (tmp_path / "raw_buffer" / "CH" / "20260906" / "1.bin.zst").write_bytes(b"x" * 1234)
    for g in (_guardian(tmp_path), _guardian(tmp_path, archive_dtype="f16")):
        assert g._dir_size(tmp_path / "raw_buffer" / "CH" / "20260906") == 1234
