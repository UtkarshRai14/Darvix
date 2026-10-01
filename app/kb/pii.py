"""PII detection and redaction (regex + context cues). Raw values are never written to reports; only hashes."""
from __future__ import annotations

import hashlib
import re

# Business contact numbers (toll-free) are not personal data.
ALLOWLIST = re.compile(r"\b1800[\s-]?\d{3}[\s-]?\d{4}\b")

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("AADHAAR", re.compile(r"(?<!\d)[2-9]\d{3}\s\d{4}\s\d{4}(?!\d)")),
    ("NIK", re.compile(r"(?<!\d)\d{16}(?!\d)")),  # Indonesian national ID number
    ("PHONE", re.compile(r"(?<![\w])(?:\+91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")),  # India mobile
    ("PHONE", re.compile(r"(?<![\w])(?:\+63\s?|0)9\d{2}[\s-]?\d{3}[\s-]?\d{4}(?!\d)")),  # Philippines mobile
    ("PHONE", re.compile(r"(?<![\w])(?:\+62\s?|0)8\d{2}[\s-]?\d{3,4}[\s-]?\d{3,4}(?!\d)")),  # Indonesia mobile
    ("DOB", re.compile(r"\b(?:DOB|D\.O\.B\.?|date of birth)[:\s]+\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", re.I)),
    # Person names introduced by a context cue ("Shared by Priya Nair", "Caller: Sanjay Kulkarni").
    ("NAME", re.compile(r"(?P<cue>(?:Shared by|Caller:|Name:|verification:) )(?P<val>[A-Z][a-z]+(?: [A-Z][a-z]*\.?){0,2})")),
]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def redact(text: str) -> tuple[str, list[dict]]:
    """Replace PII with typed placeholders. Returns (redacted_text, findings with hashed values)."""
    findings: list[dict] = []
    protected = {m.span() for m in ALLOWLIST.finditer(text)}

    def overlaps_allowlist(span):
        return any(s <= span[0] < e or s < span[1] <= e for s, e in protected)

    for kind, pattern in PATTERNS:
        def repl(m, kind=kind):
            if kind == "PHONE" and overlaps_allowlist(m.span()):
                return m.group(0)
            if "val" in m.re.groupindex:  # keep the context cue, redact only the value
                findings.append({"type": kind, "hash": _digest(m.group("val"))})
                return m.group("cue") + f"[{kind}]"
            findings.append({"type": kind, "hash": _digest(m.group(0))})
            return f"[{kind}]"
        text = pattern.sub(repl, text)
        protected = {m.span() for m in ALLOWLIST.finditer(text)}
    return text, findings
