"""Synthesize the live-insight test calls as dual-channel recordings (agent = left, customer = right).

    python -m app.live.build_scenarios

Writes live_scenarios/audio/<id>.wav and <id>.timeline.json (turn start/end times = ground truth).
Dual-channel audio mirrors how contact-centre platforms record calls, which gives exact speaker separation.
"""
from __future__ import annotations

import asyncio
import json

import numpy as np

from app.audio import mp3_to_pcm16k, wav_bytes
from app.config import SAMPLE_RATE, SCENARIOS_DIR
from app.tts import synthesize

AUDIO_DIR = SCENARIOS_DIR / "audio"
BABBLE_TEXT = ("So I told him we should take the earlier train because the traffic near the station is terrible in the "
               "evening, and then we could grab something to eat before the meeting starts at seven.")


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x ** 2)) + 1e-9)


def _street_noise(n: int, rng: np.random.Generator) -> np.ndarray:
    brown = np.cumsum(rng.normal(0, 1, n))
    brown -= np.convolve(brown, np.ones(4000) / 4000, mode="same")  # remove drift, keep low rumble
    t = np.arange(n) / SAMPLE_RATE
    hum = 0.3 * np.sin(2 * np.pi * 100 * t)
    noise = brown / (np.abs(brown).max() + 1e-9) + hum
    return noise.astype(np.float32)


def _horn(rng: np.random.Generator) -> np.ndarray:
    t = np.arange(int(0.6 * SAMPLE_RATE)) / SAMPLE_RATE
    f = rng.uniform(380, 460)
    tone = np.sign(np.sin(2 * np.pi * f * t)) * 0.6 + np.sin(2 * np.pi * 1.25 * f * t) * 0.4
    env = np.minimum(1, t / 0.03) * np.minimum(1, (t[-1] - t) / 0.05)
    return (tone * env).astype(np.float32)


async def build(scenario: dict) -> None:
    rng = np.random.default_rng(7)
    pieces, timeline, cursor = [], [], 0.6
    for i, turn in enumerate(scenario["turns"]):
        mp3, _ = await synthesize(turn["text"], scenario["voices"][turn["speaker"]])
        pcm = mp3_to_pcm16k(mp3)
        ch = 0 if turn["speaker"] == "agent" else 1
        start = cursor
        pieces.append((ch, int(start * SAMPLE_RATE), pcm))
        cursor += len(pcm) / SAMPLE_RATE
        timeline.append({"turn": i, "speaker": turn["speaker"], "text": turn["text"], "start": round(start, 2),
                         "end": round(cursor, 2)})
        cursor += float(rng.uniform(0.55, 0.95))
    total = int((cursor + 1.0) * SAMPLE_RATE)
    audio = np.zeros((total, 2), dtype=np.float32)
    for ch, offset, pcm in pieces:
        audio[offset:offset + len(pcm), ch] += pcm

    noise_cfg = scenario.get("noise")
    if noise_cfg:
        speech = np.concatenate([p for ch, _, p in pieces if ch == 1])
        noise = _street_noise(total, rng)
        mp3, _ = await synthesize(BABBLE_TEXT, scenario["voices"].get("babble", "en-US-GuyNeural"))
        babble = mp3_to_pcm16k(mp3)
        babble = np.tile(babble, total // len(babble) + 1)[:total]
        noise = noise / _rms(noise) + 0.8 * babble / _rms(babble)
        noise *= _rms(speech) / _rms(noise) / (10 ** (noise_cfg["snr_db"] / 20))
        for _ in range(noise_cfg.get("horns", 0)):
            at = int(rng.uniform(1, cursor - 1) * SAMPLE_RATE)
            h = _horn(rng) * _rms(speech) * 2.5
            noise[at:at + len(h)] += h[: len(noise) - at]
        audio[:, 1] += noise
    peak = np.abs(audio).max()
    if peak > 0.98:
        audio *= 0.98 / peak
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    (AUDIO_DIR / f"{scenario['id']}.wav").write_bytes(wav_bytes(audio))
    (AUDIO_DIR / f"{scenario['id']}.timeline.json").write_text(json.dumps(timeline, indent=1), encoding="utf-8")
    print(f"built {scenario['id']}: {total / SAMPLE_RATE:.1f} s, {len(timeline)} turns")


async def main() -> None:
    for path in sorted(SCENARIOS_DIR.glob("*.json")):
        await build(json.loads(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    asyncio.run(main())
