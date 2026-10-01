"""Exact and near-duplicate detection with numeric-conflict awareness.

Two chunks that say the same thing are duplicates: keep the most authoritative/recent one.
Two chunks that say *almost* the same thing but with different numbers ("15 days" vs "30 days") are a
conflict: the newer, more authoritative source wins and the other is marked superseded and flagged.
"""
from __future__ import annotations

import re

WORD = re.compile(r"\w+", re.UNICODE)
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _shingles(text: str, n: int = 3) -> set[tuple[str, ...]]:
    words = WORD.findall(text.lower())
    return {tuple(words[i:i + n]) for i in range(max(1, len(words) - n + 1))}


def similarity(a: str, b: str) -> tuple[float, float]:
    """Return (jaccard, containment of the smaller in the larger) over word 3-shingles."""
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0, 0.0
    inter = len(sa & sb)
    return inter / len(sa | sb), inter / min(len(sa), len(sb))


def numbers(text: str) -> set[str]:
    return set(NUMBER.findall(text))


def rank_key(rec: dict) -> tuple:
    """Preference order when two records collide: authority, then recency, then length."""
    return (rec["_authority"], rec.get("effective_date") or "", len(rec["content"]))


def resolve(records: list[dict], jaccard_min: float = 0.6, containment_min: float = 0.9) -> list[dict]:
    """Mark duplicates (status=duplicate) and conflicts (status=superseded) in place. Returns events."""
    events = []
    active = [r for r in records if r["status"] == "active"]
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            if a["status"] != "active" or b["status"] != "active":
                continue
            # Table groups were de-duplicated row by row; product-specific chunks are never merged across products.
            if "table" in (a["_kind"], b["_kind"]):
                continue
            if a["products"] and b["products"] and set(a["products"]) != set(b["products"]):
                continue
            jac, cont = similarity(a["content"], b["content"])
            if jac < jaccard_min and cont < containment_min:
                continue
            keep, drop = (a, b) if rank_key(a) >= rank_key(b) else (b, a)
            diff = numbers(drop["content"]) ^ numbers(keep["content"])
            if diff:
                drop["status"] = "superseded"
                drop["flags"].append(f"conflict_with:{keep['record_id']} (numbers differ: {sorted(diff)})")
                keep["flags"].append(f"supersedes:{drop['record_id']}")
                kind = "conflict"
            else:
                drop["status"] = "duplicate"
                drop["flags"].append(f"duplicate_of:{keep['record_id']}")
                kind = "duplicate"
            events.append({"type": kind, "kept": keep["record_id"], "dropped": drop["record_id"],
                           "jaccard": round(jac, 2), "containment": round(cont, 2),
                           "kept_source": keep["source"]["source_id"], "dropped_source": drop["source"]["source_id"]})
    return events
