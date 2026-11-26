# v3.96 Recorder Data Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The timing recorder writes each finished raw chunk straight to disk, so it no longer holds finished chunks in RAM.  It stores gap fill as zeros with a mask in the sidecar, so the archive and the ring hold the same samples.  It also reports in its status file every sample the archive writer lost.

**Architecture:** The archive writer keeps the chunk it is still filling in a RAM scratch file under `/dev/shm`, as today.  It writes each finished chunk (`.bin.zst` plus `.json`) into `/var/lib/timestd/raw_buffer` at once.  The hot-to-cold mover shrinks to a one-time drain that runs when the recorder starts.  The writer stops interpolating gaps.  It keeps ka9q-python's zeros and records where they lie in a new sidecar field, `fill_runs`.  Every reader that joins a chunk's segments carries that field through the join.

**Tech Stack:** Python 3.11, numpy, pytest, zstandard, digital_rf (events tests).  Repos: hf-timestd (at `2716b839`), hamsci-physics (at `00301b5`).  sigmond needs no code change; it ships the result as v3.96.

**Spec:** `/home/mjh/hamsci/tasks/hf-timestd-audit/ASSESSMENT.md`, "Michael's decisions, 2026-11-23", items 8 ("Free the RAM") and 9 ("Zeros plus a mask").  Detail: `part-A-recording.md` items A11, A14, A17, A18, A22.

## Size

All of it fits v3.96: eight code tasks, then one measurement task on B4.  None of the code tasks touches a station.  Only Task 9 does, through `smd align` on B4, and it restarts the recorder and the six metrology units once.

Left for later, on purpose:

- **The open chunk's buffer (A11).**  The chunk still filling stays in `/dev/shm` as complex64: 115 MB per channel at chunk end, 0.35-0.69 GB for six.  Decision 8 covers finished chunks only.  See open question 3.
- **Health checks that count stored samples (A7).**  Task 7 publishes the counts; the freshness checks still count received samples.
- **An alarm on lost samples in `smd check`.**  See open question 5.
- **Ring and archive labels (A19, A20).**  "The same samples at one RTP" holds only while the two label formulas agree.  This plan makes the samples agree; it does not touch the labels.
- **The ResourceGuardian's tiered budget (A25.2)** belongs to decision 7, retention.

## What the code says that the audit did not

1. **Decision 8 says "as F16 already does".  The code does not do that.**  Every finished chunk, F16 or complex64, goes first into `/dev/shm/timestd/raw_buffer` (`binary_archive_writer.py:388-391`).  It moves to disk only after `hot_minutes × 60 + 600` s (`tiered_storage.py:368`), at least 21 minutes after the chunk's start.  Task 2 makes the F16 path what the decision assumed.
2. **Turning tiered storage off writes the archive where no reader looks.**  Without it, the writer uses `<data_root>/<channel>` (`binary_archive_writer.py:392-394`, with `output_dir` = data root from `core_recorder_v2.py:1700`).  Every reader looks in `<data_root>/raw_buffer/<channel>`.  The recorder's own fatal message (`core_recorder_v2.py:1240-1247`) tells the operator to set `tiered_storage=false`.  sigmond's `grape_roots` (`commands/check.py:1012-1023`) records the odd path.  Task 2 fixes the path for both settings.
3. **Moving chunks to disk wakes the writer's own disk cleaner (A17).**  Today `_check_disk_space` measures `/dev/shm`, where no file reaches two days, so it deletes nothing.  Once the writer writes to the SSD, it would measure the SSD.  Past 80 % it would delete raw files older than two days by mtime, around archive-before-evict and sigmond's ledger.  B4's disk already runs past 80 %.  Task 1 removes the deleting half before Task 2 lands.
4. **The ring keeps zeros but no mask.**  It stores ka9q-python's zeros (`stream_recorder_v2.py:1749-1758`) and only a running total of gap samples (`ring_buffer.py:112`, `:665`).  So "as the ring does" covers the zeros.  The mask exists nowhere yet; Task 5 creates it in the archive sidecar.
5. The audit's line numbers (taken at `fe9f131e`) still hold at `2716b839` for every site this plan touches.

## Open questions for Michael

**Answered 2026-11-25: "As recommended"** (Michael, to V396-PLAN-REVIEW.md questions 5-10).  Each recommendation below now binds.

1. **GRAPE and the zeros.**  A fill run of up to 1000 samples (about 40 ms) used to reach GRAPE bridged.  Now it reaches the decimator as zeros, as missing minutes already do.  Should GRAPE bridge them again at read time, using the mask?  *Recommend no.*  Count them, and leave the data as received.
2. **What GRAPE counts as a gap.**  Task 8 counts, per minute, every position that holds no received sample: fill zeros and holes the writer never wrote.  Today GRAPE counts only a chunk-wide share of the fill.  *Recommend counting both.*  Both feed zeros to the filter.
3. **The open chunk's RAM buffer (A11).**  Keep it in `/dev/shm` as complex64 for v3.96?  *Recommend yes,* and measure first.  An F16 buffer would halve it.  A buffer on disk would add about 100 GB a day of SSD writes (6 channels × 115 MB × 144 chunks), so *recommend against* that.
4. **The keys that lose their meaning** (`tiered_ram_percent`, `ring_ram_percent`, `tiered_hot_minutes`).  Every station's config carries the first two from the template.  If `hf-timestd validate` warns on them, every station turns yellow.  *Recommend:* stop reading them, drop them from the template, and stay silent in v3.96.  Add them to `RETIRED_KEYS` once `smd` can strip them from deployed configs.
5. **A loss alarm.**  Task 7 publishes the counts.  What should `smd check` call bad?  *Recommend* WARN on any abandoned chunk or any timing drop in 24 hours, as a later change.
6. **Stations with `tiered_storage = false`.**  Do any exist?  Their archive sits today in `<data_root>/<channel>`, unread.  From v3.96 it lands in `raw_buffer`, and the old files stay where they are.  *Recommend* a read-only check before the fleet roll (Task 9, step 1).

## Global Constraints

- Every change ships in sigmond v3.96 through `smd align`.  Never hand-edit a station.
- Timing faults get exposed, never silently corrected.  This plan changes no label and no timing field.
- Precise station positions never go into repos or fixtures.  New fixtures carry no `station` block at all.
- Run tests as: `uv run pytest --basetemp=/var/tmp/mjh-pt/<name> -o addopts="" -q <path>`, from the repo root.  Read pytest's exit code, not the dots.
- Each new test must fail when the code it checks is removed.  Each task names its mutation check; run it, see the failure, then restore the code.
- hamsci-physics carries a copy of `chunk_segments.py`.  hf-timestd's copy stays the source of truth.  The two must stay identical below the docstring.
- sigmond's core stays stdlib-only (this plan changes no sigmond code).
- Commits: work on `main`, stage named files only, never `git add -A`.  Michael pushes, and the push gets a line on the claude-bus for Rob.  End every message with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z
  ```
- B4: announce on the claude-bus before any change, and record it in `/etc/sigmond/CLAUDE-COORDINATION.md` on the VM.  Station commands go through `ops/bin/fleet-ssh b4`, never a typed address.  It drops empty-string arguments.  Read-only, light commands only: no chunk decoding on the VM.

## Review Focus

1. **The writer's disk cleaner on the SSD.**  After Task 2, `_check_disk_space` measures the data disk.  It must refuse a flush when space runs short and must never delete a file.  Pinned in Task 1 (`test_disk_check_never_deletes_raw`).
2. **The first start of v3.96 with finished chunks still in `/dev/shm`,** and a boundary the new writer reopens on disk.  The drain must never replace a file on disk.  Pinned in Task 3 (`test_drain_never_replaces_a_file_on_disk`).
3. **A batch that crosses a chunk end with fill on both sides.**  Each chunk's `fill_runs` must name only its own zeros, at chunk positions.  Pinned in Task 5 (`test_fill_runs_split_across_chunk_end`).
4. **A chunk joined from an old segment (interpolated, no mask) and a new one.**  The joined sidecar must not claim a mask it lacks.  Pinned in Task 4 (`test_join_with_a_legacy_segment_claims_no_mask`).
5. **A GRAPE minute at the short tail of a chunk that also holds fill.**  Padding and fill must add, not take the larger.  Pinned in Task 8 (`test_minute_gap_adds_tail_padding_and_fill`).

---

### Task 1: The writer's disk check refuses; it never deletes

**Files:**
- Modify: `src/hf_timestd/core/binary_archive_writer.py:127` (drop `storage_quota_percent`), `:1274-1320` (`_check_disk_space`), `:1322-1395` (delete `_remove_oldest_files`)
- Test: `tests/unit/test_archive_writer_disk_check.py` (new)

**Interfaces:**
- Produces: `BinaryArchiveWriter._check_disk_space(self, path: Path, required_bytes: int) -> bool`.  True when `shutil.disk_usage(path).free >= required_bytes + 100 MiB`.  Otherwise it logs ERROR and returns False.  It deletes nothing.

- [ ] **Step 1: Write the failing tests**

The helper builds a writer with `channel_name="testchan"`, `sample_rate=100`, `output_dir=tmp_path / "raw_buffer"`, `compression="none"`, and leaves `_check_disk_space` unstubbed.  The fake usage object comes from `collections.namedtuple("U", "total used free")`.

```python
def test_disk_check_never_deletes_raw(tmp_path, monkeypatch):
    w = _writer(tmp_path)
    old = w.archive_dir / "20200101" / "1577836800.bin.zst"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"x" * 10)
    (old.parent / "1577836800.json").write_text("{}")
    t = time.time() - 5 * 86400
    os.utime(old, (t, t))
    monkeypatch.setattr(shutil, "disk_usage", lambda p: U(100, 95, 10**12))  # 95 % full, room left
    assert w._check_disk_space(w.archive_dir, 1000) is True
    assert old.exists() and (old.parent / "1577836800.json").exists()

def test_disk_check_refuses_when_free_space_short(tmp_path, monkeypatch):
    w = _writer(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: U(10**12, 10**12 - 50 * 2**20, 50 * 2**20))
    assert w._check_disk_space(w.archive_dir, 1000) is False
```

- [ ] **Step 2: Run the tests and watch the first fail**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/t1 -o addopts="" -q tests/unit/test_archive_writer_disk_check.py`
Expected: `test_disk_check_never_deletes_raw` FAILS (the old file gone).

- [ ] **Step 3: Implement**

Cut the quota branch and both calls to `_remove_oldest_files`.  Delete `_remove_oldest_files` and the `storage_quota_percent` field (nothing sets it).  Keep the refusal.  Its ERROR line names the free and needed MiB.  Rewrite the docstring to say the check refuses and never deletes, and that retention belongs to sigmond's ledger and the prune timer.

- [ ] **Step 4: Run the tests and the writer suites**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/t1 -o addopts="" -q tests/unit/test_archive_writer_disk_check.py tests/test_binary_archive_memmap.py tests/unit/test_archive_writer_label_faults.py`
Expected: all pass.  Mutation check: put the `_remove_oldest_files(...)` call back in the 80 % branch; `test_disk_check_never_deletes_raw` fails.

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/core/binary_archive_writer.py tests/unit/test_archive_writer_disk_check.py
git commit -m "archive writer: the disk check refuses a flush, never deletes raw (A17)"
```

---

### Task 2: Finished chunks go to disk at once; the open chunk stays in RAM

**Files:**
- Modify: `src/hf_timestd/core/binary_archive_writer.py:128` (replace `use_tiered_storage` with `scratch_root`), `:388-396` (`__init__` paths), `:533-555` (`_cleanup_orphaned_tmp_files`), `:1192-1222` (scratch placement in `_start_new_minute`)
- Modify: `src/hf_timestd/core/stream_recorder_v2.py:279-291` (the writer's config)
- Modify (drop the `use_tiered_storage=False` keyword only): `tests/test_binary_archive_memmap.py:40`, `tests/test_archive_writer_f16.py:17`, `tests/test_archive_reader_f16.py:18`, `tests/unit/test_archive_writer_label_faults.py:33`, `tests/unit/test_archive_batch_labels.py:32`, `tests/unit/test_chunk_segments.py:31`, `tests/unit/test_archive_flush_streaming.py:31`, `tests/fixtures/f16_archive/generate.py:52`
- Test: `tests/test_binary_archive_memmap.py`; `tests/unit/test_archive_writer_label_faults.py` (recorder-level test)

**Interfaces:**
- Produces: `BinaryArchiveConfig.scratch_root: Optional[Path] = None`.  This names the root under which the open chunk's memmap lives (`<scratch_root>/<channel_dir>/<YYYYMMDD>/`); None puts it beside the chunk, as before.
- Produces: `BinaryArchiveWriter.archive_dir == config.output_dir / channel_name_to_dir(channel_name)`, always.  `BinaryArchiveWriter.scratch_dir: Optional[Path]`.
- Produces: the stream recorder passes `output_dir = <data_root>/raw_buffer`, and, when `tiered_storage` holds true, `scratch_root = <hot_buffer_root or /dev/shm/timestd>/raw_buffer`.
- The writer no longer imports `tiered_storage`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_binary_archive_memmap.py`, give `_writer` a keyword `scratch_root: Optional[Path] = None` and pass it to the config.

```python
def test_open_chunk_scratch_lives_under_scratch_root(tmp_path):
    w = _writer(tmp_path, scratch_root=tmp_path / "shm" / "raw_buffer")
    buf, boundary = _drive_one_chunk(w)
    assert buf.scratch_path.parent.parent == tmp_path / "shm" / "raw_buffer" / "testchan"
    assert not list((tmp_path / "raw_buffer").rglob("*.scratch"))

def test_finished_chunk_lands_on_disk_not_in_scratch_root(tmp_path):
    w = _writer(tmp_path, scratch_root=tmp_path / "shm" / "raw_buffer")
    buf, boundary = _drive_one_chunk(w)
    assert w._flush_minute(buf)
    disk = list((tmp_path / "raw_buffer" / "testchan").rglob(f"{boundary}.*"))
    assert sorted(p.suffix for p in disk) == [".bin", ".json"]
    assert not [p for p in (tmp_path / "shm").rglob("*") if p.is_file()]

def test_orphan_scratch_under_scratch_root_removed_at_start(tmp_path):
    stale = tmp_path / "shm" / "raw_buffer" / "testchan" / "20200101" / "1.testchan.1.scratch"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"\0" * 64)
    _writer(tmp_path, scratch_root=tmp_path / "shm" / "raw_buffer")
    assert not stale.exists()
```

In `tests/unit/test_archive_writer_label_faults.py`, next to `_recorder`:

```python
@pytest.mark.parametrize("tiered", [True, False])
def test_stream_recorder_archives_under_raw_buffer(tmp_path, tiered):
    # StreamRecorderConfig as in _recorder, plus tiered_storage=tiered,
    # hot_buffer_root=tmp_path / "shm"
    w = r.archive_writer
    assert w.archive_dir == tmp_path / "raw_buffer" / "testchan"
    assert w.scratch_dir == (tmp_path / "shm" / "raw_buffer" / "testchan" if tiered else None)
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/t2 -o addopts="" -q tests/test_binary_archive_memmap.py tests/unit/test_archive_writer_label_faults.py -k "scratch_root or raw_buffer"`
Expected: FAIL (`scratch_root` unknown to the config).

- [ ] **Step 3: Implement**

- Writer: `archive_dir` from `output_dir` alone.  `scratch_dir = config.scratch_root / channel_name_to_dir(name)` when set.
- Add `_scratch_day_dir(self, boundary: int) -> Path`.  It returns `scratch_dir/<YYYYMMDD>` (made on demand), or `_get_minute_dir(boundary)` when `scratch_dir` stays None.  `_start_new_minute` takes the scratch path from it.  The scratch name stays the same.
- `_cleanup_orphaned_tmp_files` removes `*.tmp` and `*.scratch` under `archive_dir`, and `*.scratch` under `scratch_dir` when set.
- Stream recorder: `output_dir=config.output_dir / 'raw_buffer'` and the `scratch_root` rule above.
- Remove the keyword from the eight test files listed.

- [ ] **Step 4: Run the writer, reader and segment suites**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/t2 -o addopts="" -q tests/test_binary_archive_memmap.py tests/test_archive_writer_f16.py tests/test_archive_reader_f16.py tests/unit/test_archive_writer_label_faults.py tests/unit/test_archive_batch_labels.py tests/unit/test_archive_flush_streaming.py tests/unit/test_chunk_segments.py`
Expected: all pass, except `test_chunk_segments.py::test_tiering_moves_a_segment_with_its_own_sidecar` and `test_archive_writer_f16.py::test_tiered_move_sidecar_before_bin`, which Task 3 rewrites (they still pass here because the manager still exists).  Mutation check: set the recorder's `output_dir` back to `config.output_dir`; the parametrized recorder test fails.

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/core/binary_archive_writer.py src/hf_timestd/core/stream_recorder_v2.py \
  tests/test_binary_archive_memmap.py tests/test_archive_writer_f16.py tests/test_archive_reader_f16.py \
  tests/unit/test_archive_writer_label_faults.py tests/unit/test_archive_batch_labels.py \
  tests/unit/test_chunk_segments.py tests/unit/test_archive_flush_streaming.py tests/fixtures/f16_archive/generate.py
git commit -m "archive writer: finished chunks go to raw_buffer on disk at once; the open chunk fills in RAM"
```

---

### Task 3: Tiering shrinks to a one-time drain at recorder start

**Files:**
- Modify: `src/hf_timestd/core/tiered_storage.py` (replace the module body)
- Modify: `src/hf_timestd/core/__init__.py:101-124, 178-201` (lazy exports)
- Modify: `src/hf_timestd/core/core_recorder_v2.py:1197-1250` (replace the tiered block with one call at the top of `run()`)
- Modify: `config/timestd-config.toml.template:83-95`
- Test: `tests/unit/test_tiered_drain.py` (new); remove `test_tiering_moves_a_segment_with_its_own_sidecar` from `tests/unit/test_chunk_segments.py:212-229` (and its import at :20), and `test_tiered_move_sidecar_before_bin` from `tests/test_archive_writer_f16.py:144-161`.  The new file carries both cases.

**Interfaces:**
- Produces, in `tiered_storage.py`:
  - `@dataclass class DrainResult: moved: int; refused: int`
  - `drain_hot_buffer(hot_root: Path, cold_root: Path) -> DrainResult`.  `hot_root` and `cold_root` each name a `raw_buffer` directory.  The function moves every chunk file found in `hot_root/<ch>/<day>/` (any name `parse_chunk_name` accepts, segments included) to the same path under `cold_root`, whatever its age.  It moves the sidecar before the bin.  It moves a sidecar that has no bin as well.  It leaves `*.scratch` and `*.tmp` in place.  When the destination already holds that name, it leaves the hot file in place, logs ERROR with both paths, and counts it as refused.
- Produces: `CoreRecorderV2._drain_hot_buffer(self) -> None`.  It calls `tiered_storage.drain_hot_buffer` through the module (so a test can patch it), and it runs as the first act of `run()` when `[recorder] tiered_storage` holds true.  It logs one INFO line with the counts, or WARNING when `refused > 0`.
- Removes: `TieredStorageConfig`, `TieredStorageManager`, `calculate_hot_minutes`, `get_available_ram_bytes`, `get_shm_size_bytes`, `get_tiered_storage_manager`, `init_tiered_storage`, and the recorder's reads of `tiered_hot_minutes`, `tiered_ram_percent`, `ram_percent`.

- [ ] **Step 1: Write the failing tests**

```python
def test_drain_moves_every_chunk_regardless_of_age(tmp_path):
    hot, cold = tmp_path / "shm" / "raw_buffer", tmp_path / "disk" / "raw_buffer"
    day = hot / "WWV_10000" / "20261125"
    now = int(time.time()) // 600 * 600          # a chunk that ended a moment ago
    for stem in (str(now), f"{now}.seg2"):
        _chunk(day, stem)                        # writes <stem>.bin.zst and <stem>.json
    r = drain_hot_buffer(hot, cold)
    assert (r.moved, r.refused) == (2, 0)
    for stem in (str(now), f"{now}.seg2"):
        assert (cold / "WWV_10000" / "20261125" / f"{stem}.bin.zst").exists()
        assert (cold / "WWV_10000" / "20261125" / f"{stem}.json").exists()

def test_drain_moves_the_sidecar_before_the_bin(tmp_path, monkeypatch):
    # record shutil.move's sources, as the old test_tiered_move_sidecar_before_bin did
    assert order == ["100.json", "100.bin.zst"]

def test_drain_never_replaces_a_file_on_disk(tmp_path):
    # the same stem in hot and in cold, with different bytes
    r = drain_hot_buffer(hot, cold)
    assert (r.moved, r.refused) == (0, 1)
    assert (cold_day / "100.bin.zst").read_bytes() == b"disk"
    assert (hot_day / "100.bin.zst").read_bytes() == b"ram"

def test_drain_leaves_scratch_and_tmp(tmp_path):
    # 100.testchan.1.scratch and 100.bin.zst.tmp in hot
    assert drain_hot_buffer(hot, cold).moved == 0
    assert (hot_day / "100.testchan.1.scratch").exists()

def test_run_drains_before_it_opens_channels(monkeypatch, tmp_path):
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    cr.recorder_config = {"tiered_storage": True, "hot_buffer_root": str(tmp_path / "shm")}
    cr.output_dir = tmp_path / "disk"
    calls = []
    monkeypatch.setattr(tiered_storage, "drain_hot_buffer",
                        lambda h, c: calls.append(("drain", h, c)) or DrainResult(0, 0))
    monkeypatch.setattr(cr, "_initialize_channels", lambda: calls.append(("init",)) or False)
    cr.run()
    assert calls[0] == ("drain", tmp_path / "shm" / "raw_buffer", tmp_path / "disk" / "raw_buffer")
    assert calls[1] == ("init",)
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/t3 -o addopts="" -q tests/unit/test_tiered_drain.py`
Expected: FAIL on import (`drain_hot_buffer` undefined).

- [ ] **Step 3: Implement**

- Rewrite `tiered_storage.py` to the two names above.  Its docstring says why: since v3.96 the writer puts finished chunks on disk itself.  The drain clears what an older recorder left in RAM.
- In `run()`, call `_drain_hot_buffer()` before `_initialize_channels()`.  Delete the whole tiered block at `:1197-1250`, its fatal message included.
- Update the lazy-export table and `__all__` in `core/__init__.py`.
- Template: keep `tiered_storage = true` and `hot_buffer_root`.  Delete `tiered_ram_percent` and `ring_ram_percent`.  Rewrite the comment above them: with `tiered_storage` on, the chunk still filling lives in `hot_buffer_root` (RAM, about 115 MB per channel at chunk end); finished chunks go to `raw_buffer` on disk at once.  Leave `config/help.toml` as it stands (all four keys stay hidden there).

- [ ] **Step 4: Run the tests, the lazy-import suite and the recorder suites**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/t3 -o addopts="" -q tests/unit/test_tiered_drain.py tests/unit/test_core_lazy_init.py tests/unit/test_lazy_import_paths.py tests/test_ring_sizing.py tests/unit/test_chunk_segments.py tests/test_archive_writer_f16.py tests/test_core_recorder_time_map_context.py`
Expected: all pass.  Mutation check: delete the destination-exists guard; `test_drain_never_replaces_a_file_on_disk` fails.  Move the drain call after `_initialize_channels()`; `test_run_drains_before_it_opens_channels` fails.

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/core/tiered_storage.py src/hf_timestd/core/__init__.py \
  src/hf_timestd/core/core_recorder_v2.py config/timestd-config.toml.template \
  tests/unit/test_tiered_drain.py tests/unit/test_chunk_segments.py tests/test_archive_writer_f16.py
git commit -m "tiered storage: no RAM hold of finished chunks; the recorder drains /dev/shm once at start"
```

---

### Task 4: The mask helpers, and joins that carry the mask

**Files:**
- Modify: `src/hf_timestd/core/chunk_segments.py:36-48` (add helpers beside `covered_mask`), `:85-156` (`SegmentJoiner`)
- Test: `tests/unit/test_chunk_segments.py`

**Interfaces:**
- Produces:
  - `runs_from_mask(mask: np.ndarray, offset: int = 0) -> List[List[int]]`: the sorted, merged `[start, end)` runs of True positions, shifted by `offset`.
  - `fill_mask(n: int, sidecar: Optional[Dict]) -> Optional[np.ndarray]`: True at the sidecar's `fill_runs`, clipped to `n`.  None when the sidecar has no `fill_runs`.  That marks a chunk written before v3.96, whose fill the writer interpolated and never marked.
  - `SegmentJoiner.result()` for two or more segments now also sets `covered_runs` (the joined chunk's written positions; until now it kept the first segment's runs).  When every merged segment carries `fill_runs`, it sets `fill_runs` (fill positions taken into the joined chunk), `gap_fill = "zeros"` and `gap_samples` (their count).  Otherwise it removes `fill_runs` and `gap_fill` from the joined sidecar.  A single segment comes back unchanged, as now.

- [ ] **Step 1: Write the failing tests**

```python
def test_runs_from_mask_merges_and_offsets():
    m = np.array([False, True, True, False, True])
    assert runs_from_mask(m, offset=10) == [[11, 13], [14, 15]]
    assert runs_from_mask(np.zeros(4, bool)) == []

def test_fill_mask_none_for_a_sidecar_without_fill_runs():
    assert fill_mask(10, {"covered_runs": [[0, 10]]}) is None
    np.testing.assert_array_equal(fill_mask(5, {"fill_runs": [[1, 3]]}),
                                  [False, True, True, False, False])

def test_join_carries_each_segments_fill_into_the_joined_chunk():
    a = np.ones(10, np.complex64); a[2:4] = 0
    b = np.full(10, 2, np.complex64); b[7:9] = 0
    x, meta = join_segments([
        (1, a, {"covered_runs": [[0, 6]], "fill_runs": [[2, 4]], "gap_fill": "zeros"}),
        (2, b, {"covered_runs": [[4, 10]], "fill_runs": [[7, 9]], "gap_fill": "zeros"})])
    assert meta["fill_runs"] == [[2, 4], [7, 9]]
    assert meta["covered_runs"] == [[0, 10]]
    assert (meta["gap_fill"], meta["gap_samples"]) == ("zeros", 4)

def test_fill_at_a_refused_position_does_not_join():
    # segment 2 marks fill at [[5, 6]], which segment 1 already wrote
    assert meta["fill_runs"] == [[2, 4]]

def test_join_with_a_legacy_segment_claims_no_mask():
    # segment 1 without fill_runs or gap_fill, segment 2 with them
    assert "fill_runs" not in meta and "gap_fill" not in meta
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/t4 -o addopts="" -q tests/unit/test_chunk_segments.py`
Expected: FAIL on import of `runs_from_mask`.

- [ ] **Step 3: Implement**

`SegmentJoiner` keeps one more boolean array, `_fill`, beside `_filled`, plus a flag for "every segment carried a mask".  `_merge` sets `_fill[take & seg_fill]` when the segment's `fill_mask` exists, and clears the flag when it does not.  Grow `_fill` alongside `_filled`.

- [ ] **Step 4: Run the tests and the reader suites**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/t4 -o addopts="" -q tests/unit/test_chunk_segments.py tests/test_archive_reader_f16.py tests/unit/test_window_drf.py`
Expected: all pass.  Mutation check: drop the `_fill` update in `_merge`; the join test fails.

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/core/chunk_segments.py tests/unit/test_chunk_segments.py
git commit -m "chunk segments: fill_runs and covered_runs survive a join; a legacy segment claims no mask"
```

---

### Task 5: The writer stores zeros and marks them

**Files:**
- Modify: `src/hf_timestd/core/binary_archive_writer.py`: `MinuteBuffer` (`:171-280`), sidecar (`:1720-1810`, including the comment at `:1732-1737`), delete `_interpolate_gaps` (`:1887-1963`), `_write_samples_inner` (`:2312-2478`), counters (`:429-434`), `get_stats` (`:2521-2541`)
- Modify: `CLAUDE.md` ("Raw IQ storage" bullet)
- Test: `tests/unit/test_archive_writer_label_faults.py` (the I1 section)

**Interfaces:**
- Consumes: `runs_from_mask` (Task 4).
- Produces:
  - `MinuteBuffer.fill_runs: List[List[int]]` and `MinuteBuffer.note_fill(self, start: int, mask: np.ndarray) -> int`.  It adds the True runs of `mask`, placed at `start`, merges a run that continues the last one, and returns the count added.
  - Sidecar keys `gap_fill: "zeros"` and `fill_runs: [[start, end), ...]`, in chunk sample positions.  `gap_samples` and `gap_count` keep their meaning.
  - `BinaryArchiveWriter.fill_samples: int`, the fill positions stored, and `get_stats()['fill_samples']`.

- [ ] **Step 1: Write the failing tests**

```python
def test_gap_fill_stays_zero_in_the_archive(tmp_path):
    w = _writer(tmp_path, dur=30)
    base = 300 * SR
    x = _ramp(1000); x[400:600] = 0
    w.write_samples(x, base, gap_samples=200)        # a short run the writer used to bridge
    ((_, d, side),) = _drain(w)
    assert np.all(d[400:600] == 0)
    assert side["gap_fill"] == "zeros"
    assert side["fill_runs"] == [[400, 600]]
    assert w.get_stats()["fill_samples"] == 200

def test_zero_without_a_reported_gap_is_not_fill(tmp_path):
    x = _ramp(1000); x[5] = 0
    w.write_samples(x, base, gap_samples=0)
    assert side["fill_runs"] == []

def test_fill_at_positions_already_written_is_not_marked(tmp_path):
    # write 1000 samples, then a batch labelled over [500, 700) whose zeros sit at [550, 650)
    assert side["fill_runs"] == []
```

Extend the two I1 tests: `test_split_batch_leaves_its_tail_fill_to_the_next_chunk` asserts `side1["fill_runs"] == [[100, 1300]]`.  Rename `test_split_batch_with_fill_on_both_sides_divides_it` to `test_fill_runs_split_across_chunk_end` and assert `side0["fill_runs"] == [[spc - 1900, spc - 700]]` and `side1["fill_runs"] == [[500, 1700]]`.

- [ ] **Step 2: Run the tests and watch them fail**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/t5 -o addopts="" -q tests/unit/test_archive_writer_label_faults.py -k "fill or gap"`
Expected: FAIL (`d[400:600]` interpolated; no `fill_runs` key).

- [ ] **Step 3: Implement**

- Keep `fill_mask = samples == 0` when `gap_samples > 0`, and drop the `_interpolate_gaps` call and the method.
- In the free-span loop, after each `buffer.samples[a:b] = ...`, when `fill_mask` exists: `self.fill_samples += buffer.note_fill(a, fill_mask[a - sample_position:b - sample_position])`.
- Write `gap_fill` and `fill_runs` into the sidecar.  Rewrite the comment: `samples_covered` includes fill, `fill_runs` names where it lies, and received samples equal `samples_covered` minus the fill count.
- CLAUDE.md: the raw-storage bullet gains one sentence.  Since v3.96 the writer stores ka9q-python's gap fill as zeros and lists it in the sidecar's `fill_runs`.  Chunks written earlier hold interpolated fill, with no mask.

- [ ] **Step 4: Run the writer suites**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/t5 -o addopts="" -q tests/unit/test_archive_writer_label_faults.py tests/unit/test_archive_batch_labels.py tests/test_binary_archive_memmap.py tests/test_archive_writer_f16.py tests/unit/test_archive_flush_streaming.py`
Expected: all pass.  Mutation check: put back `samples = self._interpolate_gaps(samples)`; `test_gap_fill_stays_zero_in_the_archive` fails.

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/core/binary_archive_writer.py tests/unit/test_archive_writer_label_faults.py CLAUDE.md
git commit -m "archive writer: gap fill stays zero, as in the ring, and the sidecar names it (fill_runs)"
```

---

### Task 6: hf-timestd's readers carry the mask

The archive's readers in hf-timestd, and what each needs:

| Reader | Where | Change |
|---|---|---|
| `BinaryArchiveReader.read_chunk` | `binary_archive_writer.py:2599` | none beyond Task 4; pin it |
| Event windows to Digital RF | `events/window_drf.py:77-140` | take the joined sidecar from `read_chunk`, not `read_metadata` |
| Archive-drive copy, F32 to F16 | `archiver.py:170` `copy_one` | none (it copies the sidecar dict); pin it |
| Export back to F32 | `archiver.py:620` `export_f32` | none (it copies each segment's sidecar); pin it |
| The drain | `tiered_storage.py` (Task 3) | moves the sidecar with its bin; Task 3 pins it |

**Files:**
- Modify: `src/hf_timestd/events/window_drf.py:77-140`
- Test: `tests/unit/test_chunk_segments.py`, `tests/unit/test_window_drf.py`, `tests/test_archiver.py`, `tests/test_archive_export.py`

**Interfaces:**
- Consumes: Task 4's joined sidecar.
- Produces: in `_write_channel`, each file's metadata record carries the joined sidecar (`read_chunk`'s), so a segmented chunk's DRF record names every segment's fill.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_window_drf.py
def test_segmented_chunk_metadata_carries_joined_fill_runs(tmp_path):
    # make_file for <s> with fill_runs [[2, 4]] and covered_runs [[0, 24]];
    # a <s>.seg2 file with fill_runs [[30, 32]] and covered_runs [[24, 48]]
    write_window_drf(tmp_path, [CH], ORDER, tmp_path / "out")
    rec = md.read(s * RATE, s * RATE + 1)[s * RATE]
    assert json.loads(str(rec["sidecar_json"]))["fill_runs"] == [[2, 4], [30, 32]]

# tests/unit/test_chunk_segments.py
def test_reader_returns_the_joined_fill_runs(tmp_path):
    # two segments on disk under raw_buffer/testchan/<day>/, as in the segment tests
    x, meta = BinaryArchiveReader(tmp_path / "raw_buffer", "testchan").read_chunk(boundary)
    assert meta["fill_runs"] == [[2, 4], [7, 9]]

# tests/test_archiver.py
def test_copy_keeps_fill_runs_and_exact_zeros(tmp_path):
    x = _noise(); x[100:200] = 0
    b, j = _write_chunk(src, start, x, extra={"fill_runs": [[100, 200]], "gap_fill": "zeros"})
    assert A.copy_one(b, j, drive, "f16") == "converted"
    meta = json.loads((drive / j.name).read_text())
    assert meta["fill_runs"] == [[100, 200]]
    assert np.all(_decode(drive / b.name, meta)[100:200] == 0)

# tests/test_archive_export.py
def test_export_keeps_fill_runs(tmp_path):
    # as _archive, one chunk with fill at [[100, 200]]; export_f32; read the exported json
    assert meta["fill_runs"] == [[100, 200]] and meta["dtype"] == "complex64"
```

- [ ] **Step 2: Run the tests and watch the DRF test fail**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/t6 -o addopts="" -q tests/unit/test_window_drf.py tests/unit/test_chunk_segments.py tests/test_archiver.py tests/test_archive_export.py`
Expected: `test_segmented_chunk_metadata_carries_joined_fill_runs` FAILS (only `[[2, 4]]`).  The three pins pass already.

- [ ] **Step 3: Implement**

In `_write_channel`, replace `reader.read_minute(s)` plus `reader.read_metadata(s)` with `samples, side = reader.read_chunk(s)`.  Keep `side` in `sides[s]` for the metadata pass, and drop the second read.  The sidecars hold run lists only, so holding them costs little; `test_peak_memory_stays_near_one_file` must still pass.

- [ ] **Step 4: Run the tests**

Same command as Step 2.  Expected: all pass, `test_drf_roundtrip` and `test_peak_memory_stays_near_one_file` included.  Mutation checks: restore `read_metadata` in `_write_channel`, and the DRF test fails.  Make `copy_one` drop `fill_runs` from `out_meta`, and `test_copy_keeps_fill_runs_and_exact_zeros` fails.

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/events/window_drf.py tests/unit/test_window_drf.py tests/unit/test_chunk_segments.py \
  tests/test_archiver.py tests/test_archive_export.py
git commit -m "archive readers: event windows carry the joined fill mask; copy and export keep it"
```

---

### Task 7: The status file reports what the archive writer lost

**Files:**
- Modify: `src/hf_timestd/core/binary_archive_writer.py` (add `loss_report` beside `get_stats`, `:2521`)
- Modify: `src/hf_timestd/core/stream_recorder_v2.py:1815-1845` (`get_stats`)
- Modify: `src/hf_timestd/core/core_recorder_v2.py:7281-7315` (`_write_status`, the `overall` block)
- Test: `tests/unit/test_archive_writer_label_faults.py`, `tests/unit/test_core_recorder_archive_totals.py` (new)

**Interfaces:**
- Produces: `BinaryArchiveWriter.loss_report(self) -> Dict[str, Any]`, shaped as:
  ```
  {"samples_placed": samples_written, "fill_samples": ..., "samples_lost": <sum of lost_by_cause>,
   "lost_by_cause": {"timing_drops", "stale_drops", "unplaced_samples", "overlap_samples",
                     "dropped_before_start", "abandoned_samples"},
   "abandoned_chunks": ..., "write_errors": ..., "held_samples": ..., "flush_queue_depth": ...}
  ```
  `samples_placed` counts samples the writer put into a chunk buffer.  Samples of an abandoned chunk count in both `samples_placed` and `abandoned_samples`, so the docstring must say that.
- Produces: `StreamRecorderV2.get_stats()['archive']`, the writer's `loss_report()`, or None when the channel archives nothing.
- Produces: `archive_totals(channels: Dict[str, dict]) -> Dict[str, int]`, a module-level function in `core_recorder_v2.py`.  It returns `{"total_archive_samples_placed", "total_archive_samples_lost"}`, summed over channels whose `archive` holds a dict.  `_write_status` merges it into `overall`.  Every existing key stays as it is.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_archive_writer_label_faults.py
def test_status_reports_samples_dropped_before_the_chunk_start(tmp_path):
    r, w = _recorder(tmp_path)
    # stream, then one batch labelled before the open chunk's start (as in
    # test_label_behind_the_chunk_start_is_counted_not_lost_silently)
    a = r.get_status()["archive"]
    assert a["lost_by_cause"]["dropped_before_start"] == n_behind
    assert a["samples_lost"] == n_behind
    assert a["samples_placed"] == w.samples_written

# tests/unit/test_core_recorder_archive_totals.py
def test_archive_totals_sum_channels_and_skip_unarchived():
    ch = {"a": {"archive": {"samples_placed": 10, "samples_lost": 2}},
          "b": {"archive": {"samples_placed": 5, "samples_lost": 0}},
          "c": {"archive": None}}
    assert archive_totals(ch) == {"total_archive_samples_placed": 15,
                                  "total_archive_samples_lost": 2}
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/t7 -o addopts="" -q tests/unit/test_archive_writer_label_faults.py tests/unit/test_core_recorder_archive_totals.py -k "status or totals"`
Expected: FAIL (`KeyError: 'archive'`; `archive_totals` undefined).

- [ ] **Step 3: Implement** as the Interfaces block says.

- [ ] **Step 4: Run the tests and the recorder suites**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/t7 -o addopts="" -q tests/unit/test_archive_writer_label_faults.py tests/unit/test_core_recorder_archive_totals.py tests/test_core_recorder_t5_status_block.py`
Expected: all pass.  Mutation check: return `{}` from `loss_report`; the status test fails.

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/core/binary_archive_writer.py src/hf_timestd/core/stream_recorder_v2.py \
  src/hf_timestd/core/core_recorder_v2.py tests/unit/test_archive_writer_label_faults.py \
  tests/unit/test_core_recorder_archive_totals.py
git commit -m "recorder status: each channel reports the samples its archive writer placed and lost (A18)"
```

---

### Task 8: GRAPE reads the mask (hamsci-physics)

Readers in hamsci-physics:

| Reader | Where | Change |
|---|---|---|
| Segment join (copy of hf-timestd's) | `grape/chunk_segments.py` | copy Task 4's code |
| `RawBinaryReader` | `grape/raw_reader.py:345-369` | none: it joins through the copy.  It still searches `/dev/shm` first, which now holds only scratch files, and that stays harmless for chunks an older recorder left there |
| Decimation, per-minute gap | `grape/decimation_pipeline.py:49-73`, `:211-220` | count each minute's gap exactly from the mask |
| Spectrogram | `grape/spectrogram.py` | none: it reads the decimated day, not raw chunks |

**Files:**
- Modify: `src/hamsci_physics/grape/chunk_segments.py` (bring the body level with hf-timestd's, keeping the copy's docstring header)
- Modify: `src/hamsci_physics/grape/decimation_pipeline.py`
- Test: `tests/test_grape_raw_reader_segments.py`, `tests/test_grape_per_minute_gap.py`

**Interfaces:**
- Consumes: Task 4's helpers and joiner, copied.
- Produces: `_minute_gap_exact(meta: Optional[dict], minute_ts: int) -> Optional[int]`.  It counts positions in `[off, min(off + 60 × rate, samples_written))` that lie in `fill_runs` or outside `covered_runs`, where `off = (minute_ts - meta['minute_boundary']) × rate`.  It returns None when the sidecar lacks `fill_runs` or `covered_runs`.
- In the decimation loop: when `_minute_gap_exact` returns a number, `gap_info = padding + exact`; otherwise `gap_info = max(padding, _per_minute_gap(meta))`, as now.  `_per_minute_gap` stays for old chunks.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_grape_raw_reader_segments.py, inside test_copy_matches_hf_timestd, add:
    a2 = np.ones(10, np.complex64); a2[2:4] = 0
    b2 = np.full(10, 2, np.complex64); b2[7:9] = 0
    _, meta2 = join_segments([
        (1, a2, {"covered_runs": [[0, 6]], "fill_runs": [[2, 4]], "gap_fill": "zeros"}),
        (2, b2, {"covered_runs": [[4, 10]], "fill_runs": [[7, 9]], "gap_fill": "zeros"})])
    assert meta2["fill_runs"] == [[2, 4], [7, 9]] and meta2["gap_samples"] == 4

def test_reader_returns_the_joined_fill_runs(tmp_path):
    # _write two segments with fill_runs, as test_a_lone_segment_is_read writes one
    x, meta = r.read_minute(CHUNK)
    assert meta["fill_runs"] == [...] and np.all(x[2:4] == 0)

# tests/test_grape_per_minute_gap.py
def test_exact_count_from_fill_runs():
    meta = {"minute_boundary": 0, "sample_rate": 100, "samples_written": 60000,
            "covered_runs": [[0, 60000]], "fill_runs": [[6100, 6300]]}
    assert _minute_gap_exact(meta, 60) == 200
    assert _minute_gap_exact(meta, 0) == 0

def test_exact_count_includes_holes():
    meta = {..., "covered_runs": [[0, 6000], [6500, 60000]], "fill_runs": []}
    assert _minute_gap_exact(meta, 60) == 500

def test_exact_count_none_for_a_legacy_sidecar():
    assert _minute_gap_exact({"gap_samples": 12000, "file_duration_sec": 600}, 0) is None

def test_minute_gap_adds_tail_padding_and_fill():
    # samples_written = 6000 + 3000 (the second minute runs short by 3000);
    # fill at [[6100, 6200]]: the decimation loop must report 3000 + 100, not max()
```

The last test drives the loop through `DecimationPipeline` with a stub reader, the way the other GRAPE pipeline tests do.  If no such stub exists, factor the gap arithmetic into `_minute_gap_info(meta, minute_ts, padding) -> int` and test that function.

- [ ] **Step 2: Run the tests and watch them fail**

Run: `cd /home/mjh/hamsci/repos/hamsci-physics && uv run pytest --basetemp=/var/tmp/mjh-pt/t8 -o addopts="" -q tests/test_grape_raw_reader_segments.py tests/test_grape_per_minute_gap.py`
Expected: FAIL (`fill_runs` missing from the join; `_minute_gap_exact` undefined).

- [ ] **Step 3: Implement**

Copy hf-timestd's `chunk_segments.py` body below the docstring.  Check the copy with `diff <(sed '1,/^"""$/d' ../hf-timestd/src/hf_timestd/core/chunk_segments.py) <(sed '1,/^"""$/d' src/hamsci_physics/grape/chunk_segments.py)`, which must print nothing.  Then add `_minute_gap_exact` and the loop change.

- [ ] **Step 4: Run the GRAPE suite, then both full suites**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/t8 -o addopts="" -q tests/` (hamsci-physics), then `cd ../hf-timestd && uv run pytest --basetemp=/var/tmp/mjh-pt/full -o addopts="-ra" -q tests/; echo exit=$?`
Expected: both exit 0.  Mutation check: make `_minute_gap_exact` ignore `fill_runs`; `test_exact_count_from_fill_runs` fails.

- [ ] **Step 5: Commit (hamsci-physics)**

```bash
git add src/hamsci_physics/grape/chunk_segments.py src/hamsci_physics/grape/decimation_pipeline.py \
  tests/test_grape_raw_reader_segments.py tests/test_grape_per_minute_gap.py
git commit -m "grape: joins carry the archive's fill mask; each minute's gap counts its own fill and holes"
```

---

### Task 9: Measure on B4, before and after (station work)

The "before" half runs first in time: start it while Tasks 1-8 get built.  Each half lasts 24 hours.  Every command reads; nothing here changes B4 except the `smd align` in step 4.  Run everything from `/home/mjh/hamsci` on the devbox.

The audit's collector script did not survive in the task folder.  `b4/snapshot-20261123T1215Z.txt` shows its shape (unit, then eight `systemctl show` values), and the command in step 2 reproduces that shape with `key=value` output, so the two snapshots diff cleanly.  If the original collector turns up, use it instead.

- [ ] **Step 1: Read the bus and the ledger; check for stations without tiered storage**

Run the CLAUDE.md bus and ledger commands.  Add the ledger item `v396-recorder-data-path` (road: code + restart; check: step 6's numbers; targets: B4, then fleet).  Then answer open question 6 for B4: `ops/bin/fleet-ssh b4 'grep -n "^tiered_storage" /etc/hf-timestd/timestd-config.toml'`.  Expected: `tiered_storage = true`.

- [ ] **Step 2: Write the read-only probe**

Save it as `/home/mjh/hamsci/tasks/hf-timestd-audit/b4/v396_probe.sh`:

```sh
date -u +%FT%TZ; cat /proc/uptime; free -m; df -m /dev/shm /var/lib/timestd
systemctl list-units --all --type=service --no-legend --plain 'timestd-*' 'grape-*' 'hamsci-physics-*' \
  | awk '{print $1}' | while read -r u; do
    echo "$u $(systemctl show -p NRestarts,MemoryCurrent,MemoryPeak,CPUUsageNSec,IOReadBytes,IOWriteBytes,ActiveState,ActiveEnterTimestampMonotonic "$u" | tr '\n' ' ')"
  done
cg=/sys/fs/cgroup/system.slice/timestd-core-recorder.service
grep -E '^(anon|file|shmem) ' $cg/memory.stat; grep oom_kill $cg/memory.events
du -sm /dev/shm/timestd/raw_buffer
find /dev/shm/timestd/raw_buffer -name '*.bin*' | wc -l
journalctl -k --since -24h --no-pager | grep -ci 'out of memory'
```

And `/home/mjh/hamsci/tasks/hf-timestd-audit/b4/v396_day.py` (stdlib only).  For a UTC day given as its argument, it reads every `/var/lib/timestd/raw_buffer/*/<day>/*.json` and prints one line per channel: chunks, segments, `samples_expected`, `samples_covered`, `gap_samples`, `hole_samples`, `overlap_samples`, `dropped_before_start`, chunks with `gap_fill == "zeros"`, and the summed length of `fill_runs`.  It also flags any chunk whose `fill_runs` total differs from its `gap_samples`, or whose fill lies outside `covered_runs`.  With `--status` it prints each channel's `archive` block from `/var/lib/timestd/status/core-recorder-status.json`.

- [ ] **Step 3: "Before", on B4's current version**

```
ops/bin/fleet-ssh b4 "echo $(base64 -w0 tasks/hf-timestd-audit/b4/v396_probe.sh) | base64 -d | sh" > tasks/hf-timestd-audit/b4/v396-before-T0.txt
```
Run it again 24 hours later into `v396-before-T24.txt`.  Run the day summary for the full UTC day between them:
```
ops/bin/fleet-ssh b4 "echo $(base64 -w0 tasks/hf-timestd-audit/b4/v396_day.py) | base64 -d | python3 - <YYYYMMDD>" > tasks/hf-timestd-audit/b4/v396-before-day.txt
```
Expected before: `shmem` near 1.0-1.6 GB; 10-25 chunk files in `/dev/shm`; no `gap_fill` key in any sidecar.

- [ ] **Step 4: Put the v3.96 candidate on B4**

Wait until the v3.96 candidate release list names the hf-timestd and hamsci-physics commits from Tasks 1-8.  Post on the claude-bus first: what changes (recorder restart, six metrology restarts, the drain of `/dev/shm`), when, and the rollback (align back to v3.95).  Record it in `/etc/sigmond/CLAUDE-COORDINATION.md` on the VM.  Then align B4 by the release path, as the v3.96 release notes direct.  Do not hand-edit.  Record the time.

Right after: `ops/bin/fleet-ssh b4 'journalctl -u timestd-core-recorder --since -10min --no-pager | grep -E "drain|refused"'`.  Expected: one drain line, `refused` 0.

- [ ] **Step 5: "After", 24 hours**

Start after the first full chunk on the new version: same probe into `v396-after-T0.txt`, then `v396-after-T24.txt`, and the day summary for the first full UTC day into `v396-after-day.txt`, plus `python3 - --status` into `v396-after-status.txt`.

- [ ] **Step 6: Compare and record**

Pass when every line holds:

| Measure | Before | After, to pass |
|---|---|---|
| recorder `shmem` (memory.stat) | as measured | lower by 0.3 GB or more |
| chunk files in `/dev/shm` | 10-25 | 0 |
| recorder `MemoryPeak` over 24 h | as measured | not higher |
| OOM kills, recorder restarts (`NRestarts`) | as measured | not higher |
| chunks per channel per day | as measured | not fewer (144 expected) |
| sidecars with `gap_fill = "zeros"` | 0 | every chunk |
| fill mask flags from `v396_day.py` | n/a | none |
| status `archive` block per channel | absent | present on every archive channel; each nonzero cause in `lost_by_cause` has matching WARNING lines in the recorder journal |
| recorder CPU (`CPUUsageNSec` over 24 h) | as measured | not higher (interpolation gone) |

`MemoryCurrent` now also counts page cache from writing the chunks to disk.  The kernel reclaims that cache, so judge memory by `anon` plus `shmem`, not by `MemoryCurrent`.

Write the table, with numbers, into the ledger item's notes, and post it on the bus.  Move the item to `on-stations` for B4.  It reaches `verified` when the same check passes on every target after the fleet roll.

---

## Estimate

- Nine tasks.  Eight change code only, five in hf-timestd and one in hamsci-physics, with about 25 new tests.  One task touches a running station: Task 9, on B4, once, by `smd align`.
- B4 time: 24 hours before, 24 hours after, plus the align.

## Risks to stations during rollout

- **The align restarts the recorder and the six metrology units.**  Each channel loses part of one chunk; the next start writes a segment.  Every hf-timestd deploy does the same.
- **The first start drains `/dev/shm`.**  Up to about 0.5-0.7 GB of finished chunks moves to the SSD in a few seconds of I/O.  RAM falls as each file moves.
- **fsync now reaches the SSD.**  The flush workers already take turns under one lock, so one 30-50 MB chunk syncs at a time, every ten minutes per channel.
- **Page cache from the direct writes shows in `MemoryCurrent`.**  The kernel reclaims it before any OOM, and `MemoryMax=6G` stays as it is.
- **A full or unwritable data disk** now stops the flush itself.  The writer retries three times, then abandons the chunk.  Task 7 makes that loss visible in the status file, where before nothing showed it.
- **GRAPE days recorded on v3.96 carry zeros where gaps used to be bridged** (open question 1).
- **Rollback costs one partial chunk per channel.**  v3.95's recorder ignores `fill_runs` and writes to `/dev/shm` again.  When it starts mid-chunk, it reopens the boundary chunk B in RAM and names it `<B>`, because its RAM folder holds no such file.  About 21 minutes later its mover (`tiered_storage.py:390-416` at 409fa3fb) moves that file onto the disk and replaces the partial `<B>` that v3.96 wrote there.  Each rollback therefore loses the last partial chunk of every channel.  v3.95 cannot change now, so plan for the loss if a rollback becomes necessary during the B4 measurement or the fleet roll.
