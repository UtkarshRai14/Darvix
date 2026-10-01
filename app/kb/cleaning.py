"""Text cleaning and standardisation: boilerplate, terminology, currency, dates and source-error checks."""
from __future__ import annotations

import re
import unicodedata
from datetime import date

# Lines that carry no knowledge even after HTML/PDF structure has been stripped.
BOILERPLATE_LINE = re.compile(
    r"^(read more|get a free quote|download brochure|click here|accept all|manage preferences|submit)$"
    r"|all rights reserved|subject matter of solicitation|^page \d+( of \d+)?$|^follow us"
    r"|^(last |page )?updated( on)?:? [\w /,-]+$",  # page metadata; the manifest holds effective_date
    re.I,
)

# Terminology standardisation per collection: (pattern, canonical form). Applied case-insensitively.
GLOSSARY: dict[str, list[tuple[str, str]]] = {
    "health_in": [
        (r"\bpre[\s-]?existing (?:illness|condition|disease)s\b", "pre-existing diseases"),
        (r"\bpre[\s-]?existing (?:illness|condition|disease)\b", "pre-existing disease"),
        (r"\bPEDs\b", "pre-existing diseases"),
        (r"\bPED\b", "pre-existing disease"),
        (r"\bsum assured\b", "sum insured"),
        (r"\bfree[\s-]look period\b", "free-look period"),
        (r"\bNCB\b|\bno-claim bonus\b", "no claim bonus"),
        (r"\bcashless facility\b", "cashless treatment"),
        (r"\bempanelled hospitals\b|\btie-up hospitals\b", "network hospitals"),
        (r"\bempanelled hospital\b|\btie-up hospital\b", "network hospital"),
        (r"\bco-?pay(?:ment)?\b", "co-payment"),
        (r"\bhospitali[sz]ation\b", "hospitalisation"),
    ],
    "life_ph": [
        (r"\bface value\b", "face amount"),
        (r"\bautomatic premium loan\b", "Automatic Premium Loan"),
    ],
    "finance_id": [
        (r"\buang muka\b(?! \()", "DP (uang muka)"),
    ],
}

CURRENCY = [
    (re.compile(r"\b(?:Rs\.?|INR)\s?(?=\d)"), "₹"),
    (re.compile(r"₹(\d+(?:\.\d+)?)L\b"), r"₹\1 lakh"),
    (re.compile(r"\bPHP\s?(?=\d)"), "₱"),
    (re.compile(r"\bRp\.?\s(?=\d)"), "Rp"),
]

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], start=1)}
MONTH_RE = "|".join(MONTHS)
DATE_PATTERNS = [
    # 15/03/2024 or 15-03-2024 (Indian day-first convention)
    (re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b"), lambda m: (int(m[3]), int(m[2]), int(m[1]))),
    # 1st April 2026, 12 June 2026
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_RE})\s+(\d{{4}})\b", re.I),
     lambda m: (int(m[3]), MONTHS[m[2].lower()], int(m[1]))),
    # April 1, 2026
    (re.compile(rf"\b({MONTH_RE})\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.I),
     lambda m: (int(m[3]), MONTHS[m[1].lower()], int(m[2]))),
]


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line and not BOILERPLATE_LINE.search(line):
            lines.append(line)
    return "\n".join(lines)


def standardize_terms(text: str, collection: str) -> tuple[str, list[str]]:
    changes: list[str] = []
    for pattern, canonical in GLOSSARY.get(collection, []):
        def repl(m, canonical=canonical):
            if m.group(0) != canonical:
                changes.append(f"{m.group(0)} -> {canonical}")
            # Capitalise when the term starts a sentence.
            start = m.start()
            at_sentence_start = start == 0 or text[max(0, start - 2):start] in (". ", "? ", "! ") or \
                text[start - 1:start] == "\n"
            return canonical[0].upper() + canonical[1:] if at_sentence_start else canonical
        text = re.sub(pattern, repl, text, flags=re.I)
    for pattern, repl in CURRENCY:
        text = pattern.sub(repl, text)
    return text, sorted(set(changes))


def standardize_dates(text: str) -> tuple[str, list[str], list[str]]:
    """Rewrite recognised dates as ISO 8601. Returns (text, iso_dates, flags for invalid dates)."""
    found, flags = [], []

    def make_repl(extract):
        def repl(m):
            y, mo, d = extract(m)
            try:
                iso = date(y, mo, d).isoformat()
            except ValueError:
                flags.append(f"invalid_date: '{m.group(0)}'")
                return m.group(0)
            found.append(iso)
            return iso
        return repl

    for pattern, extract in DATE_PATTERNS:
        text = pattern.sub(make_repl(extract), text)
    return text, found, flags


OBSOLETE = re.compile(r"no longer available|has been discontinued|is discontinued|tidak berlaku lagi|hindi na available",
                      re.I)


def is_obsolete(text: str) -> bool:
    """Content that announces itself as expired (ended campaigns, discontinued offers) is irrelevant."""
    return bool(OBSOLETE.search(text))


def validate_facts(text: str) -> list[str]:
    """Flag obvious source errors such as impossible ages or percentages."""
    flags = []
    for m in re.finditer(r"\b(\d{3,})\s*years\b", text):
        if int(m.group(1)) > 120:
            flags.append(f"implausible_age: '{m.group(0)}'")
    for m in re.finditer(r"\b(\d{3,}(?:[.,]\d+)?)\s*%", text):
        if float(m.group(1).replace(",", ".")) > 100:
            flags.append(f"implausible_percentage: '{m.group(0)}'")
    return flags
