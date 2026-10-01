"""Knowledge-grounded conversation engine shared by all voice agents (India EN, Philippines, Indonesia).

One customer turn:
  ASR text -> no-speech guard -> KB retrieval (hybrid, gated) -> LLM (strict JSON) -> code validation
  (citations, field ranges, conflicts, verification, submit preconditions, language) -> one corrective
  re-run if needed -> safe fallback if still invalid -> business actions (CRM) -> TTS.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from uuid import uuid4

from app.agent import crm
from app.agent.rules import (STATUS_LABELS, account_context, coerce, evaluate_health_lead, evaluate_payment,
                             local_now, salutation, same_value)
from app.config import AGENT_MODEL, FAST_MODEL, RECORDINGS_DIR
from app.kb.store import KnowledgeBase
from app.llm import LLMError, chat_json
from app.tts import synthesize

TURN_MODELS = (AGENT_MODEL, FAST_MODEL)  # FAST_MODEL's separate free-tier quota absorbs bursts instead of a wait
HISTORY_MESSAGES = 12         # recent messages sent to the LLM; captured fields carry older state
CONFLICT_CHECKED_TYPES = ("integer", "number")
CONFLICT_CHECKED_FIELDS = ("full_name", "city")
ESCALATION_STATUSES = ("ptp_beyond_limit", "hardship", "retention_escalation")
WHISPER_SILENCE_PHRASES = re.compile(
    r"^(thank you\.?|thanks for watching\.?|terima kasih\.?|salamat\.?|you\.?|bye\.?|\.+)$", re.I)
TAGALOG_MARKERS = set("po opo ang ng mga sa na ko mo ba naman kayo ninyo natin lang ito iyan yung salamat "
                      "pasensya sige talaga hindi oo".split())
ENGLISH_FUNCTION_WORDS = set("the is are you your will can please and of to for this that with have has we our "
                             "would could should it be".split())


def _schema(profile: dict) -> dict:
    props = {}
    for f in profile["fields"]:
        t = {"integer": "integer", "number": "number", "boolean": "boolean"}.get(f["type"], "string")
        prop = {"type": [t, "null"]}
        if f.get("enum"):
            prop["enum"] = f["enum"] + [None]
        props[f["name"]] = prop
    names = list(props)
    return {
        "type": "object", "additionalProperties": False,
        "required": ["reply", "field_updates", "confirmed_corrections", "answer_status", "citations", "action"],
        "properties": {
            "reply": {"type": "string"},
            "field_updates": {"type": "object", "additionalProperties": False, "required": names, "properties": props},
            "confirmed_corrections": {"type": "array", "items": {"type": "string", "enum": names}},
            "answer_status": {"type": "string", "enum": ["answered_from_kb", "info_unavailable", "no_question"]},
            "citations": {"type": "array", "items": {"type": "string"}},
            "action": {"type": "string", "enum": ["continue", "escalate_to_human", "submit", "end_call"]},
        },
    }


def _system_prompt(p: dict) -> str:
    fields = "\n".join(
        f"- {f['name']} ({'required' if f.get('required') else 'optional'}, {f['type']}"
        f"{', one of ' + '/'.join(f['enum']) if f.get('enum') else ''}): {f['description']}" for f in p["fields"])
    return f"""You are {p['agent_name']}, the voice agent of {p['company']} on a live phone call. Use case: {p['title']} ({p['market']}).

# Style
{p['style'].strip()}

# Call script
{p['script'].strip()}

# Business rules
{p['rules'].strip()}

# Details to capture
{fields}

# Grounding (critical)
- Facts about products, coverage, prices, policies, processes, FAQs and objection responses must come ONLY from the KNOWLEDGE passages in the latest system message. Put the record_id of every passage you used in "citations".
- A "weak match" passage may be off-topic: use it only if it directly answers the customer.
- If the passages do not answer the customer's question, set answer_status to "info_unavailable", say plainly that you do not have verified information on that, and offer a follow-up by a human colleague. Never guess, never use outside knowledge, never invent numbers, dates or terms.
- Internal guidance passages tell you how to respond: paraphrase them naturally. Never mention documents, playbooks or record IDs.
- Account facts may only come from the ACCOUNT block.
- answer_status: "answered_from_kb" if your reply states knowledge-base facts, "info_unavailable" if you could not answer a question, otherwise "no_question".

# Capturing details
- field_updates: only values the customer stated in their latest message; null for everything else. Never guess.
- If a value contradicts one already captured, do not overwrite it: ask which is correct. When the customer confirms the new value, send it in field_updates and list the field in confirmed_corrections.

# Actions
- "continue": normal turn.
- "escalate_to_human": when the rules require it or the customer asks for a person. Acknowledge, say a human colleague will call back shortly, and say goodbye. The call ends after this reply.
- "submit": when nothing required is missing and the customer has answered your closing question. Briefly confirm next steps and say goodbye. The call ends after this reply.
- "end_call": the customer wants to end or the conversation is complete. Say a polite goodbye.

"reply" is spoken aloud by a text-to-speech voice: plain sentences only, no lists, markdown or IDs."""


class CallSession:
    def __init__(self, profile: dict, source: str = "web"):
        self.id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:4]
        self.profile = profile
        self.source = source
        self.kb = KnowledgeBase.get(profile["kb_collection"])
        self.specs = {f["name"]: f for f in profile["fields"]}
        self.required = [f["name"] for f in profile["fields"] if f.get("required")]
        self.fields: dict = {name: None for name in self.specs}
        self.conflicts: dict = {}
        self.conflict_log: list[dict] = []
        self.notes: list[str] = []
        self.history: list[dict] = []
        self.turns: list[dict] = []
        self.actions: list[dict] = []
        self.started = datetime.now(timezone.utc)
        self.now = local_now(profile)
        self.account = account_context(profile, self.now)
        self.verified = None if self.account else True
        self.verify_attempts = 0
        self.consecutive_failures = 0
        self.ended = False
        self.end_reason = None
        self.flags: list[str] = []
        self.system_prompt = _system_prompt(profile)
        self.schema = _schema(profile)
        hours = profile["outcome_params"].get("permitted_hours")
        if hours and not (hours[0] <= self.now.hour < hours[1]
                          and self.now.weekday() in profile["outcome_params"].get("permitted_weekdays", range(7))):
            self.flags.append(f"outside permitted contact hours ({hours[0]:02d}:00-{hours[1]:02d}:00, Mon-Sat local)")

    # ------------------------------------------------------------------ public API
    async def start(self) -> dict:
        p = self.profile
        text = p["greeting"].format(salutation=salutation(p, self.now),
                                    customer_name=self.account["customer_name"] if self.account else "")
        self.history.append({"role": "assistant", "content": text})
        audio, tts_ms = await self._speak(text)
        turn = self._log_agent(text, kind="greeting", timings={"tts_ms": tts_ms})
        return {"reply": text, "audio": audio, "turn": turn, "state": self.state(), "end_call": False}

    async def handle(self, text: str, asr: dict | None = None) -> dict:
        """Process one customer utterance; returns reply text, mp3 audio, turn log and state."""
        t_start = time.perf_counter()
        timings = {"stt_ms": asr.get("ms") if asr else None}
        text = (text or "").strip()
        if self._is_no_speech(text, asr):
            return await self._fallback_turn("no_speech", timings, asr, t_start)
        self.consecutive_failures = 0
        self._log_customer(text, asr)
        self.history.append({"role": "user", "content": text})

        # 1) Retrieval: short follow-ups ("and for my parents?") are expanded with the previous utterance.
        t0 = time.perf_counter()
        prev = [t["text"] for t in self.turns if t["role"] == "customer"][:-1]
        query = text if len(text.split()) >= 5 or not prev else f"{prev[-1]} {text}"
        hits = await asyncio.to_thread(self.kb.search, query, 3)
        timings["retrieval_ms"] = round((time.perf_counter() - t0) * 1000)

        # 2) LLM + validation (+ one corrective re-run)
        llm_ms, tokens, reruns = 0, 0, 0
        messages = self._messages(hits, query)
        try:
            data, info = await asyncio.to_thread(chat_json, messages, self.schema, TURN_MODELS, "agent_turn")
            llm_ms += info["ms"]; tokens += info["tokens"]
            issues, rerun_notes = self._validate(data, hits, text)
            if issues or rerun_notes:
                reruns = 1
                fix = rerun_notes + [f"Problem with your previous draft: {i}" for i in issues]
                messages = self._messages(hits, query) + [
                    {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
                    {"role": "system", "content": "Update: " + " ".join(fix) + " Produce the corrected JSON now."}]
                data, info = await asyncio.to_thread(chat_json, messages, self.schema, TURN_MODELS, "agent_turn")
                llm_ms += info["ms"]; tokens += info["tokens"]
                issues, _ = self._validate(data, hits, text, second_pass=True)
                if issues:
                    self._apply_safe_fallback(data, issues)
        except LLMError as exc:
            timings.update(llm_ms=llm_ms)
            return await self._fallback_turn("technical", timings, asr, t_start, error=str(exc))
        timings.update(llm_ms=llm_ms)

        # 3) Apply state, business actions
        self._apply_updates(data)
        outcome = self.outcome()
        action = data["action"]
        if action == "escalate_to_human" or (action in ("submit", "end_call") and outcome["status"] in ESCALATION_STATUSES):
            self._escalate(data["reply"], outcome)
            action = "escalate_to_human"
        elif action == "submit":
            self._submit(outcome)
        if action in ("escalate_to_human", "submit", "end_call"):
            self.ended, self.end_reason = True, action
        data["action"] = action

        reply = data["reply"].strip()
        self.history.append({"role": "assistant", "content": reply})
        audio, timings["tts_ms"] = await self._speak(reply)
        timings["total_ms"] = round((time.perf_counter() - t_start) * 1000) + (timings["stt_ms"] or 0)
        turn = self._log_agent(reply, kind="answer", timings=timings, data=data, hits=hits, query=query,
                               tokens=tokens, reruns=reruns, outcome=outcome)
        return {"reply": reply, "audio": audio, "turn": turn, "state": self.state(), "end_call": self.ended}

    def state(self) -> dict:
        return {
            "call_id": self.id, "profile_id": self.profile["id"], "fields": self.fields,
            "missing": [f for f in self.required if self.fields.get(f) is None],
            "conflicts": self.conflicts, "verified": self.verified, "verify_attempts": self.verify_attempts,
            "outcome": self.outcome(), "actions": self.actions, "ended": self.ended, "end_reason": self.end_reason,
            "flags": self.flags,
        }

    def outcome(self) -> dict:
        p = self.profile
        if p["outcome"] == "health_lead":
            out = evaluate_health_lead(self.fields, p["outcome_params"], self.required)
        else:
            out = evaluate_payment(self.fields, self.account, self.verified, self.verify_attempts, self.required,
                                   self.now.date())
        out["label"] = STATUS_LABELS.get(out["status"], out["status"])
        return out

    def finalize(self, reason: str | None = None) -> dict:
        """Close the call, write the mock-CRM call summary and the transcript files."""
        if not self.ended:
            self.ended, self.end_reason = True, reason or "customer_hung_up"
        outcome = self.outcome()
        summary = crm.append("call_summaries", {
            "call_id": self.id, "profile": self.profile["id"], "outcome": outcome["status"],
            "summary": self.crm_summary(outcome), "fields": self._public_fields(), "actions": self.actions})
        result = {
            "call_id": self.id, "profile_id": self.profile["id"], "profile_title": self.profile["title"],
            "market": self.profile["market"], "source": self.source,
            "started_at": self.started.isoformat(timespec="seconds"),
            "duration_s": round((datetime.now(timezone.utc) - self.started).total_seconds(), 1),
            "end_reason": self.end_reason, "outcome": outcome, "fields": self.fields, "actions": self.actions,
            "crm_summary_id": summary["id"], "crm_summary": summary["summary"], "flags": self.flags,
            "conflicts_detected": self.conflict_log, "verified": self.verified,
            "metrics": self.metrics(), "turns": self.turns,
        }
        folder = RECORDINGS_DIR / self.id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "transcript.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        (folder / "transcript.md").write_text(self.markdown(result), encoding="utf-8")
        return result

    # ------------------------------------------------------------------ prompt building
    def _messages(self, hits: list[dict], query: str) -> list[dict]:
        captured = {k: v for k, v in self.fields.items() if v is not None}
        missing = [f for f in self.required if self.fields.get(f) is None]
        outcome = self.outcome()
        lines = [f"# Turn context\nLocal date/time: {self.now:%A %Y-%m-%d %H:%M}",
                 f"Captured: {json.dumps(captured, ensure_ascii=False)}",
                 f"STILL MISSING (required): {', '.join(missing) or 'nothing'}",
                 f"Outcome preview: {outcome['status']} - {'; '.join(outcome.get('reasons', []))}"]
        if outcome.get("plan_recommendation"):
            lines.append(f"Suggested plan(s) from rules: {', '.join(outcome['plan_recommendation'])}")
        if self.conflicts:
            lines.append("CONFLICTS awaiting confirmation: " + "; ".join(
                f"{k}: captured {v['old']}, customer later said {v['new']}" for k, v in self.conflicts.items()))
        if self.account is not None:
            if self.fields.get("is_policyholder") is False:
                lines.append("ACCOUNT: the person is NOT the customer. Do not reveal anything about the account.")
            elif self.verified:
                lines.append("ACCOUNT (identity verified):\n" + json.dumps(self.account, ensure_ascii=False))
            else:
                lines.append(f"ACCOUNT: locked - identity not verified (attempts {self.verify_attempts}/2). "
                             f"Customer on record: {self.account['customer_name']}. Do not state any account details.")
        if self.notes:
            lines.append("NOTES: " + " ".join(self.notes))
            self.notes = []
        if hits:
            kb = [f'KNOWLEDGE retrieved for: "{query}"']
            for h in hits:
                tag = "relevant" if h["relevant"] else "weak match"
                kb.append(f"[{h['record_id']} | {tag} | {h['category']} | {h['audience']}] {h['title']}: "
                          f"{h['content'][:700]}")
            lines.append("\n".join(kb))
        else:
            lines.append("KNOWLEDGE: nothing found.")
        recent = self.history[-HISTORY_MESSAGES:]
        return [{"role": "system", "content": self.system_prompt}, *recent,
                {"role": "system", "content": "\n".join(lines)}]

    # ------------------------------------------------------------------ validation
    def _validate(self, data: dict, hits: list[dict], customer_text: str, second_pass: bool = False):
        issues, rerun_notes = [], []
        ids = {h["record_id"] for h in hits}
        data["citations"] = [c for c in dict.fromkeys(data.get("citations") or []) if c in ids]
        if data["answer_status"] == "answered_from_kb" and not data["citations"]:
            issues.append("answer_status is answered_from_kb but no record_id from KNOWLEDGE was cited. If the "
                          "passages do not answer the question, use info_unavailable and say so.")
        if not data["reply"].strip():
            issues.append("reply is empty.")

        updates = {k: v for k, v in (data.get("field_updates") or {}).items() if v is not None and k in self.specs}
        confirmed = set(data.get("confirmed_corrections") or [])
        for name, raw in list(updates.items()):
            ok, val = coerce(self.specs[name], raw)
            if not ok:
                self.notes.append(f"Value '{raw}' for {name} was rejected ({val}); ask again if needed.")
                updates.pop(name)
                continue
            updates[name] = val
            if name in self.conflicts:  # a conflict is pending for this field
                c = self.conflicts[name]
                if name in confirmed or (same_value(val, c["new"]) and c["turn"] < len(self.turns) - 1):
                    continue  # confirmed (explicitly, or by repeating the new value in a later turn)
                if same_value(val, c["old"]):
                    self.conflicts.pop(name)  # customer re-affirmed the original value
                updates.pop(name)
                continue
            old, spec = self.fields.get(name), self.specs[name]
            checked = spec["type"] in CONFLICT_CHECKED_TYPES or name in CONFLICT_CHECKED_FIELDS
            if checked and old is not None and not same_value(old, val) and name not in confirmed:
                self.conflicts[name] = {"old": old, "new": val, "turn": len(self.turns) - 1}
                self.conflict_log.append({"field": name, "old": old, "new": val, "t": self._offset()})
                updates.pop(name)
                if not (str(old) in data["reply"] and str(val) in data["reply"]):
                    issues.append(f"The customer now gave {name}={val}, but {old} was captured earlier. Do not "
                                  f"change it yet; ask which value is correct, mentioning both.")
        data["field_updates"] = updates

        # Identity verification is decided in code, then the reply is regenerated with the result.
        if self.account is not None and "verification_last4" in updates:
            digits = updates.pop("verification_last4")
            if not self.verified and not second_pass:
                self.verify_attempts += 1
                if digits == str(self.profile["account"]["verify_last4"]):
                    self.verified = True
                    self.fields["verification_last4"] = digits
                    rerun_notes.append("Identity verification SUCCEEDED; the ACCOUNT block is now unlocked. "
                                       "Continue with the reminder using those facts.")
                elif self.verify_attempts >= 2:
                    rerun_notes.append("Verification FAILED twice. Do not disclose anything; advise visiting a branch "
                                       "and end the call politely (action end_call).")
                else:
                    rerun_notes.append("Verification FAILED: the digits do not match. Politely ask the customer to "
                                       "check and repeat the last 4 digits. Do not reveal the correct digits.")

        if data["action"] == "submit":
            preview = dict(self.fields, **updates)
            missing = [f for f in self.required if preview.get(f) is None]
            if missing:
                issues.append(f"cannot submit yet, still missing: {', '.join(missing)}. Ask for the next one.")

        lang = self.profile["asr"]["language"]
        words = re.findall(r"[a-zA-Z']+", data["reply"].lower())
        if lang == "tl" and words and not (set(words) & TAGALOG_MARKERS):
            issues.append("reply has no Filipino at all; keep the Taglish register with 'po' as the style requires.")
        if lang == "id" and words and sum(w in ENGLISH_FUNCTION_WORDS for w in words) / len(words) > 0.2:
            issues.append("reply switched to English; answer in Bahasa Indonesia as the style requires.")
        return issues, rerun_notes

    def _apply_safe_fallback(self, data: dict, issues: list[str]) -> None:
        fb = self.profile["fallbacks"]
        if any("no record_id" in i for i in issues):
            data.update(reply=fb["unavailable"], answer_status="info_unavailable", citations=[])
        if any("cannot submit" in i for i in issues):
            data["action"] = "continue"
        conflict = next(iter(self.conflicts.items()), None)
        if conflict and any("captured earlier" in i for i in issues):
            name, v = conflict
            data["reply"] = fb["confirm_conflict"].format(old=v["old"], new=v["new"], field=self.specs[name]["label"])
            data["action"] = "continue"
        if not data["reply"].strip():
            data["reply"] = fb["technical"]
        self.flags.append(f"fallback used: {'; '.join(i[:60] for i in issues)}")

    def _apply_updates(self, data: dict) -> None:
        for name, val in data["field_updates"].items():
            if name in self.conflicts:
                self.conflicts.pop(name)
            self.fields[name] = val
        for name in list(self.conflicts):
            if name in (data.get("confirmed_corrections") or []):
                self.fields[name] = self.conflicts.pop(name)["new"]

    # ------------------------------------------------------------------ business actions
    def _public_fields(self) -> dict:
        return {k: v for k, v in self.fields.items() if v is not None and k != "verification_last4"}

    def crm_summary(self, outcome: dict) -> str:
        p, f = self.profile, self._public_fields()
        show = lambda v: {True: "yes", False: "no"}.get(v, v) if isinstance(v, bool) else v
        details = ", ".join(f"{self.specs[k]['label']}: {show(v)}" for k, v in f.items())
        plan = f" Suggested plan: {', '.join(outcome['plan_recommendation'])}." if outcome.get("plan_recommendation") else ""
        acts = "; ".join(a["type"] for a in self.actions) or "none"
        return (f"{p['title']} call ({p['market']}). Outcome: {outcome['label']}. "
                f"{('Reasons: ' + '; '.join(outcome.get('reasons', [])) + '.') if outcome.get('reasons') else ''}"
                f" Captured - {details or 'nothing'}.{plan} Actions: {acts}.").strip()

    def _submit(self, outcome: dict) -> None:
        table = "leads" if self.profile["outcome"] == "health_lead" else "payment_promises"
        rec = crm.append(table, {"call_id": self.id, "profile": self.profile["id"], "status": outcome["status"],
                                 "fields": self._public_fields(), "reasons": outcome.get("reasons", []),
                                 "plan_recommendation": outcome.get("plan_recommendation")})
        self.actions.append({"type": "lead_created" if table == "leads" else "payment_promise_logged",
                             "record_id": rec["id"], "status": outcome["status"]})

    def _escalate(self, reply: str, outcome: dict) -> None:
        last = next((t["text"] for t in reversed(self.turns) if t["role"] == "customer"), "")
        rec = crm.append("escalations", {"call_id": self.id, "profile": self.profile["id"],
                                         "reason": outcome["label"] if outcome["status"] in ESCALATION_STATUSES
                                         else "customer request / rule-based escalation",
                                         "last_customer_utterance": last, "fields": self._public_fields(),
                                         "priority": "high" if outcome["status"] in ESCALATION_STATUSES else "normal"})
        self.actions.append({"type": "escalated_to_human", "record_id": rec["id"]})

    # ------------------------------------------------------------------ fallbacks & logging
    async def _speak(self, text: str) -> tuple[bytes, int | None]:
        """TTS; a voice-service outage must not kill the call (the transcript still shows the reply)."""
        p = self.profile
        try:
            return await synthesize(text, p["tts"]["voice"], p["tts"].get("rate", "+0%"))
        except Exception as exc:
            self.flags.append(f"TTS failed: {type(exc).__name__}")
            return b"", None

    def _is_no_speech(self, text: str, asr: dict | None) -> bool:
        if not text:
            return True
        if asr and asr.get("no_speech_prob") is not None:
            if asr["no_speech_prob"] > 0.6 and (asr.get("avg_logprob") or 0) < -0.8:
                return True
            if asr["no_speech_prob"] > 0.3 and WHISPER_SILENCE_PHRASES.match(text):
                return True
        return False

    async def _fallback_turn(self, kind: str, timings: dict, asr: dict | None, t_start: float,
                             error: str = "") -> dict:
        fb = self.profile["fallbacks"]
        self.consecutive_failures += 1
        if kind == "no_speech" and asr is not None:
            self._log_customer(asr.get("text") or "", asr, ignored=True)
        reply = fb[kind]
        if self.consecutive_failures >= 3:
            reply = fb["repeated_failure"] if kind == "no_speech" else fb["ended_technical"]
            self._escalate(reply, {"status": "technical", "label": "repeated audio/technical failure"})
            self.ended, self.end_reason = True, "repeated_failure"
        self.history.append({"role": "assistant", "content": reply})
        audio, tts_ms = await self._speak(reply)
        timings.update(tts_ms=tts_ms, total_ms=round((time.perf_counter() - t_start) * 1000) + (timings.get("stt_ms") or 0))
        turn = self._log_agent(reply, kind=f"fallback_{kind}", timings=timings, error=error)
        return {"reply": reply, "audio": audio, "turn": turn, "state": self.state(), "end_call": self.ended}

    def _offset(self) -> float:
        return round((datetime.now(timezone.utc) - self.started).total_seconds(), 2)

    def _log_customer(self, text: str, asr: dict | None, ignored: bool = False) -> None:
        self.turns.append({"role": "customer", "text": text, "t": self._offset(), "ignored": ignored,
                           "asr": {k: asr.get(k) for k in ("avg_logprob", "no_speech_prob", "ms", "model")} if asr else None})

    def _log_agent(self, text: str, kind: str, timings: dict, data: dict | None = None, hits: list | None = None,
                   query: str | None = None, tokens: int = 0, reruns: int = 0, outcome: dict | None = None,
                   error: str = "") -> dict:
        cited = set((data or {}).get("citations", []))
        turn = {
            "role": "agent", "text": text, "t": self._offset(), "kind": kind, "timings": timings,
            "answer_status": (data or {}).get("answer_status"), "action": (data or {}).get("action"),
            "citations": [{"record_id": h["record_id"], "title": h["title"], "citation": h["citation"],
                           "category": h["category"]} for h in (hits or []) if h["record_id"] in cited],
            "retrieved": [{"record_id": h["record_id"], "title": h["title"], "category": h["category"],
                           "relevant": h["relevant"], "scores": h["scores"]} for h in (hits or [])],
            "query": query, "field_updates": (data or {}).get("field_updates", {}), "tokens": tokens,
            "reruns": reruns, "outcome": (outcome or {}).get("status"), "error": error or None,
        }
        self.turns.append(turn)
        return turn

    def metrics(self) -> dict:
        agent = [t for t in self.turns if t["role"] == "agent" and t["kind"] != "greeting"]

        def pct(values, q):
            values = sorted(v for v in values if v is not None)
            return values[min(len(values) - 1, int(round(q * (len(values) - 1))))] if values else None
        out = {"agent_turns": len(agent),
               "grounded_answers": sum(1 for t in agent if t["answer_status"] == "answered_from_kb"),
               "unavailable_answers": sum(1 for t in agent if t["answer_status"] == "info_unavailable"),
               "fallback_turns": sum(1 for t in agent if t["kind"].startswith("fallback")),
               "corrective_reruns": sum(t["reruns"] for t in agent),
               "tokens": sum(t["tokens"] for t in agent)}
        for key in ("stt_ms", "retrieval_ms", "llm_ms", "tts_ms", "total_ms"):
            vals = [t["timings"].get(key) for t in agent]
            out[f"p50_{key}"], out[f"p95_{key}"] = pct(vals, 0.5), pct(vals, 0.95)
        return out

    def markdown(self, r: dict) -> str:
        lines = [f"# Call {r['call_id']} - {r['profile_title']} ({r['market']})", "",
                 f"- Source: {r['source']} | Started: {r['started_at']} | Duration: {r['duration_s']} s",
                 f"- Outcome: **{r['outcome']['label']}** ({'; '.join(r['outcome'].get('reasons', []))})",
                 f"- End reason: {r['end_reason']} | Actions: {', '.join(a['type'] for a in r['actions']) or 'none'}",
                 f"- CRM summary ({r['crm_summary_id']}): {r['crm_summary']}"]
        if r["flags"]:
            lines.append(f"- Flags: {'; '.join(r['flags'])}")
        m = r["metrics"]
        lines += [f"- Latency p50 (ms): STT {m['p50_stt_ms']}, retrieval {m['p50_retrieval_ms']}, LLM {m['p50_llm_ms']}, "
                  f"TTS {m['p50_tts_ms']}, total {m['p50_total_ms']}", "", "## Transcript", ""]
        for t in r["turns"]:
            who = "Agent" if t["role"] == "agent" else "Customer"
            extra = ""
            if t["role"] == "agent":
                if t.get("citations"):
                    extra += " " + " ".join(f"`[{c['record_id']}]`" for c in t["citations"])
                if t.get("answer_status") == "info_unavailable":
                    extra += " *(info unavailable)*"
                if t.get("action") and t["action"] != "continue":
                    extra += f" *(action: {t['action']})*"
                if t["kind"].startswith("fallback"):
                    extra += f" *({t['kind']})*"
            elif t.get("ignored"):
                extra = " *(ignored: no speech)*"
            lines.append(f"**[{t['t']:>6.1f}s] {who}:** {t['text']}{extra}  ")
        cites = {c["record_id"]: c["citation"] for t in r["turns"] for c in t.get("citations", [])}
        if cites:
            lines += ["", "## Knowledge-base citations", ""] + [f"- `{k}`: {v}" for k, v in cites.items()]
        return "\n".join(lines) + "\n"
