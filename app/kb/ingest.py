"""Knowledge-base ingestion pipeline.

    python -m app.kb.ingest                 # all collections
    python -m app.kb.ingest health_in       # one collection

raw sources -> parse -> clean -> standardise (terms, currency, dates) -> validate -> PII redaction
-> section-aware chunking -> taxonomy -> dedup/conflict resolution -> versioning -> records.jsonl + index
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone

from app.config import KB_DIR, RAW_DIR
from app.kb import dedup
from app.kb.cleaning import is_obsolete, normalize_text, standardize_dates, standardize_terms, validate_facts
from app.kb.parsers import PARSERS, ExtractionError
from app.kb.pii import redact
from app.kb.store import build_index

MAX_WORDS = 160  # ~10-15 s of speech: small enough for a voice answer, large enough to be self-contained

# Section headings that override a source's default category (only for sources with section_categories).
SECTION_RULES = [
    ("qualification", r"eligib|entry age|who can be covered|underwriting|medical exam|maximum sum insured|residency"),
    ("claims", r"claim|cashless|reimburse|grievance"),
    ("policy", r"waiting|exclusion|co-?payment|maternity|grace|lapse|reinstat|cancel|denda|jatuh tempo|beneficiar"
               r"|janji bayar|slik|restruktur|free-look|while premiums are unpaid|automatic premium loan|hardship"),
    ("pricing", r"premium|discount|payment|pembayaran|bayar|\btenor\b|\bdp\b"),
]

CATEGORIES = {
    "product": "Plan features, benefits, riders and options",
    "policy": "Coverage terms: waiting periods, exclusions, co-payment, grace/lapse, penalties, cancellation",
    "qualification": "Eligibility, underwriting and entry rules",
    "pricing": "Premiums, discounts, payment modes and payment channels",
    "claims": "Cashless/reimbursement claims, grievances",
    "faq": "Customer frequently asked questions",
    "objection": "Objection-handling guidance for agents",
    "form": "Application forms and required details",
    "referral": "Referral and handoff processes",
    "partnership_benefits": "Branch partner programme",
    "company": "Company overview",
    "testimonial": "Customer stories (marketing)",
}


def _words(text: str) -> int:
    return len(text.split())


def chunk(text: str, max_words: int = MAX_WORDS) -> list[str]:
    """Pack lines/sentences into chunks of <= max_words, carrying one short unit as overlap."""
    if _words(text) <= max_words:
        return [text]
    units: list[str] = []
    for line in text.split("\n"):
        if _words(line) <= max_words:
            units.append(line)
        else:
            units.extend(s for s in re.split(r"(?<=[.!?])\s+", line) if s)
    chunks, cur = [], []
    for u in units:
        if cur and _words("\n".join(cur + [u])) > max_words:
            chunks.append("\n".join(cur))
            cur = [cur[-1]] if _words(cur[-1]) < 40 else []
        cur.append(u)
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _category(src: dict, heading_path: list[str]) -> str:
    if src.get("section_categories"):
        for heading in reversed(heading_path[1:] or heading_path):
            for cat, pattern in SECTION_RULES:
                if re.search(pattern, heading, re.I):
                    return cat
    return src["category"]


def _products(manifest: dict, src: dict, heading_path: list[str]) -> list[str]:
    heading = " ".join(heading_path)
    found = [p for p, pattern in manifest.get("products", {}).items() if re.search(pattern, heading, re.I)]
    return found or list(src.get("products", []))


def _load_previous(collection: str) -> tuple[dict, dict]:
    path = KB_DIR / collection / "records.jsonl"
    meta_path = KB_DIR / collection / "meta.json"
    prev = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                prev[rec["record_key"]] = rec
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    return prev, meta


def _bump(version: str) -> str:
    major, minor = version.split(".")
    return f"{major}.{int(minor) + 1}"


def ingest(collection: str) -> dict:
    base = RAW_DIR / collection
    manifest = json.loads((base / "sources.json").read_text(encoding="utf-8"))
    prev, prev_meta = _load_previous(collection)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    records: list[dict] = []
    source_reports = []
    pii_counter: Counter = Counter()
    used_ids = {r["record_id"] for r in prev.values()}

    for src in manifest["sources"]:
        path = base / src["path"]
        entry = {"source_id": src["source_id"], "path": src["path"], "type": src["type"], "status": "ok",
                 "sections": 0, "records": 0, "boilerplate_removed": 0, "notes": [], "issues": []}
        source_reports.append(entry)

        if src.get("exclude_reason"):
            _, findings = redact(path.read_text(encoding="utf-8", errors="replace"))
            entry.update(status="excluded", notes=[src["exclude_reason"]],
                         pii_found=dict(Counter(f["type"] for f in findings)))
            continue
        try:
            doc = PARSERS[src["type"]](path)
        except (ExtractionError, OSError, UnicodeDecodeError) as exc:
            entry.update(status="failed", issues=[str(exc)])
            continue

        entry["boilerplate_removed"] = len(doc.removed)
        entry["boilerplate_examples"] = doc.removed[:6]
        entry["notes"] += doc.notes
        entry["sections"] = len(doc.sections)
        counter = 0

        for sec in doc.sections:
            text = normalize_text(sec.text)
            if _words(text) < 5:
                entry["notes"].append(f"dropped trivial section: {' > '.join(sec.heading_path)}")
                continue
            heading_path = [standardize_terms(normalize_text(h), collection)[0] for h in sec.heading_path]
            text, term_changes = standardize_terms(text, collection)
            text, dates, date_flags = standardize_dates(text)
            fact_flags = validate_facts(text)
            text, pii_findings = redact(text)
            pii_counter.update(f["type"] for f in pii_findings)

            for ci, piece in enumerate(chunk(text)):
                category = _category(src, heading_path)
                key = f"{src['source_id']}::{'/'.join(heading_path)}::{ci}"
                counter += 1
                old = prev.get(key)
                if old:
                    record_id = old["record_id"]
                else:
                    n = counter
                    record_id = f"kb_{category}_{src['short']}_{n:03d}"
                    while record_id in used_ids:
                        n += 1
                        record_id = f"kb_{category}_{src['short']}_{n:03d}"
                used_ids.add(record_id)
                flags = date_flags + fact_flags
                records.append({
                    "record_id": record_id,
                    "record_key": key,
                    "collection": collection,
                    "title": " > ".join(heading_path),
                    "content": piece,
                    "category": category,
                    "doc_type": {"html": "web_page", "form": "form", "pdf": "pdf", "markdown": "document",
                                 "csv": "table", "text": "text"}[src["type"]],
                    "products": _products(manifest, src, heading_path),
                    "language": manifest["language"],
                    "audience": src.get("audience", "customer"),
                    "source": {"source_id": src["source_id"], "path": f"data/raw/{collection}/{src['path']}",
                               "url": src.get("url"), "section": heading_path[-1], "page": sec.page},
                    "effective_date": src.get("effective_date"),
                    "dates_mentioned": dates,
                    "terminology_standardised": term_changes,
                    "pii": {"detected": bool(pii_findings), "types": sorted({f["type"] for f in pii_findings}),
                            "redacted": bool(pii_findings)},
                    "status": "obsolete" if is_obsolete(piece) else "quarantined" if flags else "active",
                    "flags": flags + (["obsolete_content"] if is_obsolete(piece) else []),
                    "form_fields": sec.meta.get("form_fields"),
                    "_authority": src.get("authority", 1),
                    "_kind": sec.kind,
                })
        entry["records"] = counter

    dedup_events = dedup.resolve(records)

    # Versioning: unchanged content keeps its version; changed content gets a minor bump.
    changed = 0
    for rec in records:
        # Content + taxonomy metadata: a re-categorised record is a new version too.
        rec["content_hash"] = hashlib.sha1("\n".join([rec["title"], rec["content"], rec["category"],
                                                     ",".join(rec["products"])]).encode()).hexdigest()
        old = prev.get(rec["record_key"])
        if old and old["content_hash"] == rec["content_hash"] and old.get("status") == rec["status"]:
            rec["version"], rec["ingested_at"] = old["version"], old["ingested_at"]
        else:
            rec["version"] = _bump(old["version"]) if old else "1.0"
            rec["ingested_at"] = now
            changed += 1
        rec.pop("_authority"), rec.pop("_kind")
    removed = sorted(k for k in prev if k not in {r["record_key"] for r in records})
    kb_version = prev_meta.get("kb_version", 0) + (1 if (changed or removed or not prev) else 0)

    out = KB_DIR / collection
    out.mkdir(parents=True, exist_ok=True)
    with (out / "records.jsonl").open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    status_counts = Counter(r["status"] for r in records)
    report = {
        "collection": collection,
        "name": manifest["name"],
        "kb_version": kb_version,
        "generated_at": now,
        "totals": {
            "sources": len(manifest["sources"]),
            "sources_failed": sum(1 for s in source_reports if s["status"] == "failed"),
            "sources_excluded": sum(1 for s in source_reports if s["status"] == "excluded"),
            "records": len(records),
            "active": status_counts.get("active", 0),
            "duplicates_removed": status_counts.get("duplicate", 0),
            "superseded_conflicts": status_counts.get("superseded", 0),
            "quarantined": status_counts.get("quarantined", 0),
            "obsolete_removed": status_counts.get("obsolete", 0),
            "records_with_pii_redacted": sum(1 for r in records if r["pii"]["detected"]),
            "pii_entities_redacted": dict(pii_counter),
            "records_changed_this_run": changed,
            "records_removed_this_run": len(removed),
        },
        "categories": dict(Counter(r["category"] for r in records if r["status"] == "active")),
        "taxonomy": CATEGORIES,
        "sources": source_reports,
        "dedup_events": dedup_events,
        "flagged_records": [{"record_id": r["record_id"], "status": r["status"], "flags": r["flags"],
                             "source": r["source"]["path"]} for r in records if r["flags"]],
        "removed_record_keys": removed,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "meta.json").write_text(json.dumps({"kb_version": kb_version, "generated_at": now}), encoding="utf-8")
    build_index(collection)
    return report


def main(argv: list[str]) -> None:
    collections = argv or sorted(p.name for p in RAW_DIR.iterdir() if (p / "sources.json").exists())
    for c in collections:
        rep = ingest(c)
        t = rep["totals"]
        print(f"[{c}] v{rep['kb_version']}: {t['active']} active records | {t['duplicates_removed']} duplicates | "
              f"{t['superseded_conflicts']} superseded | {t['quarantined']} quarantined | "
              f"{t['sources_failed']} failed sources | PII redacted: {t['pii_entities_redacted']}")


if __name__ == "__main__":
    main(sys.argv[1:])
