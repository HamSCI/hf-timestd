#!/usr/bin/env python3
"""
ChainDelayStore — the on-disk record of a station's learned T6 chain delay.

The offset judge learns how far the T6 (TS-1) bench sits from the lower
tier it is judged against.  This store keeps that value, its candidate
replacement and a short history, in one JSON file (layout: spec
docs/superpowers/specs/2026-10-27-t6-chain-delay-learning-design.md §2).

The value is a diagnostic.  It never corrects a timestamp, the chrony feed
or the T6 anchor.

Rules:
  * Only the judge writes the record.  The CLI writes a request file beside
    it (<path>.request.json), which the judge consumes with take_request().
  * Writes are atomic (temp file, fsync, rename).
  * A failed save keeps the record in memory and logs one ERROR per failure
    streak; the next successful save re-arms the log.
  * A record whose identity no longer matches the running config goes to
    history with one WARNING.  A corrupt file moves aside with one ERROR.
  * load() never raises.
"""

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChainDelayRecord:
    offset_ns: float
    sigma_ns: float
    spread_ns: float
    n: int
    n_eff: float
    span_s: float
    learned_from_utc: str
    learned_to_utc: str
    lower_tier: str
    state: str  # "seeded" | "confirmed"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ChainDelayRecord":
        """Build from a dict, ignoring unknown keys.  Raises on a missing field."""
        return cls(**{f.name: d[f.name] for f in fields(cls)})


class ChainDelayStore:
    SCHEMA = 1
    HISTORY_MAX = 10

    def __init__(
        self,
        path: os.PathLike,
        identity: Dict[str, float],
        time_fn: Callable[[], float] = time.time,
    ):
        self._path = Path(path)
        self._identity = dict(identity)
        self._time_fn = time_fn
        self._record: Optional[ChainDelayRecord] = None
        self._candidate: Optional[ChainDelayRecord] = None
        self._history: List[Dict[str, Any]] = []
        self._save_failing = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def request_path(self) -> Path:
        return self._path.with_name(self._path.name + ".request.json")

    @property
    def record(self) -> Optional[ChainDelayRecord]:
        return self._record

    @property
    def candidate(self) -> Optional[ChainDelayRecord]:
        return self._candidate

    # ---- load -------------------------------------------------------------

    def load(self) -> None:
        """Read the file.  Never raises; problems leave the store empty."""
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.error("chain delay: cannot read %s: %s", self._path, exc)
            return
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict) or data.get("schema") != self.SCHEMA:
                raise ValueError("not a schema-%d chain delay file" % self.SCHEMA)
            record = (
                ChainDelayRecord.from_dict(data) if "offset_ns" in data else None
            )
            cand = data.get("candidate")
            candidate = ChainDelayRecord.from_dict(cand) if cand else None
            history = data.get("history", [])
            if not isinstance(history, list):
                raise ValueError("history is not a list")
            identity = data.get("identity")
            if not isinstance(identity, dict):
                raise ValueError("identity is missing")
        except (ValueError, KeyError, TypeError) as exc:  # includes UnicodeDecodeError
            self._move_aside(exc)
            return

        self._history = [h for h in history if isinstance(h, dict)][-self.HISTORY_MAX:]
        if identity != self._identity:
            logger.warning(
                "chain delay: stored identity %s differs from running config %s; "
                "setting the record aside and learning again",
                identity, self._identity,
            )
            self._push_history(record)
            self._push_history(candidate)
            self._save()
            return
        self._record = record
        self._candidate = candidate

    def _move_aside(self, exc: Exception) -> None:
        stamp = datetime.fromtimestamp(self._time_fn(), timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )
        dest = self._path.with_name("%s.corrupt-%s" % (self._path.name, stamp))
        try:
            os.replace(self._path, dest)
            where = "moved to %s" % dest
        except OSError as mv_exc:
            where = "could not be moved aside (%s)" % mv_exc
        logger.error("chain delay: %s is corrupt (%s); %s", self._path, exc, where)

    # ---- mutation ---------------------------------------------------------

    def set_record(self, rec: ChainDelayRecord) -> bool:
        self._push_history(self._record)
        self._record = rec
        return self._save()

    def set_candidate(self, rec: Optional[ChainDelayRecord]) -> bool:
        self._candidate = rec
        return self._save()

    def promote_candidate(self) -> bool:
        """The candidate becomes the record; the old record goes to history."""
        if self._candidate is None:
            return False
        self._push_history(self._record)
        self._record = self._candidate
        self._candidate = None
        return self._save()

    def discard(self) -> bool:
        """Record and candidate go to history; learning starts again."""
        self._push_history(self._record)
        self._push_history(self._candidate)
        self._record = None
        self._candidate = None
        return self._save()

    def _now_iso(self) -> str:
        return datetime.fromtimestamp(self._time_fn(), timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def _push_history(self, rec: Optional[ChainDelayRecord]) -> None:
        if rec is None:
            return
        entry = rec.to_dict()
        entry["replaced_utc"] = self._now_iso()
        self._history.append(entry)
        del self._history[: -self.HISTORY_MAX]

    # ---- requests (CLI -> judge) ------------------------------------------

    def take_request(self) -> Optional[Dict]:
        """Read and delete the request file.  An unreadable one is deleted too."""
        try:
            raw = self.request_path.read_text()
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("chain delay: cannot read request file: %s", exc)
            raw = None
        req: Optional[Dict] = None
        if raw is not None:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    req = parsed
                else:
                    logger.warning("chain delay: request file is not an object; dropped")
            except ValueError as exc:
                logger.warning("chain delay: unreadable request file dropped: %s", exc)
        try:
            self.request_path.unlink()
        except OSError as exc:
            # An undeletable request would replay every tick; refuse it.
            logger.error(
                "chain delay: cannot delete request file %s (%s); ignoring it",
                self.request_path, exc,
            )
            return None
        return req

    @staticmethod
    def write_request(path: os.PathLike, request: Dict) -> None:
        """Atomically write a request file (the CLI side).  Raises on failure."""
        ChainDelayStore._atomic_write(Path(path), json.dumps(request, indent=2))

    # ---- status / save ----------------------------------------------------

    def to_status(self) -> Dict:
        return {
            "path": str(self._path),
            "record": self._record.to_dict() if self._record else None,
            "candidate": self._candidate.to_dict() if self._candidate else None,
            "identity": dict(self._identity),
            "history_len": len(self._history),
        }

    def _payload(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"schema": self.SCHEMA}
        if self._record is not None:
            data.update(self._record.to_dict())
        data["candidate"] = self._candidate.to_dict() if self._candidate else None
        data["identity"] = self._identity
        data["history"] = self._history
        return data

    def _save(self) -> bool:
        try:
            self._atomic_write(self._path, json.dumps(self._payload(), indent=2))
        except OSError as exc:
            if not self._save_failing:
                logger.error(
                    "chain delay: cannot save %s (%s); keeping the record in "
                    "memory", self._path, exc,
                )
            self._save_failing = True
            return False
        self._save_failing = False
        return True

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", dir=path.parent, prefix=path.name + ".", suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp_name = tmp.name
                tmp.write(text)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.chmod(tmp_name, 0o644)  # readable by the CLI and station-web
            os.replace(tmp_name, path)
            tmp_name = None
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
