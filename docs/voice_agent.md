# Knowledge-grounded voice agent (Part 1)

Use case: **health-insurance lead qualification** for the fictional *Nivaran Health Insurance* (India, English).
The same engine also runs the Philippine and Indonesian agents (Part 3) through different profiles.

Code: `app/agent/engine.py` (conversation engine), `app/agent/rules.py` (business logic), `app/agent/crm.py`
(mock CRM), `profiles/health_lead_in.yaml` (script, rules, fields, voice), `web/assets/call.js` (web calling interface).

## Voice platform configuration

| Layer | Configuration |
|---|---|
| Calling interface | Browser web call (`/`). Mic captured via an AudioWorklet at the device rate, downsampled to 16 kHz. Energy VAD with a rolling-percentile noise floor: speech start after 60 ms above floor + 12 dB, end after 600 ms of silence, max 15 s per utterance. Half duplex (the mic is ignored while the agent speaks). |
| ASR | Groq `whisper-large-v3-turbo`, `language=en`, domain prompt with plan names, "lakh", "pre-existing disease", etc. `verbose_json` confidence is used to ignore non-speech (`no_speech_prob`, known silence hallucinations such as "Thank you."). |
| LLM | Groq `openai/gpt-oss-120b` (overflows to `openai/gpt-oss-20b` when 120b's free-tier quota is used up, instead of waiting), `reasoning_effort=low`, **strict JSON schema** output (reply, field updates, confirmed corrections, answer status, citations, action). |
| Knowledge | Hybrid search over the `health_in` collection (Part 2), top 3 per customer utterance with relevance-gate labels. |
| TTS | Edge neural voice `en-IN-NeerjaNeural`. |
| Recording | The browser records a stereo WAV (agent playback = left, microphone = right) and uploads it at the end of the call. The transcript, outcome and metrics are saved to `recordings/<call_id>/`. |

## Script and business rules vs knowledge

The profile contains only what must always be in context. The **script** is six steps: opening with the
recording disclosure, discovery, qualification, recommendation, questions/objections, close. The **business
rules** cover age limits, underwriting, budget, the ban on final quotes and sensitive data, and escalation
triggers. Everything a customer can ask about comes from the knowledge base at run time and is **not** in the
system prompt: plans, waiting periods, exclusions, co-payment, tax benefit, claims, the premium table, the form
fields and objection responses.

## Conversation flow per turn

```
customer audio ─► ASR ─► no-speech guard ─► KB search (utterance, + previous utterance if short)
   ─► LLM (system prompt + last 12 messages + turn context: captured fields, missing fields, conflicts,
          outcome preview, KNOWLEDGE passages with record IDs)
   ─► validation ─► (one corrective re-run if invalid) ─► (safe fallback if still invalid)
   ─► apply fields ─► outcome rules ─► actions (lead / escalation) ─► TTS ─► browser
```

Validation is done in code, not left to the prompt:

| Check | Behaviour |
|---|---|
| Grounding | Citations must be IDs retrieved this turn. `answered_from_kb` with no valid citation triggers a re-run; if it fails again, the reply is replaced with the profile's "I don't have verified information… a licensed advisor can call you back" line. |
| Field values | Type and range validation (e.g. age 10-110, sum insured 1-100 lakh). Invalid values are rejected and the model is told next turn. |
| Conflicting details | If a new value contradicts a captured one (age 42 → 38), it is **not** applied; the conflict is stored and the model must ask which is correct, mentioning both values. It is applied only when the customer confirms, either explicitly or by repeating the new value in a later turn. Shown amber in the UI and logged in `conflicts_detected`. |
| Incomplete details | `STILL MISSING (required)` is in every turn's context. `submit` with missing fields is refused and the agent asks for the next one. |
| Language | For the PH/ID agents: a reply with no Filipino markers, or one that drifts into English, is regenerated. |

## Qualification logic (`rules.py::evaluate_health_lead`)

Deterministic, so outcomes are auditable and cannot be hallucinated:

| Status | Rule |
|---|---|
| `do_not_contact` | customer declined contact |
| `not_eligible` | proposer under 18 |
| `incomplete` | any required field missing |
| `review` | eldest member above 75 (new cover only up to 75) |
| `nurture` | annual budget below ₹6,000 |
| `qualified_underwriting` | a pre-existing condition was declared (never told "declined") |
| `qualified` | everything captured, no declared conditions |

Plan suggestion: Family Floater for spouse/children, Essential for one adult, Senior Care for parents or a
proposer aged 61+.

## Objections, unsupported questions, escalation

- **Objections** ("too expensive", "I have employer cover", "claims get rejected", "I'll think about it") retrieve
  the playbook record. The agent paraphrases the internal guidance and cites it. The playbook forbids promising
  claim approval.
- **Unsupported or out-of-scope questions** (e.g. claim settlement ratio, treatment abroad, booking flights)
  don't pass the relevance gate. The model receives `KNOWLEDGE: nothing found` or only weak matches, sets
  `answer_status=info_unavailable` and says so. This is shown as an "info unavailable" badge.
- **Human escalation** happens when the customer asks, or on complaint, emergency, legal threat or distress (rules),
  or after 3 consecutive no-speech/technical failures. It creates an escalation ticket in the mock CRM with the last
  customer utterance and the captured details, then ends the call with a callback promise. A real deployment would
  warm-transfer over SIP.

## Business action (optional requirement)

- `submit` creates a **lead** in `data/crm/leads.jsonl` with status, captured fields, reasons and the suggested plan.
- `escalate_to_human` creates a ticket in `data/crm/escalations.jsonl`.
- Every call writes a **mock-CRM call summary** (`data/crm/call_summaries.jsonl`), viewable in Call Log → CRM records.

Example summary format, generated from structured fields with no LLM:

`Health insurance lead qualification call (India). Outcome: Qualified - advisor follow-up. Reasons: … Captured - name: …, age: …, city: …, … Suggested plan: Family Floater. Actions: lead_created.`

## Test calls

`tests/call_personas.yaml` defines three India test calls covering the required scenarios:

| Persona | Coverage | Automated checks |
|---|---|---|
| `in_cooperative` | cooperative customer | outcome `qualified`, lead created, at least one grounded answer |
| `in_objection_out_of_scope` | objection + out-of-scope questions | an `objection` record cited, at least one `info_unavailable` answer, outcome `qualified_underwriting` or `incomplete` |
| `in_conflict_human_request` | conflicting/incomplete details + human request | conflict detected, escalated to human |

Run `python -m app.agent.simulate` to produce the recordings, transcripts and `reports/call_results.md`. Calls
made on the web interface land in the same Call Log and report, so live human test calls can be added the same way.

Per-turn latency is logged for each stage (STT, retrieval, LLM, TTS) and as a total, from the moment the
customer's audio reaches the server until reply audio is ready.
