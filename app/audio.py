"""Small audio helpers (numpy + soundfile only; no ffmpeg needed)."""
from __future__ import annotations

import io

import numpy as np
import soundfile as sf

from app.config import SAMPLE_RATE


def decode(data: bytes) -> tuple[np.ndarray, int]:
    """Decode WAV/FLAC/MP3 bytes to float32 (mono or multi-channel) and sample rate."""
    audio, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=False)
    return audio, sr


def to_mono(audio: np.ndarray) -> np.ndarray:
    return audio.mean(axis=1) if audio.ndim == 2 else audio


def resample(audio: np.ndarray, sr_from: int, sr_to: int = SAMPLE_RATE) -> np.ndarray:
    if sr_from == sr_to or len(audio) == 0:
        return audio.astype(np.float32)
    n = int(round(len(audio) * sr_to / sr_from))
    x_old = np.linspace(0, 1, len(audio), endpoint=False)
    x_new = np.linspace(0, 1, n, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def wav_bytes(audio: np.ndarray, sr: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, np.clip(audio, -1, 1), sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def mp3_to_pcm16k(mp3: bytes) -> np.ndarray:
    audio, sr = decode(mp3)
    return resample(to_mono(audio), sr, SAMPLE_RATE)


def rms_db(frame: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(frame.astype(np.float64) ** 2)) + 1e-9))
