"""Signal extraction for live calls.

- Agent channel  -> deterministic compliance rules (auditable, ~1 ms): disclosure checklist, risky statements.
- Customer channel -> small LLM (gpt-oss-20b, strict JSON): cross-sell, buying intent, payment difficulty,
  callback request, topic, and a frustration score that is turned into a *trend* signal.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from app.config import FAST_MODEL
from app.llm import chat_json


@dataclass
class Signal:
    type: str
    confidence: float
    evidence: str
    t: float                      # audio time (s) of the utterance that produced it
    source: str                   # rule | llm | trend
    detail: str = ""
    topic_key: str = ""
    meta: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------------------------
# Compliance (agent channel)
# ----------------------------------------------------------------------------------------------
DISCLOSURE = re.compile(r"\b(call|conversation)\b[^.]{0,40}\brecorded\b|\brecorded for\b", re.I)
WAITING = re.compile(r"waiting period|covered after (thirty|36|\d+)", re.I)
NEGATION = re.compile(r"\bno\b|\bwithout\b|\bzero\b|\bnot any\b|\bnone\b", re.I)


def discloses_waiting_period(text: str) -> bool:
    """True if a sentence states a waiting period; 'there is no waiting period' is the opposite of a disclosure."""
    return any(WAITING.search(s) and not NEGATION.search(s) for s in re.split(r"(?<=[.!?])\s+", text))
PRICE_OR_CLOSE = re.compile(r"rupees|₹|\bpremium (is|of|would be|will be)\b|payment link|send (you )?the (payment )?link"
                            r"|proceed with (the )?payment", re.I)
RISKY = [
    ("overstated_coverage", re.compile(r"no waiting period|covered from day one|everything is covered|"
                                       r"all (diseases|illnesses) are covered|covers everything", re.I),
     "Coverage overstated - waiting periods and exclusions apply."),
    ("guaranteed_claim", re.compile(r"(definitely|surely|certainly)[^.]{0,25}(approv|settl|paid)|"
                                    r"(100|hundred) ?(%|percent)[^.]{0,15}guarantee|guaranteed[^.]{0,20}(claim|approv)|"
                                    r"claim[^.]{0,25}guaranteed", re.I),
     "Claim approval promised - claims are assessed under policy terms."),
    ("risk_free", re.compile(r"\brisk[- ]free\b|no questions asked", re.I), "Absolute claim made."),
]
CHECKLISTS = {
    "sales": ["recording_disclosure", "waiting_period_before_quote"],
    "service": ["recording_disclosure"],
}
DISCLOSURE_DEADLINE_TURNS = 2      # disclosure belongs in the opening; flagged when the 2nd agent turn starts without it
DISCLOSURE_DEADLINE_S = 30.0       # audio seconds


def _sentence(text: str, m: re.Match) -> str:
    start = max(text.rfind(".", 0, m.start()) + 1, 0)
    end = text.find(".", m.end())
    return text[start: end if end != -1 else len(text)].strip()[:160]


class ComplianceMonitor:
    def __init__(self, call_type: str):
        self.items = {name: "pending" for name in CHECKLISTS.get(call_type, CHECKLISTS["sales"])}
        self.agent_turns = 0
        self.flagged: set[str] = set()

    def on_agent(self, text: str, t: float, new_turn: bool) -> list[Signal]:
        out = []
        self.agent_turns += 1 if new_turn else 0
        if "recording_disclosure" in self.items and DISCLOSURE.search(text):
            self.items["recording_disclosure"] = "done"
        if "waiting_period_before_quote" in self.items and discloses_waiting_period(text):
            if self.items["waiting_period_before_quote"] != "missed":
                self.items["waiting_period_before_quote"] = "done"
        for key, pattern, why in RISKY:
            m = pattern.search(text)
            if m:
                out.append(Signal("risky_statement", 0.9, _sentence(text, m), t, "rule", detail=why,
                                  topic_key=f"risky:{key}"))
        if self.items.get("waiting_period_before_quote") == "pending" and PRICE_OR_CLOSE.search(text):
            self.items["waiting_period_before_quote"] = "missed"
            out.append(Signal("compliance_waiting_period", 0.85, _sentence(text, PRICE_OR_CLOSE.search(text)), t,
                              "rule", topic_key="compliance:waiting_period"))
        out += self.check_deadlines(t)
        return out

    def check_deadlines(self, t: float) -> list[Signal]:
        if self.items.get("recording_disclosure") == "pending" and "disclosure" not in self.flagged and (
                self.agent_turns >= DISCLOSURE_DEADLINE_TURNS or t >= DISCLOSURE_DEADLINE_S):
            self.flagged.add("disclosure")
            self.items["recording_disclosure"] = "missed"
            return [Signal("compliance_disclosure_missing", 0.95, "No call-recording disclosure so far.", t, "rule",
                           topic_key="compliance:recording_disclosure")]
        return []


# ----------------------------------------------------------------------------------------------
# Customer signals (LLM)
# ----------------------------------------------------------------------------------------------
LLM_TYPES = ["cross_sell", "buying_signal", "payment_difficulty", "callback_request"]
TOPICS = ["pricing", "coverage", "claims", "eligibility", "family", "payment", "complaint", "small_talk", "other"]
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["signals", "topic", "frustration"],
    "properties": {
        "signals": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["type", "confidence", "evidence", "detail"],
            "properties": {"type": {"type": "string", "enum": LLM_TYPES}, "confidence": {"type": "number"},
                           "evidence": {"type": "string"}, "detail": {"type": "string"}}}},
        "topic": {"type": "string", "enum": TOPICS},
        "frustration": {"type": "number"},
    },
}
PROMPT = """You assist a human agent on a live health-insurance call by spotting signals in what the CUSTOMER just said.
Return signals found in the LATEST customer utterance only (earlier lines are context).
Signal types:
- cross_sell: the customer mentions people or needs that their current discussion does not cover (parents or senior relatives, spouse, children, a new baby). detail = who.
- buying_signal: clear intent to proceed (asks how to buy, apply or pay; agrees to go ahead).
- payment_difficulty: says they cannot afford or cannot pay now (job loss, money tight).
- callback_request: asks to be called back later or says they cannot talk now.
Also return topic, and frustration from 0 (calm) to 1 (very angry) for the latest utterance.
Be conservative: if the utterance is short, vague, off-topic background talk, or looks like a transcription error, return no signals and frustration 0. Confidence (0-1) = how explicit the evidence is. evidence = a short quote."""

GROUPS = [("parents", r"parent|mother|father|\bmom\b|\bdad\b|senior|in-law|grand"),
          ("children", r"child|kid|son|daughter|baby|pregnan"), ("spouse", r"wife|husband|spouse|partner")]


def topic_group(detail: str, evidence: str) -> str:
    text = f"{detail} {evidence}".lower()
    return next((g for g, p in GROUPS if re.search(p, text)), "other")


def extract_customer_signals(window: list[dict], latest: str, t: float) -> tuple[list[Signal], dict]:
    context = "\n".join(f"{w['speaker'].upper()}: {w['text']}" for w in window[-8:])
    messages = [{"role": "system", "content": PROMPT},
                {"role": "user", "content": f"Recent transcript:\n{context}\n\nLATEST CUSTOMER UTTERANCE: {latest}"}]
    t0 = time.perf_counter()
    data, info = chat_json(messages, SCHEMA, FAST_MODEL, name="call_signals", max_tokens=700, temperature=0.0)
    signals = []
    for s in data["signals"]:
        key = s["type"] if s["type"] != "cross_sell" else f"cross_sell:{topic_group(s['detail'], s['evidence'])}"
        signals.append(Signal(s["type"], float(s["confidence"]), s["evidence"][:160], t, "llm", detail=s["detail"][:80],
                              topic_key=key))
    return signals, {"topic": data["topic"], "frustration": float(data["frustration"]),
                     "llm_ms": round((time.perf_counter() - t0) * 1000), "tokens": info["tokens"]}


class FrustrationTrend:
    """Turns per-utterance frustration scores into a 'rising frustration' signal (trend, not one-off words)."""

    def __init__(self):
        self.scores: list[float] = []

    def update(self, score: float, evidence: str, t: float) -> list[Signal]:
        prev = self.scores[-1] if self.scores else 0.0
        self.scores.append(score)
        rising = score >= 0.6 and prev >= 0.4 and score >= prev
        if score >= 0.85 or rising:
            return [Signal("frustration", round(score, 2), evidence[:160], t, "trend",
                           detail=f"frustration {prev:.1f} -> {score:.1f}", topic_key="frustration")]
        return []
