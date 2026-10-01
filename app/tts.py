"""Text-to-speech with Microsoft Edge neural voices (free, no API key) via edge-tts.

Native voices used: en-IN (India), fil-PH (Filipino), id-ID (Indonesian); jv-ID is used only by the
call simulator as a Javanese-accent proxy for the Indonesian customer.
"""
from __future__ import annotations

import asyncio
import re
import time

import edge_tts

_cache: dict[tuple, bytes] = {}


def speakable(text: str) -> str:
    """Remove things a voice should never read aloud (markdown, record IDs, brackets)."""
    text = re.sub(r"\[?kb_[a-z_]+_\d+\]?", "", text)
    text = re.sub(r"[*_#`>|]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


HEDGE_AFTER_S = 2.5  # a normal reply takes ~1.1 s; the service occasionally stalls for 10-15 s


async def _fetch(text: str, voice: str, rate: str) -> bytes:
    audio = bytearray()
    async for chunk in edge_tts.Communicate(text, voice, rate=rate).stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
    return bytes(audio)


async def _hedged(text: str, voice: str, rate: str) -> bytes:
    """If the first request has not finished (or failed) after HEDGE_AFTER_S, race a second one."""
    tasks = [asyncio.create_task(_fetch(text, voice, rate))]
    done, _ = await asyncio.wait(tasks, timeout=HEDGE_AFTER_S)
    if not done or tasks[0].exception():
        tasks.append(asyncio.create_task(_fetch(text, voice, rate)))
    error: Exception | None = None
    try:
        for fut in asyncio.as_completed(tasks):
            try:
                return await fut
            except Exception as exc:
                error = exc
        raise error
    finally:
        for t in tasks:
            t.cancel()


async def synthesize(text: str, voice: str, rate: str = "+0%") -> tuple[bytes, int]:
    """Return (mp3 bytes, synthesis ms). Short fixed phrases (greetings, fallbacks) are cached."""
    text = speakable(text)
    key = (text, voice, rate)
    if key in _cache:
        return _cache[key], 0
    t0 = time.perf_counter()
    data = await _hedged(text, voice, rate)
    if len(text) < 260:
        _cache[key] = data
    return data, round((time.perf_counter() - t0) * 1000)
