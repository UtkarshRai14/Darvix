"""Mock CRM: append-only JSONL tables for leads, escalations, payment promises and call summaries."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from uuid import uuid4

from app.config import CRM_DIR

_lock = threading.Lock()
TABLES = ("leads", "escalations", "payment_promises", "call_summaries")


def append(table: str, payload: dict) -> dict:
    assert table in TABLES, table
    record = {"id": f"{table[:3].upper()}-{uuid4().hex[:8]}",
              "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **payload}
    with _lock, (CRM_DIR / f"{table}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read(table: str, limit: int = 50) -> list[dict]:
    path = CRM_DIR / f"{table}.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows[-limit:][::-1]
