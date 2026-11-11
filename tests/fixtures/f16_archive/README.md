# f16_archive fixture

Three 60 s, 24 kHz chunks of one channel (`TESTCHAN`) in one day directory
(`TESTCHAN/20260906/`), written by the real `BinaryArchiveWriter`:
`1788729000` is an F16 chunk (`"dtype": "complex-f16"`, `"scale": 4096.0`),
`1788729060` is a plain F32 chunk (`"dtype": "complex64"`), and `1788729120`
is an overflow fallback (`"dtype": "complex64"`, `"f16_fallback": "overflow"`,
one component of 20.0).  The samples are low-amplitude noise (RMS about 3.5e-6)
rounded to a 2^-20 grid, so zstd keeps each chunk small and the F16 chunk
round-trips exactly.  The files are committed; `generate.py` beside them
rebuilds them (`uv run python tests/fixtures/f16_archive/generate.py [OUT_DIR]`)
and its `chunks()` function returns the expected samples, so a test can compare
a decode against the source.  To reuse the fixture in another repo, copy this
directory and `generate.py`, which needs hf-timestd importable only to regenerate.
