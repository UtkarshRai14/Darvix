"""Recorded test calls through the full voice pipeline.

    python -m app.agent.simulate                    # all personas in tests/call_personas.yaml
    python -m app.agent.simulate in_cooperative     # selected personas
    python -m app.agent.simulate --report           # only rebuild reports/call_results.md

An LLM plays the customer (persona + language register), its words are spoken by a native neural voice,
transcribed by Whisper with the market's ASR config, and answered by the same engine the web call uses.
Output per call: recordings/<call_id>/call.wav (stereo: agent left, customer right), transcript.json/.md.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys

import numpy as np
import yaml

from app.audio import mp3_to_pcm16k, wav_bytes
from app.agent.engine import CallSession
from app.agent.rules import load_profiles
from app.config import FAST_MODEL, RECORDINGS_DIR, REPORTS_DIR, ROOT, SAMPLE_RATE
from app.llm import atranscribe, chat_text
from app.metrics import percentile, wer
from app.tts import synthesize

MAX_TURNS = 14
GAP = np.zeros(int(0.35 * SAMPLE_RATE), dtype=np.float32)


def _customer_messages(p: dict, transcript: list[tuple[str, str]]) -> list[dict]:
    system = (
        "You are role-playing a CUSTOMER on a phone call with a company's voice agent.\n"
        f"Persona and goals: {p['persona'].strip()}\n"
        f"Language and register: {p['language'].strip()}\n"
        "Rules: reply with only the words you say out loud - one or two short spoken sentences, no stage directions, "
        "no quotation marks, no speaker labels. Follow your persona; do not volunteer everything at once. Never say "
        "you are an AI or a test. When the agent has said goodbye or the call is clearly over, reply exactly <hangup>.")
    msgs = [{"role": "system", "content": system}]
    for who, text in transcript:
        msgs.append({"role": "user" if who == "agent" else "assistant", "content": text})
    return msgs


def _evaluate_checks(checks: list[dict], r: dict) -> list[dict]:
    agent = [t for t in r["turns"] if t["role"] == "agent"]
    out = []
    for c in checks:
        t = c["type"]
        if t == "outcome_in":
            ok, got = r["outcome"]["status"] in c["values"], r["outcome"]["status"]
        elif t == "action":
            got = [a["type"] for a in r["actions"]]
            ok = c["value"] in got
        elif t == "answer_status_seen":
            got = sorted({a["answer_status"] for a in agent if a.get("answer_status")})
            ok = c["value"] in got
        elif t == "cited_category_any":
            got = sorted({ct["category"] for a in agent for ct in a.get("citations", [])})
            ok = bool(set(c["values"]) & set(got))
        elif t == "conflict_detected":
            got = [f"{x['field']}: {x['old']} -> {x['new']}" for x in r.get("conflicts_detected", [])]
            ok = bool(got)
        elif t == "verified":
            ok, got = bool(r.get("verified")), r.get("verified")
        else:
            ok, got = False, f"unknown check {t}"
        out.append({"check": t, "expected": c.get("values") or c.get("value") or True, "got": got, "passed": ok})
    return out


async def run_persona(persona: dict, profiles: dict) -> dict:
    profile = profiles[persona["profile"]]
    session = CallSession(profile, source="simulated")
    segments: list[tuple[int, np.ndarray]] = []
    transcript: list[tuple[str, str]] = []
    asr_pairs = []

    start = await session.start()
    if start["audio"]:
        segments.append((0, mp3_to_pcm16k(start["audio"])))
    transcript.append(("agent", start["reply"]))
    print(f"\n=== {persona['id']} ({profile['id']}) call {session.id}\nAGENT: {start['reply']}")

    for _ in range(MAX_TURNS):
        said = await asyncio.to_thread(chat_text, _customer_messages(persona, transcript), FAST_MODEL)
        said = re.sub(r"^(customer|me|[A-Z][a-z]+)\s*:\s*", "", said.strip().strip('"')).strip()
        if not said or "<hangup>" in said:
            break
        mp3, _ = await synthesize(said, persona.get("voice") or profile["customer_voice"])
        pcm = mp3_to_pcm16k(mp3)
        segments.append((1, pcm))
        transcript.append(("customer", said))
        asr = await atranscribe(wav_bytes(pcm), language=profile["asr"]["language"], prompt=profile["asr"]["prompt"])
        score, errors = wer(said, asr["text"])
        asr_pairs.append({"spoken": said, "asr": asr["text"], "wer": round(score, 3), "errors": errors[:8]})
        print(f"CUSTOMER: {said}\n   (ASR: {asr['text']})")
        res = await session.handle(asr["text"], asr)
        if res["audio"]:
            segments.append((0, mp3_to_pcm16k(res["audio"])))
        transcript.append(("agent", res["reply"]))
        print(f"AGENT: {res['reply']}")
        if res["end_call"]:
            break

    result = session.finalize("customer_hung_up" if not session.ended else None)
    stereo = []
    for ch, pcm in segments:
        block = np.zeros((len(pcm) + len(GAP), 2), dtype=np.float32)
        block[: len(pcm), ch] = pcm
        stereo.append(block)
    folder = RECORDINGS_DIR / session.id
    (folder / "call.wav").write_bytes(wav_bytes(np.concatenate(stereo), SAMPLE_RATE))
    result["test"] = {"persona": persona["id"], "coverage": persona["coverage"], "voice": persona.get("voice"),
                      "checks": _evaluate_checks(persona.get("checks", []), result), "asr_pairs": asr_pairs,
                      "asr_wer_mean": round(float(np.mean([a["wer"] for a in asr_pairs])), 3) if asr_pairs else None}
    result["recording"] = "call.wav"
    (folder / "transcript.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print("CHECKS:", [(c["check"], c["passed"]) for c in result["test"]["checks"]])
    return result


def write_report() -> None:
    calls = []
    for f in sorted(RECORDINGS_DIR.glob("*/transcript.json")):
        calls.append(json.loads(f.read_text(encoding="utf-8")))
    lines = ["# Voice-agent test calls", "",
             "Generated by `python -m app.agent.simulate --report` from `recordings/*/transcript.json` "
             "(simulated calls and calls made in the web interface).", "",
             "| Call | Agent | Source / scenario | Coverage | Outcome | Turns | Grounded | Unavailable | Actions | "
             "p50 / p95 turn latency (ms) | Checks |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    all_total = []
    for c in calls:
        m, t = c["metrics"], c.get("test") or {}
        checks = t.get("checks", [])
        passed = f"{sum(x['passed'] for x in checks)}/{len(checks)}" if checks else "-"
        all_total += [x["timings"].get("total_ms") for x in c["turns"] if x["role"] == "agent" and x["kind"] != "greeting"]
        rec = f"[{c['call_id']}](../recordings/{c['call_id']}/transcript.md)"
        lines.append(f"| {rec} | {c['profile_id']} | {c['source']} {t.get('persona', '')} | "
                     f"{', '.join(t.get('coverage', [])) or '-'} | {c['outcome']['label']} | {m['agent_turns']} | "
                     f"{m['grounded_answers']} | {m['unavailable_answers']} | "
                     f"{', '.join(a['type'] for a in c['actions']) or '-'} | {m['p50_total_ms']} / {m['p95_total_ms']} | "
                     f"{passed} |")
    lines += ["", f"Overall agent-turn latency (customer speech end -> reply audio ready): p50 "
              f"{percentile(all_total, 0.5)} ms, p95 {percentile(all_total, 0.95)} ms over {len(all_total)} turns.", ""]
    for c in calls:
        t = c.get("test")
        if not t:
            continue
        lines += [f"## {t['persona']} - `{c['call_id']}`", "",
                  f"Outcome: **{c['outcome']['label']}** | ASR mean WER on this call: {t['asr_wer_mean']}", ""]
        lines += [f"- {'PASS' if x['passed'] else 'FAIL'} `{x['check']}` expected {x['expected']}, got {x['got']}"
                  for x in t["checks"]]
        lines.append("")
    (REPORTS_DIR / "call_results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote reports/call_results.md ({len(calls)} calls)")


async def main(argv: list[str]) -> None:
    if "--report" not in argv:
        personas = yaml.safe_load((ROOT / "tests" / "call_personas.yaml").read_text(encoding="utf-8"))
        selected = [p for p in personas if not argv or p["id"] in argv]
        profiles = load_profiles()
        for p in selected:
            await run_persona(p, profiles)
    write_report()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
