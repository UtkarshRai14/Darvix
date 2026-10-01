"""Streaming energy-based voice-activity segmenter (one per audio channel).

Audio arrives in small chunks; the segmenter emits a speech segment once it sees `hangover` of silence
after speech (end-pointing). The threshold adapts to the channel's noise floor, so steady background noise
(traffic hum) does not open segments; loud bursts can, and are filtered later by ASR confidence.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.config import SAMPLE_RATE

FRAME = SAMPLE_RATE // 50  # 20 ms


@dataclass
class Segment:
    channel: int
    start: float     # audio time (s) of speech start
    end: float       # audio time (s) of last speech frame
    audio: np.ndarray


class Segmenter:
    def __init__(self, channel: int, margin_db: float = 8.0, min_threshold_db: float = -48.0,
                 hangover_s: float = 0.55, min_speech_s: float = 0.3, max_segment_s: float = 12.0,
                 preroll_s: float = 0.2):
        self.channel = channel
        self.margin_db, self.min_threshold_db = margin_db, min_threshold_db
        self.hangover = int(hangover_s * 50)
        self.min_speech = int(min_speech_s * 50)
        self.max_frames = int(max_segment_s * 50)
        self.preroll = int(preroll_s * 50)
        self.levels: list[float] = []         # frame levels (dB) of the last 3 s -> noise-floor estimate
        self.noise_floor = -70.0
        self.buffer = np.zeros(0, dtype=np.float32)
        self.frames: list[np.ndarray] = []   # frames of the current segment (incl. preroll)
        self.recent: list[np.ndarray] = []   # rolling preroll frames
        self.in_speech = False
        self.speech_frames = 0
        self.silence_run = 0
        self.frame_index = 0                 # absolute frame counter -> audio time
        self.seg_start_frame = 0
        self.last_speech_frame = 0

    def _threshold(self) -> float:
        return max(self.noise_floor + self.margin_db, self.min_threshold_db)

    def push(self, samples: np.ndarray) -> list[Segment]:
        out: list[Segment] = []
        self.buffer = np.concatenate([self.buffer, samples.astype(np.float32)])
        while len(self.buffer) >= FRAME:
            frame, self.buffer = self.buffer[:FRAME], self.buffer[FRAME:]
            db = float(20 * np.log10(np.sqrt(np.mean(frame.astype(np.float64) ** 2)) + 1e-9))
            # Noise floor = 15th percentile of the last 3 s of frame levels. Speech has gaps between words, so
            # the low percentile follows the background noise even while someone is talking.
            self.levels = (self.levels + [db])[-150:]
            if len(self.levels) >= 25:
                self.noise_floor = float(np.percentile(self.levels, 15))
            speech = db > self._threshold()
            if not self.in_speech:
                self.recent = (self.recent + [frame])[-self.preroll:]
                if speech:
                    self.in_speech, self.speech_frames, self.silence_run = True, 1, 0
                    self.frames = list(self.recent)
                    self.seg_start_frame = self.frame_index - len(self.recent) + 1
                    self.last_speech_frame = self.frame_index
            else:
                self.frames.append(frame)
                if speech:
                    self.speech_frames += 1
                    self.silence_run = 0
                    self.last_speech_frame = self.frame_index
                else:
                    self.silence_run += 1
                if self.silence_run >= self.hangover or len(self.frames) >= self.max_frames:
                    seg = self._close()
                    if seg:
                        out.append(seg)
            self.frame_index += 1
        return out

    def _close(self) -> Segment | None:
        seg = None
        if self.speech_frames >= self.min_speech:
            seg = Segment(self.channel, self.seg_start_frame / 50, (self.last_speech_frame + 1) / 50,
                          np.concatenate(self.frames))
        self.in_speech, self.frames, self.recent, self.speech_frames, self.silence_run = False, [], [], 0, 0
        return seg

    def flush(self) -> list[Segment]:
        if self.in_speech:
            seg = self._close()
            return [seg] if seg else []
        return []
