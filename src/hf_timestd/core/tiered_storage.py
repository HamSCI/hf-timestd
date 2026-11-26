#!/usr/bin/env python3
"""
Drain of the RAM buffer, run once when the recorder starts.

Before v3.96 the archive writer left finished chunks in /dev/shm and a
background thread moved them to disk once they aged out.  Since v3.96 the
writer puts each finished chunk on disk at once and keeps only the chunk still
filling in RAM.  A recorder of the older kind may have left finished chunks in
/dev/shm; ``drain_hot_buffer`` moves them to the disk tree at start, whatever
their age, and nothing moves chunks afterwards.

Layout (both roots are ``raw_buffer`` directories):
    <hot_root>/<channel>/<day>/<boundary>[.seg<N>].{json,bin*}
    <cold_root>/<channel>/<day>/...

The drain never replaces a file on disk.  The new writer may already have
reopened a boundary chunk on disk; if the destination holds the name, the RAM
file stays where it is, and the drain logs an error and counts it as refused.
"""

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from .chunk_segments import chunk_stem, parse_chunk_name

logger = logging.getLogger(__name__)


@dataclass
class DrainResult:
    moved: int    # chunk stems moved to disk (a chunk = its bin and its sidecar)
    refused: int  # chunk stems left in RAM


def _move_without_replacing(src: Path, dest: Path) -> bool:
    """Move src to dest unless dest exists.  True when it moved.

    The copy lands under a name no reader parses, then os.link publishes it
    under the final name.  link fails when dest exists, so a writer that
    creates dest in the meantime keeps its file."""
    staging = dest.with_name(dest.name + ".drain")
    shutil.move(str(src), str(staging))
    try:
        os.link(staging, dest)
    except FileExistsError:
        shutil.move(str(staging), str(src))
        return False
    except BaseException:
        shutil.move(str(staging), str(src))
        raise
    os.unlink(staging)
    return True


def pending_stems(hot_root: Path) -> int:
    """How many chunk stems still sit under hot_root."""
    n = 0
    for day_dir in Path(hot_root).glob('*/*'):
        if day_dir.is_dir():
            n += len({chunk_stem(p[0], p[1]) for p in
                      (parse_chunk_name(f.name) for f in day_dir.iterdir()) if p})
    return n


def drain_hot_buffer(hot_root: Path, cold_root: Path) -> DrainResult:
    """Move every chunk file under hot_root to the same path under cold_root."""
    moved = refused = 0
    hot_root, cold_root = Path(hot_root), Path(cold_root)
    if not hot_root.is_dir():
        return DrainResult(0, 0)
    for channel_dir in sorted(p for p in hot_root.iterdir() if p.is_dir()):
        for day_dir in sorted(p for p in channel_dir.iterdir() if p.is_dir()):
            stems: Dict[str, List[Path]] = {}
            for f in day_dir.iterdir():
                parsed = parse_chunk_name(f.name)
                if parsed is None or not f.is_file():
                    continue  # *.scratch, *.tmp and strangers stay put
                stems.setdefault(chunk_stem(parsed[0], parsed[1]), []).append(f)
            cold_dir = cold_root / channel_dir.name / day_dir.name
            for stem in sorted(stems):
                # The sidecar goes first: a crash between the two moves must
                # never leave a disk bin without the sidecar naming its dtype.
                files = sorted(stems[stem], key=lambda p: not p.name.endswith('.json'))
                taken = [cold_dir / f.name for f in files if (cold_dir / f.name).exists()]
                if taken:
                    logger.error(
                        "drain refused: %s exists on disk; RAM copy %s stays",
                        taken[0], files[0].parent / stem)
                    refused += 1
                    continue
                try:
                    cold_dir.mkdir(parents=True, exist_ok=True)
                    for f in files:
                        if not _move_without_replacing(f, cold_dir / f.name):
                            logger.error(
                                "drain refused: %s appeared on disk; RAM copy %s stays",
                                cold_dir / f.name, f)
                            refused += 1
                            break
                    else:
                        moved += 1
                except Exception as e:
                    logger.error("drain failed for %s: %s", day_dir / stem, e)
                    refused += 1
    return DrainResult(moved, refused)
