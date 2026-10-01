"""Real-time call analysis: audio chunks -> end-pointing -> streaming ASR -> signals -> nudges -> delivery.

A dual-channel recording is replayed at real-time speed in 250 ms chunks (the same code path a live
media stream would feed). Every nudge carries component timings so P50/P95 latency can be reported.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from typing import Awaitable, Callable

import numpy as np
import soundfile as sf

from app.audio import resample, wav_bytes
from app.config import RECORDINGS_DIR, REPORTS_DIR, SAMPLE_RATE, SCENARIOS_DIR
from app.live.nudges import NudgeController
from app.live.segmenter import Segmenter
from app.live.signals import ComplianceMonitor, FrustrationTrend, extract_customer_signals
from app.llm import LLMError, atranscribe
from app.metrics import percentile

CHUNK_S = 0.25
# Each channel uses its own Whisper model, so each gets its own free-tier rate-limit bucket. The customer side
# (more varied, noisier speech, where most signals come from) gets the larger model.
ASR_MODELS = {0: "whisper-large-v3-turbo", 1: "whisper-large-v3"}
ASR_PROMPT = ("Nivaran Health Insurance. Family floater, senior care, essential plan, pre-existing disease, waiting "
              "period, premium, lakh, rupees, cashless, claim, reimbursement, renewal.")
HALLUCINATIONS = re.compile(r"^(thank you\.?|thanks for watching!?|you\.?|bye\.?|\.+|subtitles by.*|"
                            r"please subscribe.*)$", re.I)
SPEAKERS = {0: "agent", 1: "customer"}
SESSIONS_DIR = REPORTS_DIR / "live_sessions"

Emit = Callable[[dict], Awaitable[None]]


def list_sources() -> list[dict]:
    """Replayable calls: synthetic test scenarios + recorded health-insurance calls from the voice agent."""
    out = []
    for path in sorted(SCENARIOS_DIR.glob("*.json")):
        sc = json.loads(path.read_text(encoding="utf-8"))
        wav = SCENARIOS_DIR / "audio" / f"{sc['id']}.wav"
        if wav.exists():
            out.append({"id": sc["id"], "kind": "scenario", "title": sc["title"], "description": sc["description"],
                        "call_type": sc["call_type"], "expected": sc["expected"], "audio_url": f"/scenario-audio/{sc['id']}.wav",
                        "duration": round(sf.info(str(wav)).duration, 1)})
    for wav in sorted(RECORDINGS_DIR.glob("*/call.wav")):
        meta_path = wav.parent / "transcript.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("profile_id") != "health_lead_in":
            continue  # signal extractor and playbook are tuned for the English health-insurance domain
        out.append({"id": f"rec:{meta['call_id']}", "kind": "recording", "title": f"Recorded agent call {meta['call_id']}",
                    "description": f"Voice-agent call ({meta['source']}), outcome: {meta['outcome']['label']}. "
                                   "Agent channel = voice bot.",
                    "call_type": "sales", "expected": None, "audio_url": f"/recordings/{meta['call_id']}/call.wav",
                    "duration": round(sf.info(str(wav)).duration, 1)})
    return out


def _source_paths(source_id: str) -> tuple:
    if source_id.startswith("rec:"):
        return RECORDINGS_DIR / source_id[4:] / "call.wav", None
    return SCENARIOS_DIR / "audio" / f"{source_id}.wav", SCENARIOS_DIR / "audio" / f"{source_id}.timeline.json"


class LiveSession:
    def __init__(self, source_id: str, emit: Emit, speed: float = 1.0):
        src = next((s for s in list_sources() if s["id"] == source_id), None)
        if src is None:
            raise ValueError(f"unknown source {source_id}")
        self.source, self.emit, self.speed = src, emit, speed
        wav_path, timeline_path = _source_paths(source_id)
        audio, sr = sf.read(str(wav_path), dtype="float32", always_2d=True)
        if audio.shape[1] == 1:
            raise ValueError("live analysis needs a dual-channel recording (agent / customer)")
        self.audio = np.stack([resample(audio[:, c], sr) for c in (0, 1)], axis=1)
        self.timeline = json.loads(timeline_path.read_text(encoding="utf-8")) if timeline_path else None
        self.segmenters = {0: Segmenter(0), 1: Segmenter(1)}
        self.compliance = ComplianceMonitor(src["call_type"])
        self.frustration = FrustrationTrend()
        self.controller = NudgeController()
        self.asr_sem = asyncio.Semaphore(4)
        self.window: list[dict] = []
        self.segments: list[dict] = []
        self.signals_log: list[dict] = []
        self.emitted: dict[str, dict] = {}   # nudge id -> nudge dict (with timings)
        self.last_speaker = None
        self.topic = None
        self.tasks: list[asyncio.Task] = []
        self.t0 = 0.0
        self.seg_counter = 0
        self.errors: list[str] = []

    # ------------------------------------------------------------------ clocks
    def wall(self, audio_t: float) -> float:
        return self.t0 + audio_t / self.speed

    def audio_now(self) -> float:
        return (time.monotonic() - self.t0) * self.speed

    # ------------------------------------------------------------------ main loop
    async def run(self) -> dict:
        n = len(self.audio)
        step = int(CHUNK_S * SAMPLE_RATE)
        self.t0 = time.monotonic()
        await self.emit({"type": "start", "source": self.source, "speed": self.speed, "duration": n / SAMPLE_RATE,
                         "checklist": self.compliance.items})
        for i, start in enumerate(range(0, n, step)):
            delay = self.wall(start / SAMPLE_RATE) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            received = time.monotonic()
            for ch in (0, 1):
                for seg in self.segmenters[ch].push(self.audio[start:start + step, ch]):
                    self.tasks.append(asyncio.create_task(self._process(seg, received)))
            if i % 4 == 0:
                await self._tick()
        for ch in (0, 1):
            for seg in self.segmenters[ch].flush():
                self.tasks.append(asyncio.create_task(self._process(seg, time.monotonic())))
        await asyncio.gather(*self.tasks)
        await asyncio.sleep(0.5)  # let late acknowledgements arrive
        await self._tick()
        summary = self.summary()
        await self.emit({"type": "end", "summary": summary})
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        (SESSIONS_DIR / f"{self.source['id'].replace(':', '_')}-{datetime.now():%Y%m%d-%H%M%S}.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        return summary

    async def _tick(self) -> None:
        now = self.audio_now()
        await self.emit({"type": "progress", "t": round(now, 2)})
        for n in self.controller.tick(now):
            await self._emit_update(n)
        sigs = self.compliance.check_deadlines(now)
        if sigs:
            await self._deliver(sigs, {"speech_end_wall": time.monotonic()})
            await self.emit({"type": "checklist", "items": self.compliance.items})

    # ------------------------------------------------------------------ per segment
    async def _process(self, seg, finalized_at: float) -> None:
        self.seg_counter += 1
        seg_id = f"s{self.seg_counter}"
        speaker = SPEAKERS[seg.channel]
        speech_end_wall = self.wall(seg.end)
        timing = {"endpoint_ms": round((finalized_at - speech_end_wall) * 1000), "speech_end_wall": speech_end_wall}
        t_wait = time.monotonic()
        try:
            async with self.asr_sem:
                t_asr = time.monotonic()
                asr = await atranscribe(wav_bytes(seg.audio), language="en", prompt=ASR_PROMPT,
                                        model=ASR_MODELS[seg.channel])
        except LLMError as exc:
            self.errors.append(str(exc))
            await self.emit({"type": "error", "message": f"ASR failed: {exc}"})
            return
        timing["asr_queue_ms"] = round((t_asr - t_wait) * 1000) + asr.get("waited_ms", 0)
        timing["asr_ms"] = asr["ms"]
        text = asr["text"].strip()
        low = self._low_confidence(asr, text)
        rec = {"id": seg_id, "speaker": speaker, "start": round(seg.start, 2), "end": round(seg.end, 2), "text": text,
               "asr_ms": asr["ms"], "endpoint_ms": timing["endpoint_ms"], "queue_ms": timing["asr_queue_ms"],
               "rtf": round(asr["ms"] / 1000 / max(seg.end - seg.start, 0.1), 3), "model": asr["model"],
               "avg_logprob": asr.get("avg_logprob"), "no_speech_prob": asr.get("no_speech_prob"),
               "low_confidence": low}
        self.segments.append(rec)
        await self.emit({"type": "transcript", **rec})
        if low:
            return

        self.window.append({"speaker": speaker, "text": text, "t": seg.start})
        self.window.sort(key=lambda w: w["t"])
        signals, changed = [], []
        t_rules = time.perf_counter()
        if speaker == "agent":
            new_turn = self.last_speaker != "agent"
            self.last_speaker = "agent"
            signals += self.compliance.on_agent(text, seg.end, new_turn)
            changed = self.controller.on_agent_text(text, self.audio_now(), new_turn)
        else:
            self.last_speaker = "customer"
        timing["rules_ms"] = round((time.perf_counter() - t_rules) * 1000, 2)

        if speaker == "customer" and len(re.findall(r"\w+", text)) >= 3:
            try:
                llm_signals, info = await asyncio.to_thread(extract_customer_signals, list(self.window), text, seg.end)
                timing["llm_ms"] = info["llm_ms"]
                signals += llm_signals + self.frustration.update(info["frustration"], text, seg.end)
                await self.emit({"type": "sentiment", "t": seg.end, "frustration": info["frustration"],
                                 "topic": info["topic"]})
                if info["topic"] not in ("small_talk", "other") and info["topic"] != self.topic:
                    if self.topic is not None:
                        await self.emit({"type": "insight", "kind": "topic_shift", "t": seg.end,
                                         "text": f"Topic: {self.topic} -> {info['topic']}"})
                    self.topic = info["topic"]
            except LLMError as exc:
                self.errors.append(str(exc))
                await self.emit({"type": "error", "message": f"Signal LLM failed: {exc}"})

        for n in changed:  # resolutions, expirations and missed-opportunity repeats triggered by agent speech
            if n.id in self.emitted:
                await self._emit_update(n)
            else:
                await self._emit_nudge(n, timing)
        await self._deliver(signals, timing)
        if speaker == "agent":
            await self.emit({"type": "checklist", "items": self.compliance.items})

    def _low_confidence(self, asr: dict, text: str) -> str | None:
        if not text:
            return "empty transcript"
        if HALLUCINATIONS.match(text):
            return "known silence hallucination"
        if (asr.get("no_speech_prob") or 0) > 0.5:
            return f"no_speech_prob {asr['no_speech_prob']}"
        if (asr.get("avg_logprob") or 0) < -1.0:
            return f"avg_logprob {asr['avg_logprob']}"
        return None

    async def _deliver(self, signals: list, timing: dict) -> None:
        if not signals:
            return
        t_ctrl = time.perf_counter()
        before = len(self.controller.suppressed)
        nudges = self.controller.consider(signals, self.audio_now())
        timing = dict(timing, nudge_ms=round((time.perf_counter() - t_ctrl) * 1000, 2))
        suppressed = self.controller.suppressed[before:]
        for s in signals:
            entry = {"type": s.type, "confidence": round(s.confidence, 2), "source": s.source, "t": round(s.t, 2),
                     "evidence": s.evidence, "topic_key": s.topic_key,
                     "suppressed": next((x["reason"] for x in suppressed if x["type"] == s.type
                                         and x["topic_key"] == s.topic_key), None)}
            self.signals_log.append(entry)
            await self.emit({"type": "signal", **entry})
        for n in nudges:
            if n.id in self.emitted:
                await self._emit_update(n)
            else:
                await self._emit_nudge(n, timing)

    async def _emit_nudge(self, n, timing: dict) -> None:
        now = time.monotonic()
        t = {k: v for k, v in timing.items() if k != "speech_end_wall"}
        t["e2e_emit_ms"] = round((now - timing["speech_end_wall"]) * 1000)
        n.timings = t
        record = n.to_dict()
        record["emitted_audio_t"] = round(self.audio_now(), 2)
        record["_emitted_wall"] = now
        record["_speech_end_wall"] = timing["speech_end_wall"]
        self.emitted[n.id] = record
        await self.emit({"type": "nudge", "nudge": {k: v for k, v in record.items() if not k.startswith("_")}})

    async def _emit_update(self, n) -> None:
        if n.id in self.emitted:
            self.emitted[n.id]["status"] = n.status
        await self.emit({"type": "nudge_update", "id": n.id, "status": n.status})

    def ack(self, nudge_id: str) -> None:
        """Dashboard confirms a nudge was rendered: delivery and end-to-end display latency."""
        rec = self.emitted.get(nudge_id)
        if rec and "delivery_ms" not in rec["timings"]:
            now = time.monotonic()
            rec["timings"]["delivery_ms"] = round((now - rec["_emitted_wall"]) * 1000)
            rec["timings"]["e2e_display_ms"] = round((now - rec["_speech_end_wall"]) * 1000)

    # ------------------------------------------------------------------ reporting
    def evaluate(self) -> dict | None:
        expected = self.source.get("expected")
        if expected is None or not self.timeline:
            return None
        windows = []
        for e in expected:
            turn = self.timeline[e["turn"]]
            windows.append({**e, "from": turn["start"] - 1.0, "to": turn["end"] + 20.0, "matched": None})
        tp, fp, optional = [], [], []
        for n in sorted(self.emitted.values(), key=lambda r: r["emitted_audio_t"]):
            cand = [w for w in windows if w["type"] == n["type"] and w["matched"] is None
                    and w["from"] <= n["emitted_audio_t"] <= w["to"]]
            cand.sort(key=lambda w: w.get("optional", False))
            if cand:
                cand[0]["matched"] = n["id"]
                (optional if cand[0].get("optional") else tp).append(n["id"])
            else:
                fp.append({"id": n["id"], "type": n["type"], "t": n["emitted_audio_t"], "evidence": n["evidence"]})
        fn = [{"type": w["type"], "turn": w["turn"]} for w in windows if not w.get("optional") and w["matched"] is None]
        precision = len(tp) / (len(tp) + len(fp)) if (tp or fp) else None
        recall = len(tp) / (len(tp) + len(fn)) if (tp or fn) else None
        return {"true_positives": len(tp), "optional_matched": len(optional), "false_positives": fp,
                "false_negatives": fn, "precision": precision, "recall": recall}

    def summary(self) -> dict:
        nudges = [{k: v for k, v in r.items() if not k.startswith("_")} for r in self.emitted.values()]
        comp = {}
        for key in ("endpoint_ms", "asr_queue_ms", "asr_ms", "rules_ms", "llm_ms", "nudge_ms", "delivery_ms",
                    "e2e_emit_ms", "e2e_display_ms"):
            vals = [n["timings"].get(key) for n in nudges]
            comp[key] = {"p50": percentile(vals, 0.5), "p95": percentile(vals, 0.95),
                         "n": len([v for v in vals if v is not None])}
        seg_asr = [s["asr_ms"] for s in self.segments]
        reasons: dict[str, int] = {}
        for s in self.controller.suppressed:
            r = s["reason"].split(" (")[0]
            reasons[r] = reasons.get(r, 0) + 1
        return {
            "source_id": self.source["id"], "title": self.source["title"], "speed": self.speed,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "segments": len(self.segments),
            "segments_filtered_low_confidence": sum(1 for s in self.segments if s["low_confidence"]),
            "asr_per_segment": {"p50_ms": percentile(seg_asr, 0.5), "p95_ms": percentile(seg_asr, 0.95),
                                "p50_rtf": percentile([s["rtf"] for s in self.segments], 0.5)},
            "raw_signals": len(self.signals_log), "nudges_delivered": len(nudges),
            "suppressed_by_reason": reasons, "latency": comp, "nudges": nudges, "transcript": self.segments,
            "signals": self.signals_log, "checklist": self.compliance.items, "evaluation": self.evaluate(),
            "errors": self.errors[:20],
        }
