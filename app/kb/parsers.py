"""Source parsers: turn web pages, PDFs, Markdown, CSV tables, forms and text exports into sections.

Every parser returns a ParsedDoc whose sections keep the heading path and page number, so each
knowledge record can later be traced back to the exact place it came from.
"""
from __future__ import annotations

import csv
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
from bs4 import BeautifulSoup, Tag


class ExtractionError(Exception):
    """Raised when a source cannot be parsed into usable text."""


@dataclass
class Section:
    heading_path: list[str]
    text: str
    page: int | None = None
    kind: str = "text"  # text | faq | table | form
    meta: dict = field(default_factory=dict)


@dataclass
class ParsedDoc:
    title: str
    sections: list[Section]
    removed: list[str] = field(default_factory=list)  # boilerplate removed during parsing (for the report)
    notes: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------------------------------
# HTML web pages
# ----------------------------------------------------------------------------------------------
BOILERPLATE_ATTR = re.compile(r"cookie|banner|breadcrumb|social|newsletter|menu|footer|header|login|sidebar", re.I)
STRIP_TAGS = ["script", "style", "noscript", "nav", "header", "footer", "aside", "iframe", "svg", "form"]
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4}


def _push(stack: list, level: int, text: str) -> list:
    """Heading stack of (level, text); returns the new stack. Handles skipped levels (h1 -> h3)."""
    stack = [item for item in stack if item[0] < level]
    return stack + [(level, text)]


def _path(stack: list) -> list[str]:
    return [text for _, text in stack]


def _clean_title(raw: str) -> str:
    return raw.split("|")[0].strip() if raw else ""


def parse_html(path: Path) -> ParsedDoc:
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    title = _clean_title(soup.title.get_text(" ", strip=True) if soup.title else path.stem)
    removed: list[str] = []

    for tag in soup.find_all(STRIP_TAGS):
        removed.append(f"<{tag.name}> " + tag.get_text(" ", strip=True)[:80])
        tag.decompose()
    def attrs_of(tag: Tag) -> str:
        return (" ".join(tag.get("class") or []) + " " + (tag.get("id") or "")).strip()

    for tag in [t for t in soup.find_all(True) if BOILERPLATE_ATTR.search(attrs_of(t))]:
        if tag.decomposed:  # already removed together with a boilerplate ancestor
            continue
        removed.append(f"[{attrs_of(tag)}] " + tag.get_text(" ", strip=True)[:80])
        tag.decompose()
    for a in soup.find_all("a"):
        if a.find_parent(["p", "li", "td"]) is not None:
            a.unwrap()  # inline link: keep its text
        else:  # standalone call-to-action ("Read more", "Get a free quote") carries no knowledge
            removed.append("link: " + a.get_text(" ", strip=True))
            a.decompose()

    root = soup.find("main") or soup.body or soup
    stack: list[str] = []
    sections: list[Section] = []
    current: Section | None = None

    for el in root.find_all(list(HEADINGS) + ["p", "li", "table"]):
        if not isinstance(el, Tag):
            continue
        if el.name in HEADINGS:
            level = HEADINGS[el.name]
            text = el.get_text(" ", strip=True)
            stack = _push(stack, level, text)
            current = Section(heading_path=_path(stack), text="", kind="faq" if text.endswith("?") else "text")
            sections.append(current)
            continue
        if el.find_parent("table") is not None or (el.name == "p" and el.find_parent("li") is not None):
            continue
        if el.name == "table":
            text = _render_table([[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                                  for tr in el.find_all("tr")])
        else:
            text = el.get_text(" ", strip=True)
        if not text:
            continue
        if current is None:
            current = Section(heading_path=[title], text="")
            sections.append(current)
        current.text += ("\n" if current.text else "") + text

    return ParsedDoc(title=title, sections=[s for s in sections if s.text.strip()], removed=removed)


def _render_table(rows: list[list[str]]) -> str:
    rows = [[(c or "").replace("\n", " ").strip() for c in r] for r in rows if r and any(c for c in r)]
    if len(rows) < 2:
        return " ".join(" ".join(r) for r in rows)
    header, body = rows[0], rows[1:]
    lines = []
    for r in body:
        cells = [f"{h}: {v}" for h, v in zip(header[1:], r[1:]) if v]
        lines.append(f"{r[0]} - " + "; ".join(cells) + ".")
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------------
# HTML forms -> canonical form-field schema
# ----------------------------------------------------------------------------------------------
FIELD_ALIASES: list[tuple[str, str]] = [
    (r"nominee", "nominee"),
    (r"plan", "plan"),
    (r"full name|\bname\b", "full_name"),
    (r"d\.?o\.?b|date of birth|birth date", "date_of_birth"),
    (r"mobile|phone|contact no", "mobile_number"),
    (r"e-?mail", "email"),
    (r"pin ?code|postal", "pincode"),
    (r"sum (assured|insured)|cover amount", "sum_insured"),
    (r"members", "members_count"),
    (r"if yes, details|ped details|details of pre", "pre_existing_disease_details"),
    (r"\bped\b|pre[- ]?existing", "pre_existing_disease"),
    (r"occupation", "occupation"),
    (r"\bpan\b", "pan"),
    (r"agree|consent", "contact_consent"),
]
FIELD_LABELS = {
    "full_name": "full name", "date_of_birth": "date of birth", "mobile_number": "mobile number",
    "email": "email address", "pincode": "PIN code", "plan": "plan name", "sum_insured": "sum insured",
    "members_count": "number of members to be insured",
    "pre_existing_disease": "pre-existing disease declaration (yes or no)",
    "pre_existing_disease_details": "details of any pre-existing disease", "occupation": "occupation",
    "nominee": "nominee name and relationship", "pan": "PAN", "contact_consent": "consent to be contacted",
}


def canonical_field(label: str) -> str | None:
    low = label.lower()
    for pattern, name in FIELD_ALIASES:
        if re.search(pattern, low):
            return name
    return None


def parse_form(path: Path) -> ParsedDoc:
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    title = _clean_title(soup.title.get_text(" ", strip=True) if soup.title else path.stem)
    form = soup.find("form")
    if form is None:
        raise ExtractionError("no <form> element found")
    fields = []
    for label in form.find_all("label"):
        raw = label.get_text(" ", strip=True)
        target = form.find(id=label.get("for")) if label.get("for") else label.find(["input", "select", "textarea"])
        required = "*" in raw or (target is not None and target.has_attr("required"))
        name = canonical_field(raw.replace("*", ""))
        cond = re.search(r"\((mandatory if[^)]*)\)", raw, re.I)
        fields.append({"source_label": raw.replace("*", "").strip(), "field": name or "unmapped",
                       "required": bool(required) and not cond, "condition": cond.group(1) if cond else None})
    intro = (soup.find("main") or soup).find("p")
    mandatory = [FIELD_LABELS.get(f["field"], f["source_label"]) for f in fields if f["required"]]
    optional = [FIELD_LABELS.get(f["field"], f["source_label"]) + (f" ({f['condition']})" if f["condition"] else "")
                for f in fields if not f["required"]]
    text = (f"To apply, the proposal form asks for these mandatory details: {', '.join(mandatory)}. "
            f"Optional details: {', '.join(optional)}.")
    if intro:
        text += " " + intro.get_text(" ", strip=True)
    unmapped = [f["source_label"] for f in fields if f["field"] == "unmapped"]
    notes = [f"unmapped form labels: {unmapped}"] if unmapped else []
    return ParsedDoc(title=title, sections=[Section([title, "Required details"], text, kind="form",
                                                    meta={"form_fields": fields})], notes=notes)


# ----------------------------------------------------------------------------------------------
# PDF documents
# ----------------------------------------------------------------------------------------------
def parse_pdf(path: Path) -> ParsedDoc:
    try:
        pdf = pdfplumber.open(path)
        pages = pdf.pages
    except Exception as exc:  # truncated / corrupt files
        raise ExtractionError(f"unreadable PDF ({type(exc).__name__}: {str(exc)[:120]})") from exc

    removed: list[str] = []
    elements = []  # (page_no, top, kind, payload)
    sizes: list[float] = []
    try:
        for pno, page in enumerate(pages, start=1):
            h = float(page.height)
            tables = page.find_tables()
            boxes = [t.bbox for t in tables]

            def outside(obj, boxes=boxes):
                return not any(b[0] <= obj["x0"] and obj["x1"] <= b[2] and b[1] <= obj["top"] and obj["bottom"] <= b[3]
                               for b in boxes)

            for line in page.filter(outside).extract_text_lines():
                text = line["text"].strip()
                if not text:
                    continue
                # Running headers and footers live in the top/bottom margin of every page.
                if line["top"] < 0.07 * h or line["bottom"] > 0.93 * h:
                    removed.append(f"p{pno} header/footer: {text[:90]}")
                    continue
                size = max(c["size"] for c in line["chars"]) if line.get("chars") else 0
                sizes.append(size)
                elements.append((pno, line["top"], "line", (text, size)))
            for t in tables:
                elements.append((pno, t.bbox[1], "table", t.extract()))
    except Exception as exc:
        raise ExtractionError(f"failed while reading pages ({type(exc).__name__}: {str(exc)[:120]})") from exc
    finally:
        pdf.close()

    if sum(len(e[3][0]) for e in elements if e[2] == "line") < 40:
        raise ExtractionError("no extractable text layer (likely a scanned image); OCR required")

    body = statistics.median(sizes) if sizes else 10
    heading_sizes = sorted({round(s, 1) for s in sizes if s >= body + 1.5}, reverse=True)
    elements.sort(key=lambda e: (e[0], e[1]))

    title, stack, sections, current = path.stem, [], [], None
    for pno, _, kind, payload in elements:
        if kind == "table":
            text = _render_table(payload)
            if current is None:
                current = Section([title], "", page=pno); sections.append(current)
            current.text += ("\n" if current.text else "") + text
            continue
        text, size = payload
        if round(size, 1) in heading_sizes:
            level = heading_sizes.index(round(size, 1)) + 1
            if level == 1 and not sections:
                title = text
            text = re.sub(r"^\d+\.\s*", "", text)
            stack = _push(stack, level, text)
            current = Section(_path(stack), "", page=pno); sections.append(current)
            continue
        if current is None:
            current = Section([title], "", page=pno); sections.append(current)
        # Re-join wrapped lines; keep bullets on their own line.
        if text.startswith(("- ", "â€¢ ")) or not current.text:
            current.text += ("\n" if current.text else "") + text
        else:
            current.text += " " + text
    return ParsedDoc(title=title, sections=[s for s in sections if s.text.strip()], removed=removed)


# ----------------------------------------------------------------------------------------------
# Markdown and plain text
# ----------------------------------------------------------------------------------------------
def parse_markdown(path: Path) -> ParsedDoc:
    title, stack, sections, current = path.stem, [], [], None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            level, text = len(m.group(1)), m.group(2).strip()
            if level == 1:
                title = text
            stack = _push(stack, level, text)
            current = Section(_path(stack), "", kind="faq" if text.endswith("?") else "text")
            sections.append(current)
            continue
        if not line:
            continue
        if current is None:
            current = Section([title], ""); sections.append(current)
        current.text += ("\n" if current.text else "") + line
    return ParsedDoc(title=title, sections=[s for s in sections if s.text.strip()])


def parse_text(path: Path) -> ParsedDoc:
    text = path.read_text(encoding="utf-8", errors="replace")
    return ParsedDoc(title=path.stem, sections=[Section([path.stem], text)])


# ----------------------------------------------------------------------------------------------
# CSV tables
# ----------------------------------------------------------------------------------------------
COLUMN_ALIASES = [
    (r"plan", "plan"), (r"member", "members"), (r"age", "age_band"),
    (r"sum (assured|insured)", "sum_insured"), (r"premium", "annual_premium"),
]


def parse_csv(path: Path) -> ParsedDoc:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = [r for r in reader if any(c.strip() for c in r)]
    mapping = {}
    for i, col in enumerate(header):
        for pattern, name in COLUMN_ALIASES:
            if re.search(pattern, col, re.I) and name not in mapping.values():
                mapping[i] = name
                break
    seen, unique, dupes = set(), [], 0
    for r in rows:
        key = tuple(c.strip().lower() for c in r)
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        unique.append({mapping.get(i, header[i]): c.strip() for i, c in enumerate(r)})

    notes = [f"standardised columns: {dict((header[i], n) for i, n in mapping.items())}"]
    if dupes:
        notes.append(f"dropped {dupes} duplicate table row(s)")
    groups: dict[tuple, list[dict]] = {}
    for r in unique:
        groups.setdefault((r.get("plan", ""), r.get("members", "")), []).append(r)
    sections = []
    for (plan, members), items in groups.items():
        lines = [f"Eldest member aged {r['age_band']} years, sum insured Rs. {r['sum_insured']}: "
                 f"indicative annual premium Rs. {r['annual_premium']} (excluding GST)." for r in items]
        heading = f"Indicative annual premium - {plan}, {members.lower()}"
        sections.append(Section([heading], "\n".join(lines), kind="table", meta={"rows": items}))
    return ParsedDoc(title=path.stem, sections=sections, notes=notes)


PARSERS = {"html": parse_html, "form": parse_form, "pdf": parse_pdf, "markdown": parse_markdown,
           "text": parse_text, "csv": parse_csv}

