# Knowledge base design (Part 2)

Goal: turn mixed business content into records that a voice agent can retrieve in milliseconds, quote safely,
and trace back to an exact source section and version.

Code: `app/kb/` · Inputs: `data/raw/<collection>/` · Output: `data/kb/<collection>/` · Run: `python -m app.kb.ingest`

## 1. Inputs

Three collections are built by the same pipeline:

| Collection | Content | Language |
|---|---|---|
| `health_in` | Nivaran Health (India): 9 web pages, product brochure PDF, internal underwriting PDF, scanned claim-form PDF, corrupt rate-card PDF, premium table (CSV), proposal form (HTML form), objection playbook (Markdown), CRM call-notes export (text) | English |
| `life_ph` | Liwanag Life via Sinag Bank (Philippines): product guide, payment/lapse rules, objection guide with Taglish phrasing, bank-referral process | English (as PH insurers write internal docs), queried in Taglish |
| `finance_id` | Arunika Multifinance (Indonesia): installment and penalty rules, payment channels, FAQ, objection guide | Bahasa Indonesia |

The `health_in` sources deliberately contain the problems listed in the brief:
- navigation, headers, footers, cookie banners and calls to action;
- a block repeated on four pages;
- an archived FAQ with inconsistent terminology (PED, sum assured, cashless facility), Indian date formats and an outdated 15-day free-look period;
- an expired campaign;
- an implausible value ("755 years");
- a scanned image-only PDF and a truncated PDF;
- a table with inconsistent column names and a duplicate row;
- a form with abbreviated labels;
- testimonials and a CRM export containing names, phone numbers, emails, PAN, Aadhaar and date of birth.

Each collection has a **manifest** (`sources.json`) giving every source:
- `source_id`, `path`, `type` and canonical `url`;
- `effective_date`;
- an `authority` rank (internal policy 4 > brochure/playbook 3 > website 2 > archive/marketing 1);
- the default `category` and the `audience` (customer-facing or internal guidance);
- an optional exclusion reason.

It also holds the market's product patterns and **query-expansion glossary**, covered in section 6.

## 2. Extraction and parsing (`parsers.py`)

| Type | Method | Boilerplate handling |
|---|---|---|
| HTML pages | BeautifulSoup. Drops `script/style/nav/header/footer/aside/form/iframe`, any element whose class/id matches `cookie|banner|breadcrumb|social|newsletter|menu|footer|header|login|sidebar`, and standalone call-to-action links (inline links keep their text). Walks `h1-h4/p/li/table` in document order to build a heading path; an `h3` ending in `?` becomes a FAQ section. | Each removed element is logged (count + examples per source in `report.json`). |
| PDFs | pdfplumber. Lines in the top/bottom 7% of each page are running headers/footers. Headings are detected by font size (body median + 1.5 pt, ranked into levels). Tables are found with `find_tables()`, their area is excluded from the text flow and rendered row by row as `Row - Column: value`. | Header/footer lines logged per page. |
| CSV tables | Columns are mapped to canonical names (`Sum Assured (Rs.)` → `sum_insured`), exact duplicate rows are dropped, and rows are grouped (plan × members) and rendered as speakable sentences. | Duplicate row count reported. |
| HTML forms | Labels are mapped to a canonical field schema (`D.O.B` → `date_of_birth`, `Any PED? (Y/N)` → `pre_existing_disease`, `Sum Assured` → `sum_insured`) with required/conditional flags, then rendered as one "required details" record that keeps `form_fields` as structured metadata. | Unmapped labels are reported. |
| Markdown / text | `#` heading levels → heading path. | - |

**Extraction failures** never stop the run. The source is marked `failed` with a reason, for example:
- `no extractable text layer (likely a scanned image); OCR required`
- `unreadable PDF (PdfminerException: Unexpected EOF)`

**Excluded sources** (the CRM export) are still scanned for PII so the report shows what was kept out.

## 3. Cleaning and standardisation (`cleaning.py`)

- **Normalisation:** Unicode NFKC, straight quotes, collapsed whitespace, and removal of leftover boilerplate lines ("All rights reserved", "Page 2 of 3", "Read more").
- **Terminology:** a per-collection glossary rewrites variants to a canonical term:
  - pre-existing disease ← PED / pre existing illness / pre-existing condition;
  - sum insured ← sum assured;
  - free-look period, no claim bonus ← NCB;
  - cashless treatment, network hospital ← empanelled / tie-up hospital;
  - co-payment, hospitalisation.

  Every change is listed on the record (`terminology_standardised`). Headings are standardised too.
- **Currency:** `Rs.`/`INR` → `₹`, `₹5L` → `₹5 lakh`, `PHP` → `₱`, `Rp 1.000` → `Rp1.000`.
- **Dates:** `15/03/2024`, `1st April 2026` and `April 1, 2026` all become ISO `2026-04-01`. Recognised dates are stored in `dates_mentioned`; impossible dates (e.g. 31/02) are flagged.
- **Source-error checks:** ages above 120 years and percentages above 100% are flagged. A flagged record is **quarantined**: kept for review, never indexed.
- **Obsolete content:** text that declares itself expired ("no longer available", "has been discontinued") is marked `obsolete` and not indexed.

## 4. PII protection (`pii.py`)

Detects the following with typed regexes and context cues:
- emails;
- Indian, Philippine and Indonesian mobile numbers;
- PAN, Aadhaar, Indonesian NIK and dates of birth;
- names introduced by cues ("Shared by …", "Caller: …").

Toll-free business numbers (`1800-…`) are allow-listed. Values are replaced with placeholders such as
`[NAME]` and `[PHONE]`. Records carry `pii: {detected, types, redacted}`, and the report contains only counts by
type. Raw values are never written to the index or reports. The agent profiles also forbid collecting
Aadhaar, PAN, card or OTP details on calls.

## 5. Records, chunking, taxonomy, versioning (`ingest.py`, `dedup.py`)

**Chunking.** One record per heading section, so the unit is semantically whole: one FAQ answer, one objection,
one policy clause. Sections over 160 words (about 10-15 s of speech) are split on line/sentence boundaries, and a
short last unit carries over as overlap. Sections under 5 words are dropped. Records keep the full heading path
as `title`, so a chunk is self-explanatory out of context, e.g.
`Nivaran Family Floater > Waiting periods`.

**Taxonomy.**
- `category` is one of: `product`, `policy`, `qualification`, `pricing`, `claims`, `faq`, `objection`, `form`, `referral`, `partnership_benefits`, `company`, `testimonial`.
- Product pages, the brochure and the market docs use **section-level categories** from the heading, e.g. "Waiting periods" → `policy`, "Eligibility" → `qualification`. Other sources use the manifest category.
- `products` comes from heading-path patterns in the manifest, so a "Waiting periods" chunk under "Family Floater" is tagged `family_floater`.
- `audience` (`customer` | `internal`) tells the agent to paraphrase internal guidance rather than read it out.

**Duplicates and conflicts.**
- Word-3-shingle Jaccard ≥ 0.6, or containment ≥ 0.9, marks a near-duplicate.
- If the two texts contain the same numbers, the lower-ranked one (authority → effective date → length) becomes a `duplicate`.
- If the numbers differ, it is a **conflict**: the more authoritative, newer record wins and the other becomes `superseded`, with `conflict_with:<id> (numbers differ: ['15', '30'])`.
- Tables and chunks tagged with different products are never merged.

**Versioning.**
- `record_key` = `source_id::heading path::chunk index` is the stable identity.
- `content_hash` covers title, content, category and product tags.
- Unchanged → same `version` and `ingested_at`. Changed → minor bump (`1.0` → `1.1`). New → `1.0`. Removed keys are listed in the report.
- The collection's `kb_version` increments only when something changed. This was verified by editing one sentence of `claims.html`: exactly one record moved to `1.1` and the collection to v2.

### Schema

| Field | Example / meaning |
|---|---|
| `record_id` | `kb_partnership_benefits_prt_002` (category + source short code + sequence; stable across runs) |
| `record_key` | `web_partners::Grow with Nivaran: branch partnership/Branch partnership benefits::0` |
| `title` | `Grow with Nivaran: branch partnership > Branch partnership benefits` |
| `content` | `Operational, marketing, and technology support is provided to branch partners. This includes a dedicated relationship manager, …` |
| `category` / `doc_type` | `partnership_benefits` / `web_page` |
| `products`, `language`, `audience` | `[]`, `en`, `customer` |
| `source` | `{source_id: web_partners, path: data/raw/health_in/website/partners.html, url: https://www.nivaranhealth.example/partners, section: Branch partnership benefits, page: null}` |
| `effective_date`, `dates_mentioned` | `2026-06-01`, `[]` |
| `terminology_standardised` | e.g. `["free look period -> free-look period"]` |
| `pii` | `{detected: false, types: [], redacted: false}` |
| `status`, `flags` | `active` \| `duplicate` \| `superseded` \| `quarantined` \| `obsolete`, with reasons |
| `version`, `content_hash`, `ingested_at` | `1.0`, sha1, ISO timestamp |
| `form_fields` | structured canonical fields (form records only) |

Sample non-active records (real output):
- `kb_faq_faqold_003` is **superseded**. It is the archived 15-day free-look FAQ, flagged `conflict_with:kb_faq_faq_003 (numbers differ: ['15', '30'])`.
- `kb_qualification_uw_002` is **quarantined**, flagged `implausible_age: '755 years'`. The same entry ages remain available from the brochure and product pages.
- `kb_testimonial_tst_002` is active with `pii.types = [EMAIL, NAME, PHONE]` and content `… Shared by [NAME] from Kochi ([EMAIL], [PHONE]).`

## 6. Indexing and retrieval (`store.py`)

- **Embeddings:** `paraphrase-multilingual-MiniLM-L12-v2` (384-d, ONNX on CPU via fastembed) over `title + content` of **active records only**. Stored as a normalised NumPy matrix.
- **Lexical:** BM25 (rank-bm25), built at load time, with a small English/Tagalog/Indonesian stop-word list.
- **Query expansion:** the manifest glossary appends canonical KB terms for local or colloquial words before both searches:
  - Indonesian `cicilan` → `angsuran`, `telat` → `terlambat denda`, `PHK` → `kehilangan pekerjaan restrukturisasi`;
  - Taglish `pera`/`pambayad` → `money payment premium`;
  - Indian English "insurance from my company" → `employer health cover`.

  This is the market terminology list doubling as a retrieval aid.
- **Ranking:** reciprocal-rank fusion of the top 20 dense and top 20 BM25 results, `score = Σ 1/(60 + rank)`.
- **Relevance gate:** a hit is *relevant* if it clears any step: dense ≥ 0.55; or dense ≥ 0.40 and BM25 ≥ 4; or dense ≥ 0.30 and BM25 ≥ 6. The steps trade semantic against lexical evidence, so cross-lingual or colloquial matches with strong keyword overlap still pass.

  The thresholds were calibrated on the 25-query set below, which has **no held-out split**. That is a known limitation.
- **Use by the voice agent:** each customer utterance triggers a top-3 search. Short follow-ups are concatenated with the previous utterance. Passages are labelled `relevant` or `weak match`; if nothing passes the gate the agent must answer `info_unavailable`.

## 7. Citation method

- Every passage given to the agent carries its `record_id`.
- The agent must list the IDs it used in `citations`. Code drops any ID that was not retrieved for that turn.
- An answer marked `answered_from_kb` without a valid citation is regenerated once, then replaced by the safe "I don't have verified information" fallback.
- Citations are resolved to `source path > section, page (version)`, e.g. `data/raw/health_in/website/faq.html > Are pre-existing diseases covered? (v1.0)`. They are shown on each agent bubble in the UI and listed in `transcript.md`.

## 8. Retrieval testing

`python -m app.kb.evaluate` runs `data/eval/retrieval_queries.json`: 25 questions covering product, policy,
qualification, FAQ, objection, pricing, claims, form, partnership and out-of-scope, across all three
collections. Each query records the top record, source reference, scores, an explanation and a verdict.

Current result (`reports/retrieval_eval.md`): **22 correct, 3 partially correct, 0 incorrect**, median latency
about 9 ms. The partial results are instructive:

| ID | Question | What happened |
|---|---|---|
| Q11 | "premium for a 30 year old for 5 lakh cover" | Ranked the Family Floater table above the individual Essential table. The question is ambiguous: it doesn't say whether one person or a family is covered. |
| Q17 | Taglish grace-period question | Ranked the objection guide, which also states the 31-day grace period, above the policy section. The answer is right, the record is not the canonical one. |
| Q18 | "Wala pa akong pera" | Ranked the hardship-options policy above the objection guide. Both are useful; the expected one was #2. |

All five out-of-scope questions are rejected by the gate. That is the behaviour that lets the agent say
"I don't have verified information on that" instead of guessing.

The same retrieval is available interactively on the **Knowledge Base** page, which also shows the
expanded query, scores, the gate decision and the citation for every hit.
