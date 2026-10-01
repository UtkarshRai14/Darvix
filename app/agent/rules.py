"""Deterministic business logic: profiles, field validation, account context and call outcomes.

The LLM collects details and talks; qualification, eligibility, deadlines and penalties are decided here in
code so they are auditable and cannot be hallucinated.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import yaml

from app.config import PROFILES_DIR


def load_profiles() -> dict[str, dict]:
    profiles = {}
    for path in sorted(PROFILES_DIR.glob("*.yaml")):
        p = yaml.safe_load(path.read_text(encoding="utf-8"))
        profiles[p["id"]] = p
    return profiles


def local_now(profile: dict) -> datetime:
    tz = timezone(timedelta(hours=float(profile.get("utc_offset_hours", 0))))
    return datetime.now(tz).replace(tzinfo=None)


def salutation(profile: dict, now: datetime) -> str:
    s, h = profile["salutations"], now.hour
    if h < 11 or (h < 12 and "midday" not in s):
        return s["morning"]
    if "midday" in s and h < 15:
        return s["midday"]
    return s["afternoon"] if h < 18 else s["evening"]


def format_amount(amount: float, currency: str) -> str:
    if currency == "Rp":
        return "Rp" + f"{amount:,.0f}".replace(",", ".")
    return f"{currency}{amount:,.0f}"


def account_context(profile: dict, now: datetime) -> dict | None:
    """Computed, display-ready account facts for reminder profiles (deterministic: no LLM arithmetic)."""
    acc = profile.get("account")
    if not acc:
        return None
    cur = profile["outcome_params"]["currency"]
    today = now.date()
    due = today + timedelta(days=acc["due_in_days"])
    days_late = max((today - due).days, 0)
    ctx = {"reference": acc["reference"], "product": acc["product"], "customer_name": acc["customer_name"],
           "salutation_name": acc["salutation_name"], "amount_label": acc["amount_label"],
           "amount": format_amount(acc["amount"], cur), "due_date": due.isoformat(), "days_overdue": days_late}
    if "grace_days" in acc:  # life insurance: grace period, no penalty
        grace_end = due + timedelta(days=acc["grace_days"])
        ctx.update(grace_period_end=grace_end.isoformat(), payment_mode=acc["payment_mode"],
                   face_amount=acc["face_amount"], riders=acc["riders"], autodebit_status=acc["autodebit_status"],
                   automatic_premium_loan="active" if acc["apl_active"] else "not active", branch=acc["branch"],
                   total_due=format_amount(acc["amount"], cur), deadline_date=grace_end.isoformat())
    else:  # multifinance: daily late penalty with a cap, promise-to-pay window
        penalty = min(days_late * acc["penalty_rate_per_day"] * acc["amount"], acc["penalty_cap"] * acc["amount"])
        ptp_max = today + timedelta(days=acc["ptp_max_days"])
        ctx.update(installment=f"{acc['installment_no']} dari {acc['tenor_months']}",
                   penalty_so_far=format_amount(penalty, cur),
                   penalty_rule=f"{acc['penalty_rate_per_day'] * 100:.1f}% per hari dari angsuran, maksimal "
                                f"{acc['penalty_cap'] * 100:.0f}%",
                   penalty_per_day=format_amount(acc["penalty_rate_per_day"] * acc["amount"], cur),
                   total_due_today=format_amount(acc["amount"] + penalty, cur),
                   promise_to_pay_latest=ptp_max.isoformat(), deadline_date=ptp_max.isoformat())
    return ctx


# ----------------------------------------------------------------------------------------------
# Field validation
# ----------------------------------------------------------------------------------------------
def coerce(spec: dict, value):
    """Validate/convert an LLM-extracted value. Returns (ok, value_or_reason)."""
    if value is None or value == "":
        return False, "empty"
    t = spec["type"]
    try:
        if t == "integer":
            value = int(round(float(str(value).replace(",", ""))))
        elif t == "number":
            value = float(str(value).replace(",", ""))
            value = int(value) if value.is_integer() else value
        elif t == "boolean":
            if isinstance(value, str):
                value = value.strip().lower() in ("true", "yes", "y", "1")
            value = bool(value)
        else:
            value = str(value).strip()
    except ValueError:
        return False, f"not a valid {t}"
    if "min" in spec and value < spec["min"]:
        return False, f"below minimum {spec['min']}"
    if "max" in spec and value > spec["max"]:
        return False, f"above maximum {spec['max']}"
    if spec.get("enum") and value not in spec["enum"]:
        return False, f"must be one of {spec['enum']}"
    if spec["name"] == "verification_last4":
        digits = re.sub(r"\D", "", value)
        if len(digits) != 4:
            return False, "needs exactly 4 digits"
        value = digits
    if spec["name"] == "promised_payment_date":
        try:
            date.fromisoformat(value)
        except ValueError:
            return False, "not an ISO date (YYYY-MM-DD)"
    return True, value


def same_value(a, b) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
        return norm(a) == norm(b) or norm(a) in norm(b) or norm(b) in norm(a)
    return a == b


# ----------------------------------------------------------------------------------------------
# Outcomes
# ----------------------------------------------------------------------------------------------
STATUS_LABELS = {
    "qualified": "Qualified - advisor follow-up",
    "qualified_underwriting": "Qualified - underwriting review (pre-existing condition)",
    "nurture": "Nurture - below minimum budget",
    "review": "Advisor review needed",
    "not_eligible": "Not eligible",
    "do_not_contact": "Do not contact",
    "incomplete": "Incomplete",
    "promise_to_pay": "Promise to pay recorded",
    "ptp_beyond_limit": "Promised date beyond allowed window - escalate",
    "hardship": "Payment difficulty - advisor/branch follow-up",
    "paid_pending_verification": "Says already paid - verify payment",
    "retention_escalation": "Wants to cancel - retention escalation",
    "callback_scheduled": "Callback scheduled",
    "no_commitment": "No commitment",
    "wrong_party": "Wrong party - no details disclosed",
    "unverified": "Identity not verified",
    "verification_failed": "Verification failed - no details disclosed",
}


def _plan_recommendation(members: str, eldest: int | None) -> list[str]:
    m = (members or "").lower()
    parents = bool(re.search(r"parent|mother|father|\bmom\b|\bdad\b|in-law", m))
    family = bool(re.search(r"spouse|wife|husband|child|children|kid|son|daughter|family|baby", m))
    self_ = bool(re.search(r"\b(self|me|myself|i|my own)\b", m))
    plans = []
    if family:
        plans.append("Family Floater")
    elif self_ or not parents:
        plans.append("Senior Care" if (eldest or 0) >= 61 and not parents else "Essential")
    if parents:
        plans.append("Senior Care")
    return list(dict.fromkeys(plans))


def evaluate_health_lead(fields: dict, params: dict, required: list[str]) -> dict:
    missing = [f for f in required if fields.get(f) is None]
    reasons = []
    if fields.get("consent_to_contact") is False:
        return {"status": "do_not_contact", "reasons": ["customer declined to be contacted"], "missing": missing}
    age, eldest = fields.get("age"), fields.get("eldest_member_age")
    if age is not None and age < params["proposer_min_age"]:
        return {"status": "not_eligible", "reasons": [f"proposer is {age}; must be at least {params['proposer_min_age']}"],
                "missing": missing}
    plans = _plan_recommendation(fields.get("members_to_cover"), eldest)
    if eldest is not None and eldest > params["member_max_age"]:
        reasons.append(f"eldest member is {eldest}; new cover only up to {params['member_max_age']} - advisor to review")
    if missing:
        return {"status": "incomplete", "reasons": reasons or [f"missing: {', '.join(missing)}"],
                "missing": missing, "plan_recommendation": plans}
    status = "qualified"
    if reasons:
        status = "review"
    budget = fields.get("annual_budget_inr")
    if budget is not None and budget < params["min_budget_inr"]:
        status = "nurture"
        reasons.append(f"annual budget {budget} is below {params['min_budget_inr']}")
    ped = (fields.get("pre_existing_conditions") or "").strip().lower()
    if status == "qualified" and ped and not re.fullmatch(r"(none|no|nil|nothing|no conditions?|none known)\.?", ped):
        status = "qualified_underwriting"
        reasons.append(f"declared condition(s): {fields['pre_existing_conditions']}")
    if status == "qualified":
        reasons.append("all required details captured; no declared conditions")
    return {"status": status, "reasons": reasons, "missing": [], "plan_recommendation": plans}


def evaluate_payment(fields: dict, ctx: dict, verified: bool | None, attempts: int, required: list[str],
                     today: date) -> dict:
    if fields.get("is_policyholder") is False:
        return {"status": "wrong_party", "reasons": ["the person who answered is not the customer"], "missing": []}
    if not verified:
        status = "verification_failed" if attempts >= 2 else "unverified"
        return {"status": status, "reasons": [f"verification attempts: {attempts}"], "missing": ["verification_last4"]}
    c = fields.get("payment_commitment")
    reasons, missing = [], [f for f in required if fields.get(f) is None]
    if c == "will_pay":
        d = fields.get("promised_payment_date")
        if not d:
            return {"status": "incomplete", "reasons": ["payment date not captured"], "missing": ["promised_payment_date"]}
        d = date.fromisoformat(d)
        deadline = date.fromisoformat(ctx["deadline_date"])
        if d < today:
            return {"status": "incomplete", "reasons": [f"promised date {d} is in the past"],
                    "missing": ["promised_payment_date"]}
        if d > deadline:
            return {"status": "ptp_beyond_limit", "reasons": [f"promised {d}, latest allowed {deadline}"], "missing": []}
        reasons.append(f"will pay on {d} via {fields.get('payment_channel') or 'unspecified channel'}")
        return {"status": "promise_to_pay", "reasons": reasons, "missing": []}
    if c == "cannot_pay_now":
        return {"status": "hardship", "reasons": [fields.get("reason_for_delay") or "cannot pay now"], "missing": []}
    if c == "already_paid":
        return {"status": "paid_pending_verification", "reasons": ["customer reports payment made"], "missing": []}
    if c == "wants_cancel":
        return {"status": "retention_escalation", "reasons": ["customer wants to cancel"], "missing": []}
    if fields.get("callback_time"):
        return {"status": "callback_scheduled", "reasons": [f"callback: {fields['callback_time']}"], "missing": []}
    return {"status": "incomplete" if missing else "no_commitment", "reasons": [], "missing": missing}
