"""Web server: voice-agent calls, knowledge-base explorer, live call insights.

    uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent import crm
from app.agent.engine import CallSession
from app.agent.rules import account_context, load_profiles, local_now
from app.config import (AGENT_MODEL, ASR_MODEL, EMBED_MODEL, FAST_MODEL, GROQ_API_KEY, KB_DIR, RECORDINGS_DIR,
                        REPORTS_DIR, SCENARIOS_DIR, WEB_DIR)
from app.kb.store import KnowledgeBase, get_embedder
from app.live.pipeline import SESSIONS_DIR, LiveSession, list_sources
from app.llm import LLMError, atranscribe

PROFILES = load_profiles()
CALLS: dict[str, tuple[CallSession, float]] = {}
CALL_TTL_S = 1800


@asynccontextmanager
async def lifespan(_: FastAPI):
    def warm():
        get_embedder()
        for p in PROFILES.values():
            KnowledgeBase.get(p["kb_collection"]).search("warm up", 1)
    try:
        await asyncio.to_thread(warm)
    except FileNotFoundError as exc:
        print(f"WARNING: {exc}")
    yield


app = FastAPI(title="Callwise", lifespan=lifespan)


@app.exception_handler(LLMError)
async def llm_error(_, exc: LLMError):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _session(call_id: str) -> CallSession:
    now = time.time()
    for cid in [c for c, (_, ts) in CALLS.items() if now - ts > CALL_TTL_S]:
        CALLS.pop(cid, None)
    if call_id not in CALLS:
        raise HTTPException(404, "call not found or expired")
    session, _ = CALLS[call_id]
    CALLS[call_id] = (session, now)
    return session


# ---------------------------------------------------------------------------------------------- system
@app.get("/api/health")
def health():
    return {"groq_key_configured": bool(GROQ_API_KEY), "agent_model": AGENT_MODEL, "fast_model": FAST_MODEL,
            "asr_model": ASR_MODEL, "embedding_model": EMBED_MODEL,
            "knowledge_bases": sorted(p.name for p in KB_DIR.iterdir() if (p / "embeddings.npy").exists())}


@app.get("/api/profiles")
def profiles():
    out = []
    for p in PROFILES.values():
        acc = account_context(p, local_now(p))
        out.append({k: p[k] for k in ("id", "title", "market", "language_label", "company", "agent_name",
                                      "kb_collection")} | {
            "asr": p["asr"], "tts_voice": p["tts"]["voice"],
            "fields": [{"name": f["name"], "label": f["label"], "required": f.get("required", False)} for f in p["fields"]],
            "test_account": None if not acc else {**acc, "verify_last4": p["account"]["verify_last4"]}})
    return out


# ---------------------------------------------------------------------------------------------- voice calls
class StartCall(BaseModel):
    profile_id: str


@app.post("/api/calls")
async def start_call(body: StartCall):
    if body.profile_id not in PROFILES:
        raise HTTPException(404, "unknown profile")
    if not GROQ_API_KEY:
        raise HTTPException(503, "GROQ_API_KEY is not set. Add it to .env and restart the server.")
    session = CallSession(PROFILES[body.profile_id], source="web")
    CALLS[session.id] = (session, time.time())
    res = await session.start()
    return {"call_id": session.id, "reply": res["reply"], "audio_b64": _b64(res["audio"]), "turn": res["turn"],
            "state": res["state"]}


@app.post("/api/calls/{call_id}/turn")
async def call_turn(call_id: str, audio: UploadFile | None = File(None), text: str | None = Form(None)):
    session = _session(call_id)
    if session.ended:
        raise HTTPException(409, "call already ended")
    asr = None
    if audio is not None:
        cfg = session.profile["asr"]
        asr = await atranscribe(await audio.read(), language=cfg["language"], prompt=cfg["prompt"])
        text = asr["text"]
    res = await session.handle(text or "", asr)
    return {"user_text": text or "", "asr": asr, "reply": res["reply"], "audio_b64": _b64(res["audio"]),
            "turn": res["turn"], "state": res["state"], "end_call": res["end_call"]}


@app.post("/api/calls/{call_id}/end")
def end_call(call_id: str):
    session = _session(call_id)
    result = session.finalize()
    CALLS.pop(call_id, None)
    return result


@app.post("/api/calls/{call_id}/recording")
async def upload_recording(call_id: str, audio: UploadFile = File(...)):
    folder = RECORDINGS_DIR / call_id
    if not (folder / "transcript.json").exists():
        raise HTTPException(404, "end the call before uploading its recording")
    (folder / "call.wav").write_bytes(await audio.read())
    meta = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
    meta["recording"] = "call.wav"
    (folder / "transcript.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


@app.get("/api/calls")
def list_calls():
    out = []
    for f in sorted(RECORDINGS_DIR.glob("*/transcript.json"), reverse=True):
        c = json.loads(f.read_text(encoding="utf-8"))
        checks = (c.get("test") or {}).get("checks", [])
        out.append({"call_id": c["call_id"], "profile_id": c["profile_id"], "profile_title": c["profile_title"],
                    "market": c["market"], "source": c["source"], "started_at": c["started_at"],
                    "duration_s": c["duration_s"], "outcome": c["outcome"], "actions": c["actions"],
                    "has_recording": (f.parent / "call.wav").exists(), "metrics": c["metrics"],
                    "persona": (c.get("test") or {}).get("persona"),
                    "checks_passed": sum(x["passed"] for x in checks), "checks_total": len(checks)})
    return out


@app.get("/api/calls/{call_id}")
def get_call(call_id: str):
    f = RECORDINGS_DIR / call_id / "transcript.json"
    if not f.exists():
        raise HTTPException(404, "call not found")
    data = json.loads(f.read_text(encoding="utf-8"))
    data["has_recording"] = (f.parent / "call.wav").exists()
    return data


@app.get("/api/crm")
def crm_tables():
    return {t: crm.read(t, 20) for t in crm.TABLES}


# ---------------------------------------------------------------------------------------------- knowledge base
@app.get("/api/kb")
def kb_overview():
    out = []
    for folder in sorted(KB_DIR.iterdir()):
        rep = folder / "report.json"
        if rep.exists():
            r = json.loads(rep.read_text(encoding="utf-8"))
            out.append({"collection": r["collection"], "name": r["name"], "kb_version": r["kb_version"],
                        "generated_at": r["generated_at"], "totals": r["totals"], "categories": r["categories"]})
    return out


@app.get("/api/kb/{collection}/report")
def kb_report(collection: str):
    rep = KB_DIR / collection / "report.json"
    if not rep.exists():
        raise HTTPException(404, "unknown collection")
    return json.loads(rep.read_text(encoding="utf-8"))


@app.get("/api/kb/{collection}/records")
def kb_records(collection: str, status: str | None = None, category: str | None = None):
    path = KB_DIR / collection / "records.jsonl"
    if not path.exists():
        raise HTTPException(404, "unknown collection")
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [r for r in rows if (not status or r["status"] == status) and (not category or r["category"] == category)]


@app.get("/api/kb/{collection}/search")
async def kb_search(collection: str, q: str, k: int = 5, category: str | None = None):
    try:
        kb = KnowledgeBase.get(collection)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    t0 = time.perf_counter()
    hits = await asyncio.to_thread(kb.search, q, k, category)
    return {"query": q, "expanded_query": kb.expand(q), "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "hits": hits}


@app.get("/api/kb-eval")
def kb_eval():
    f = REPORTS_DIR / "retrieval_eval.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


# ---------------------------------------------------------------------------------------------- live insights
@app.get("/api/live/sources")
def live_sources():
    return list_sources()


@app.get("/api/live/sessions")
def live_sessions():
    latest = {}
    for path in sorted(SESSIONS_DIR.glob("*.json")) if SESSIONS_DIR.exists() else []:
        s = json.loads(path.read_text(encoding="utf-8"))
        latest[s["source_id"]] = {k: s[k] for k in ("source_id", "title", "finished_at", "nudges_delivered",
                                                    "raw_signals", "suppressed_by_reason", "latency", "evaluation")}
    return list(latest.values())


@app.websocket("/ws/live")
async def live_ws(ws: WebSocket):
    await ws.accept()
    lock = asyncio.Lock()
    session: LiveSession | None = None
    task: asyncio.Task | None = None

    async def emit(event: dict) -> None:
        async with lock:
            await ws.send_json(event)

    async def runner(s: LiveSession):
        try:
            await s.run()
        except Exception as exc:  # surface failures to the dashboard instead of dropping the socket
            await emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "start":
                if task and not task.done():
                    task.cancel()
                if not GROQ_API_KEY:
                    await emit({"type": "error", "message": "GROQ_API_KEY is not set. Add it to .env and restart."})
                    continue
                try:
                    session = LiveSession(msg["source_id"], emit, speed=1.0)
                except ValueError as exc:
                    await emit({"type": "error", "message": str(exc)})
                    continue
                task = asyncio.create_task(runner(session))
            elif msg.get("type") == "ack" and session:
                session.ack(msg["id"])
            elif msg.get("type") == "stop" and task:
                task.cancel()
                await emit({"type": "stopped"})
    except WebSocketDisconnect:
        if task:
            task.cancel()


# ---------------------------------------------------------------------------------------------- static
app.mount("/recordings", StaticFiles(directory=RECORDINGS_DIR), name="recordings")
SCENARIO_AUDIO = SCENARIOS_DIR / "audio"
SCENARIO_AUDIO.mkdir(parents=True, exist_ok=True)
app.mount("/scenario-audio", StaticFiles(directory=SCENARIO_AUDIO), name="scenario-audio")
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
