"""Does the sigmond storage ledger manage hf-timestd right now?

sigmond writes /run/sigmond/storage-status.json every 2 minutes.  While a
fresh file says hf-timestd is managed, the ledger owns retention and
hf-timestd's own percentage quota steps aside.  A missing, unparseable or
stale file means "not managed": today's behaviour stands.

Callers read the gate at each enforcement pass.  Nothing here caches.
"""
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_STATUS_PATH = Path("/run/sigmond/storage-status.json")
CLIENT_NAME = "hf-timestd"


def ledger_manages_us(status_path: Path = DEFAULT_STATUS_PATH,
                      now: Callable[[], float] = time.time,
                      max_age_s: float = 900) -> bool:
    """True when the status file is fresh and a clients row for hf-timestd
    has managed true (hf-timestd may have one row per store)."""
    try:
        doc = json.loads(Path(status_path).read_text())
        stamp = datetime.strptime(doc["generated_at"], "%Y-%m-%dT%H:%M:%SZ")
        generated = stamp.replace(tzinfo=timezone.utc).timestamp()
        if now() - generated > max_age_s:
            return False
        return any(isinstance(r, dict)
                   and r.get("client") == CLIENT_NAME
                   and r.get("managed") is True
                   for r in doc["clients"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
