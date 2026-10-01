"""Groq client wrapper: strict-JSON chat, speech-to-text, and free-tier aware rate limiting.

Free-tier limits are per model (e.g. gpt-oss: 30 RPM / 8K TPM; whisper: 20 RPM). Instead of failing
mid-call with HTTP 429, requests wait for capacity in a sliding 60 s window and retry on 429.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque

from groq import APIStatusError, BadRequestError, Groq, RateLimitError

from app.config import ASR_MODEL, GROQ_API_KEY

LIMITS = {  # model -> (requests per minute, tokens per minute); conservative copies of the free tier
    "openai/gpt-oss-120b": (28, 7600),
    "openai/gpt-oss-20b": (28, 7600),
    "whisper-large-v3": (19, None),
    "whisper-large-v3-turbo": (19, None),
}


class LLMError(RuntimeError):
    pass


class _Window:
    """Sliding 60-second window of (timestamp, tokens) for one model."""

    def __init__(self, rpm: int, tpm: int | None):
        self.rpm, self.tpm = rpm, tpm
        self.events: deque[list] = deque()
        self.lock = threading.Lock()
        self.blocked_until = 0.0
        self.cached_hint = 0  # prompt-cache hits on the latest response; they do not count toward TPM

    def try_acquire(self, est_tokens: int) -> tuple[list | None, float]:
        """Reserve capacity now -> (event, 0); otherwise (None, seconds until capacity may free up)."""
        est_tokens = max(est_tokens - self.cached_hint, 0)
        with self.lock:
            now = time.monotonic()
            if now < self.blocked_until:
                return None, self.blocked_until - now
            while self.events and now - self.events[0][0] > 60:
                self.events.popleft()
            used = sum(e[1] for e in self.events)
            ok_req = len(self.events) < self.rpm
            ok_tok = self.tpm is None or used + est_tokens <= self.tpm or not self.events
            if ok_req and ok_tok:
                event = [now, est_tokens]
                self.events.append(event)
                return event, 0.0
            return None, max(60 - (now - self.events[0][0]) + 0.05, 0.05)

    def acquire(self, est_tokens: int) -> list:
        while True:
            event, wait = self.try_acquire(est_tokens)
            if event:
                return event
            time.sleep(min(wait, 2.0))

    def block(self, seconds: float) -> None:
        self.blocked_until = max(self.blocked_until, time.monotonic() + seconds)


_windows: dict[str, _Window] = {}
_client: Groq | None = None


def _window(model: str) -> _Window:
    if model not in _windows:
        rpm, tpm = LIMITS.get(model, (25, None))
        _windows[model] = _Window(rpm, tpm)
    return _windows[model]


def client() -> Groq:
    global _client
    if not GROQ_API_KEY:
        raise LLMError("GROQ_API_KEY is not set. Copy .env.example to .env and add a free key from console.groq.com.")
    if _client is None:
        _client = Groq(api_key=GROQ_API_KEY, max_retries=0, timeout=30)
    return _client


def _retry_after(exc: APIStatusError) -> float:
    try:
        return float(exc.response.headers.get("retry-after", "2"))
    except (TypeError, ValueError):
        return 2.0


def _rate_limited_tokens(usage, fallback: int, model: str) -> int:
    """Tokens that count toward the TPM limit: Groq does not count prompt-cache hits."""
    if not usage:
        return fallback
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) or 0
    _window(model).cached_hint = cached
    return usage.total_tokens - cached


def _error_body(exc: APIStatusError) -> dict:
    body = exc.body if isinstance(exc.body, dict) else {}
    return body.get("error", body) if isinstance(body.get("error", body), dict) else {}


def _error_code(exc: APIStatusError) -> str | None:
    return _error_body(exc).get("code")


_JSON_TYPES = {"string": str, "object": dict, "array": list, "boolean": bool}
RETRYABLE_400 = ("tool_use_failed", "json_validate_failed", "output_parse_failed")


def _salvage(exc: APIStatusError, schema: dict) -> dict | None:
    """gpt-oss on Groq sometimes wraps the structured output as a tool call (400 tool_use_failed) or omits
    null-valued keys (400 json_validate_failed). The complete generation is in the error body; use it when its
    top-level keys, types and enums are right, instead of paying for (and waiting on) a re-generation."""
    err = _error_body(exc)
    if err.get("code") not in ("tool_use_failed", "json_validate_failed"):
        return None
    try:
        gen = json.loads(err.get("failed_generation") or "")
        data = gen.get("arguments", gen) if isinstance(gen, dict) else None
        if isinstance(data, str):
            data = json.loads(data)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    for key in schema.get("required", []):
        prop = schema.get("properties", {}).get(key, {})
        expected = _JSON_TYPES.get(prop.get("type"))
        if key not in data or (expected and not isinstance(data[key], expected)) or \
                ("enum" in prop and data[key] not in prop["enum"]):
            return None
    return data


def _acquire_any(models: tuple[str, ...], est: int) -> tuple[list, str]:
    """First model (in preference order) with free-tier capacity right now; waits only if all are exhausted."""
    while True:
        waits = []
        for m in models:
            event, wait = _window(m).try_acquire(est)
            if event:
                return event, m
            waits.append(wait)
        time.sleep(min(min(waits), 2.0))


def chat_json(messages: list[dict], schema: dict, model: str | tuple[str, ...], name: str = "response",
              reasoning_effort: str = "low", max_tokens: int = 1200, temperature: float = 0.3) -> tuple[dict, dict]:
    """Chat completion constrained to a JSON schema. Returns (parsed_json, info{ms, tokens, waited_ms}).

    `model` may be a tuple of models in preference order: each has its own free-tier quota, so a request
    goes to the next model instead of waiting when the preferred one is rate-limited."""
    models = (model,) if isinstance(model, str) else tuple(model)
    est = int(sum(len(m["content"]) for m in messages) / 3.5) + 300
    strict = True
    for attempt in range(4):
        t_wait = time.perf_counter()
        event, model = _acquire_any(models, est)
        t0 = time.perf_counter()
        try:
            resp = client().chat.completions.create(
                model=model, messages=messages, temperature=temperature, max_completion_tokens=max_tokens,
                reasoning_effort=reasoning_effort, include_reasoning=False,
                response_format={"type": "json_schema",
                                 "json_schema": {"name": name, "strict": strict, "schema": schema}},
            )
            event[1] = _rate_limited_tokens(resp.usage, est, model)
            content = resp.choices[0].message.content or ""
            return json.loads(content), {"ms": round((time.perf_counter() - t0) * 1000),
                                         "waited_ms": round((t0 - t_wait) * 1000),
                                         "tokens": event[1], "model": model}
        except RateLimitError as exc:
            _window(model).block(min(_retry_after(exc), 20))
        except BadRequestError as exc:
            data = _salvage(exc, schema)
            if data is not None:
                return data, {"ms": round((time.perf_counter() - t0) * 1000),
                              "waited_ms": round((t0 - t_wait) * 1000), "tokens": event[1], "model": model}
            # Strict schema generation can occasionally fail validation; fall back to best-effort mode once.
            if strict:
                strict = False
                continue
            if _error_code(exc) in RETRYABLE_400 and attempt < 3:
                continue
            raise LLMError(f"LLM rejected request: {str(exc)[:200]}") from exc
        except json.JSONDecodeError:
            continue
        except APIStatusError as exc:
            if attempt == 3:
                raise LLMError(f"LLM API error {exc.status_code}") from exc
            time.sleep(1.5)
        except Exception as exc:  # network errors, timeouts
            if attempt == 3:
                raise LLMError(f"LLM request failed: {type(exc).__name__}: {exc}") from exc
            time.sleep(1.0)
    raise LLMError("LLM did not return valid JSON")


def chat_text(messages: list[dict], model: str, max_tokens: int = 400, temperature: float = 0.8,
              reasoning_effort: str = "low") -> str:
    """Plain-text completion (used by the simulated test customer)."""
    est = int(sum(len(m["content"]) for m in messages) / 3.5) + 200
    for attempt in range(4):
        event = _window(model).acquire(est)
        try:
            resp = client().chat.completions.create(
                model=model, messages=messages, temperature=temperature, max_completion_tokens=max_tokens,
                reasoning_effort=reasoning_effort, include_reasoning=False)
            event[1] = resp.usage.total_tokens if resp.usage else est
            return (resp.choices[0].message.content or "").strip()
        except RateLimitError as exc:
            time.sleep(min(_retry_after(exc), 20))
        except Exception as exc:
            if attempt == 3:
                raise LLMError(f"LLM request failed: {type(exc).__name__}: {exc}") from exc
            time.sleep(1.0)
    raise LLMError("LLM request failed")


def transcribe(wav: bytes, language: str | None = None, prompt: str | None = None,
               model: str = ASR_MODEL) -> dict:
    """Whisper transcription with confidence signals from verbose_json."""
    for attempt in range(4):
        t_wait = time.perf_counter()
        _window(model).acquire(0)
        t0 = time.perf_counter()
        try:
            resp = client().audio.transcriptions.create(
                file=("audio.wav", wav), model=model, language=language, prompt=prompt or None,
                response_format="verbose_json", temperature=0.0)
            data = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
            segments = data.get("segments") or []
            total = sum(max(s.get("end", 0) - s.get("start", 0), 0.01) for s in segments) or 1.0
            avg_logprob = (sum(s.get("avg_logprob", 0) * max(s.get("end", 0) - s.get("start", 0), 0.01)
                               for s in segments) / total) if segments else None
            no_speech = max((s.get("no_speech_prob", 0) for s in segments), default=None)
            return {"text": (data.get("text") or "").strip(), "language": data.get("language"),
                    "avg_logprob": None if avg_logprob is None else round(avg_logprob, 3),
                    "no_speech_prob": None if no_speech is None else round(no_speech, 3),
                    "ms": round((time.perf_counter() - t0) * 1000), "waited_ms": round((t0 - t_wait) * 1000),
                    "model": model}
        except RateLimitError as exc:
            time.sleep(min(_retry_after(exc), 20))
        except Exception as exc:
            if attempt == 3:
                raise LLMError(f"Transcription failed: {type(exc).__name__}: {exc}") from exc
            time.sleep(1.0)
    raise LLMError("Transcription failed")


async def achat_json(*args, **kwargs):
    return await asyncio.to_thread(chat_json, *args, **kwargs)


async def atranscribe(*args, **kwargs):
    return await asyncio.to_thread(transcribe, *args, **kwargs)
