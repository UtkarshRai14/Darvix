"""Nudge controller: turns signals into a small number of useful, timely recommendations.

Controls (in order): confidence threshold -> duplicate suppression by topic key -> per-type cooldown ->
global spacing for non-critical nudges -> cap on simultaneously active nudges. Active nudges expire, and
are marked resolved when the agent addresses them; an unaddressed cross-sell is repeated once as a
higher-priority "missed opportunity" (repetition rule).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from itertools import count

from app.live.signals import Signal

CONFIDENCE_THRESHOLD = 0.65
GLOBAL_SPACING_S = 8.0     # min audio seconds between two non-critical nudges
MAX_ACTIVE = 3
FOLLOWUP_AGENT_TURNS = 2       # agent turns without addressing a cross-sell -> missed opportunity

OFFERS = {"parents": "Nivaran Senior Care for parents aged 60-75",
          "children": "Family Floater: one sum insured for spouse and children",
          "spouse": "Family Floater: one sum insured for the couple",
          "other": "a plan that covers the additional need"}

# type -> priority (1 = critical), category, title, text template, ttl (s), cooldown (s), resolve pattern
PLAYBOOK = {
    "compliance_disclosure_missing": (1, "compliance", "Recording disclosure missing",
                                      "State the call-recording disclosure now, before continuing.", 60, 0,
                                      r"\brecorded\b"),
    "compliance_waiting_period": (1, "compliance", "Disclose waiting periods",
                                  "Before quoting or closing, disclose the 30-day initial and 36-month pre-existing "
                                  "disease waiting periods.", 60, 0, r"(?<!no )waiting period (of|is|for|applies)"),
    "risky_statement": (1, "compliance", "Risky statement - correct it",
                        "Correct this: \"{evidence}\". {detail}", 60, 0,
                        r"subject to|policy terms|exclusions apply|let me correct|to clarify|i misspoke|"
                        r"(?<!no )waiting period (of|is|applies)"),
    "frustration": (2, "sentiment", "Rising frustration",
                    "Acknowledge the concern and apologise before continuing with process questions.", 45, 60,
                    r"\bsorry\b|apologi|understand (your|how|that)|i hear you|frustrat"),
    "payment_difficulty": (2, "retention", "Payment difficulty",
                           "Offer approved support: switch to the monthly premium option or a callback from the "
                           "renewals team. Don't push for payment now.", 45, 90, r"monthly|callback|call you back"),
    "cross_sell": (3, "opportunity", "Cross-sell opportunity",
                   "Customer mentioned {detail}. Suggest {offer}.", 45, 30, None),
    "missed_opportunity": (2, "opportunity", "Missed opportunity",
                           "Still unaddressed: {detail}. Bring up {offer} before closing.", 45, 0, None),
    "buying_signal": (3, "opportunity", "Buying intent",
                      "Customer is ready: confirm the plan and next step (proposal form) - after required disclosures.",
                      40, 60, r"proposal|form|link|next step"),
    "callback_request": (3, "follow-up", "Callback requested",
                         "Confirm a specific callback day and time and log it before ending the call.", 40, 60,
                         r"\b(at|by) \d|evening|morning|tomorrow|o'clock|pm|am\b"),
}
CROSS_SELL_RESOLVE = {"parents": r"parent|senior|mother|father", "children": r"child|kid|son|daughter|family floater",
                      "spouse": r"wife|husband|spouse|family floater", "other": r"also cover|add"}


@dataclass
class Nudge:
    id: str
    type: str
    priority: int
    category: str
    title: str
    text: str
    evidence: str
    confidence: float
    source: str
    t: float
    expires_t: float
    topic_key: str
    status: str = "active"           # active | resolved | expired
    timings: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class NudgeController:
    def __init__(self, threshold: float = CONFIDENCE_THRESHOLD):
        self.threshold = threshold
        self.ids = count(1)
        self.nudges: list[Nudge] = []
        self.last_by_type: dict[str, float] = {}
        self.last_noncritical = -1e9
        self.seen_keys: set[str] = set()
        self.pending_cross_sell: dict[str, dict] = {}   # topic group -> {nudge, turns, escalated}
        self.suppressed: list[dict] = []

    def active(self) -> list[Nudge]:
        return [n for n in self.nudges if n.status == "active"]

    def _suppress(self, s: Signal, reason: str) -> None:
        self.suppressed.append({"type": s.type, "reason": reason, "t": s.t, "confidence": s.confidence,
                                "evidence": s.evidence, "topic_key": s.topic_key})

    def consider(self, signals: list[Signal], now_t: float) -> list[Nudge]:
        out = []
        for s in sorted(signals, key=lambda x: PLAYBOOK[x.type][0]):
            priority, category, title, template, ttl, cooldown, _ = PLAYBOOK[s.type]
            if s.confidence < self.threshold:
                self._suppress(s, f"low confidence ({s.confidence:.2f} < {self.threshold})"); continue
            if s.topic_key in self.seen_keys:
                self._suppress(s, "duplicate topic"); continue
            if cooldown and now_t - self.last_by_type.get(s.type, -1e9) < cooldown:
                self._suppress(s, f"cooldown ({cooldown}s)"); continue
            if priority > 1 and now_t - self.last_noncritical < GLOBAL_SPACING_S:
                self._suppress(s, f"spacing ({GLOBAL_SPACING_S}s between non-critical nudges)"); continue
            active = self.active()
            if len(active) >= MAX_ACTIVE:
                # Replace the least important (then oldest) nudge; critical compliance nudges are never dropped.
                worst = max(active, key=lambda n: (n.priority, -n.t))
                if worst.priority <= priority and priority != 1:
                    self._suppress(s, "too many active nudges"); continue
                worst.status = "expired"
                out.append(worst)
            group = s.topic_key.split(":", 1)[1] if s.type in ("cross_sell", "missed_opportunity") else "other"
            n = Nudge(id=f"n{next(self.ids)}", type=s.type, priority=priority, category=category, title=title,
                      text=template.format(evidence=s.evidence, detail=s.detail or s.evidence, offer=OFFERS[group]),
                      evidence=s.evidence, confidence=round(s.confidence, 2), source=s.source, t=s.t,
                      expires_t=now_t + ttl, topic_key=s.topic_key)
            self.nudges.append(n)
            self.seen_keys.add(s.topic_key)
            self.last_by_type[s.type] = now_t
            if priority > 1:
                self.last_noncritical = now_t
            if s.type == "cross_sell":
                self.pending_cross_sell[group] = {"nudge": n, "turns": 0, "escalated": False,
                                                  "detail": s.detail or s.evidence}
            out.append(n)
        return out

    def on_agent_text(self, text: str, now_t: float, new_turn: bool) -> list[Nudge]:
        """Resolve nudges the agent addressed; repeat an unaddressed cross-sell once (missed opportunity)."""
        changed = []
        for n in self.active():
            pattern = PLAYBOOK[n.type][6]
            if pattern and re.search(pattern, text, re.I):
                n.status = "resolved"; changed.append(n)
        for group, p in self.pending_cross_sell.items():
            if p["escalated"]:
                continue
            if re.search(CROSS_SELL_RESOLVE[group], text, re.I):
                if p["nudge"].status == "active":
                    p["nudge"].status = "resolved"; changed.append(p["nudge"])
                p["escalated"] = True
                continue
            p["turns"] += 1 if new_turn else 0
            if p["turns"] >= FOLLOWUP_AGENT_TURNS:
                p["escalated"] = True
                if p["nudge"].status == "active":
                    p["nudge"].status = "expired"; changed.append(p["nudge"])
                sig = Signal("missed_opportunity", 0.9, p["nudge"].evidence, now_t, "rule", detail=p["detail"],
                             topic_key=f"missed:{group}")
                self.last_noncritical = -1e9  # repetition rule overrides spacing once
                changed += self.consider([sig], now_t)
        return changed

    def tick(self, now_t: float) -> list[Nudge]:
        expired = []
        for n in self.active():
            if now_t >= n.expires_t:
                n.status = "expired"; expired.append(n)
        return expired
