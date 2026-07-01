#!/usr/bin/env python3
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import sounddevice as sd
from scipy.fftpack import dct


@dataclass
class MatchResult:
    keyword: str
    matched: bool
    score: float
    threshold: float


def audio_rms(audio: np.ndarray) -> float:
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio.astype(np.float32)))))


def hz_to_mel(freq: float | np.ndarray) -> float | np.ndarray:
    return 2595.0 * np.log10(1.0 + np.asarray(freq) / 700.0)


def mel_to_hz(mel: float | np.ndarray) -> float | np.ndarray:
    return 700.0 * (10.0 ** (np.asarray(mel) / 2595.0) - 1.0)


def mfcc_features(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if audio.size == 0:
        return np.zeros((0, 13), dtype=np.float32)

    max_abs = float(np.max(np.abs(audio)))
    if max_abs > 0:
        audio = audio / max_abs
    audio = np.append(audio[0], audio[1:] - 0.97 * audio[:-1])

    frame_len = int(sample_rate * 0.025)
    frame_step = int(sample_rate * 0.010)
    if audio.size < frame_len:
        audio = np.pad(audio, (0, frame_len - audio.size))

    frame_count = 1 + int(np.ceil((audio.size - frame_len) / frame_step))
    pad_len = (frame_count - 1) * frame_step + frame_len
    if pad_len > audio.size:
        audio = np.pad(audio, (0, pad_len - audio.size))

    indices = (
        np.tile(np.arange(frame_len), (frame_count, 1))
        + np.tile(np.arange(frame_count) * frame_step, (frame_len, 1)).T
    )
    frames = audio[indices]
    frames *= np.hamming(frame_len)

    nfft = 512
    mag = np.absolute(np.fft.rfft(frames, nfft))
    power = (1.0 / nfft) * (mag**2)

    filter_count = 26
    low_mel = hz_to_mel(20)
    high_mel = hz_to_mel(sample_rate / 2)
    mel_points = np.linspace(low_mel, high_mel, filter_count + 2)
    hz_points = mel_to_hz(mel_points)
    bins = np.floor((nfft + 1) * hz_points / sample_rate).astype(int)

    filters = np.zeros((filter_count, nfft // 2 + 1), dtype=np.float32)
    for m in range(1, filter_count + 1):
        left = bins[m - 1]
        center = bins[m]
        right = bins[m + 1]
        if center > left:
            filters[m - 1, left:center] = (np.arange(left, center) - left) / (center - left)
        if right > center:
            filters[m - 1, center:right] = (right - np.arange(center, right)) / (right - center)

    energies = np.dot(power, filters.T)
    energies = np.where(energies == 0, np.finfo(float).eps, energies)
    cepstra = dct(np.log(energies), type=2, axis=1, norm="ortho")[:, :13]
    cepstra -= np.mean(cepstra, axis=0, keepdims=True)
    return cepstra.astype(np.float32)


def dtw_distance(left: np.ndarray, right: np.ndarray) -> float:
    if left.size == 0 or right.size == 0:
        return float("inf")
    n, m = len(left), len(right)
    previous = np.full(m + 1, np.inf, dtype=np.float64)
    current = np.full(m + 1, np.inf, dtype=np.float64)
    previous[0] = 0.0
    for i in range(1, n + 1):
        current[0] = np.inf
        li = left[i - 1]
        for j in range(1, m + 1):
            cost = float(np.linalg.norm(li - right[j - 1]))
            current[j] = cost + min(previous[j], current[j - 1], previous[j - 1])
        previous, current = current, previous
    return float(previous[m] / max(n + m, 1))


class KeywordMatcher:
    def __init__(self, templates_dir: Path, sample_rate: int, config: dict[str, Any]) -> None:
        self.templates_dir = templates_dir
        self.sample_rate = sample_rate
        self.config = config
        self.templates: dict[str, list[np.ndarray]] = {}
        self.load()

    def load(self) -> None:
        self.templates = {}
        if not self.templates_dir.exists():
            return
        for keyword_dir in self.templates_dir.glob("*"):
            if not keyword_dir.is_dir():
                continue
            features = []
            for path in sorted(keyword_dir.glob("*.npy")):
                try:
                    features.append(np.load(path))
                except Exception:
                    pass
            if features:
                self.templates[keyword_dir.name] = features

    def has_keyword(self, keyword: str) -> bool:
        return bool(self.templates.get(keyword))

    def threshold_for(self, keyword: str) -> float:
        thresholds = self.config.get("thresholds", {})
        if keyword in thresholds:
            return float(thresholds[keyword])
        templates = self.templates.get(keyword, [])
        if len(templates) < 2:
            return float(self.config.get("default_threshold", 42.0))
        distances = []
        for i, left in enumerate(templates):
            for right in templates[i + 1 :]:
                distances.append(dtw_distance(left, right))
        if not distances:
            return float(self.config.get("default_threshold", 42.0))
        return max(float(np.median(distances) * 1.85), float(self.config.get("minimum_threshold", 24.0)))

    def score(self, keyword: str, audio: np.ndarray) -> float:
        templates = self.templates.get(keyword, [])
        if not templates:
            return float("inf")
        features = mfcc_features(audio, self.sample_rate)
        return min(dtw_distance(features, template) for template in templates)

    def match(self, keyword: str, audio: np.ndarray) -> MatchResult:
        score = self.score(keyword, audio)
        threshold = self.threshold_for(keyword)
        return MatchResult(keyword=keyword, matched=score <= threshold, score=score, threshold=threshold)


class StreamingKeywordDetector:
    def __init__(
        self,
        matcher: KeywordMatcher,
        keyword: str,
        config: dict[str, Any],
        on_detect: Callable[[MatchResult], None],
    ) -> None:
        self.matcher = matcher
        self.keyword = keyword
        self.config = config
        self.on_detect = on_detect
        self.sample_rate = matcher.sample_rate
        self.queue: queue.Queue[np.ndarray | None] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.active = False
        self.segment: list[np.ndarray] = []
        self.silence_samples = 0
        self.last_detected_at = 0.0
        self.lock = threading.Lock()

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def stop(self) -> None:
        self.queue.put(None)
        if self.worker and self.worker.is_alive() and threading.current_thread() is not self.worker:
            self.worker.join(timeout=1.0)
        self.worker = None

    def accept_audio(self, audio: np.ndarray) -> None:
        mono = np.asarray(audio, dtype=np.float32)
        if mono.ndim > 1:
            mono = mono.mean(axis=1)
        level = audio_rms(mono)
        start_rms = float(self.config.get("vad_start_rms", 0.025))
        stop_rms = float(self.config.get("vad_stop_rms", 0.012))
        min_samples = int(float(self.config.get("min_phrase_seconds", 0.35)) * self.sample_rate)
        max_samples = int(float(self.config.get("max_phrase_seconds", 1.8)) * self.sample_rate)
        silence_limit = int(float(self.config.get("silence_end_seconds", 0.30)) * self.sample_rate)

        with self.lock:
            if not self.active and level >= start_rms:
                self.active = True
                self.segment = []
                self.silence_samples = 0

            if not self.active:
                return

            self.segment.append(mono.copy())
            if level < stop_rms:
                self.silence_samples += len(mono)
            else:
                self.silence_samples = 0

            total = sum(len(chunk) for chunk in self.segment)
            ended_by_silence = total >= min_samples and self.silence_samples >= silence_limit
            ended_by_length = total >= max_samples
            if ended_by_silence or ended_by_length:
                segment = np.concatenate(self.segment) if self.segment else np.array([], dtype=np.float32)
                self.active = False
                self.segment = []
                self.silence_samples = 0
                if len(segment) >= min_samples:
                    self.queue.put(segment)

    def _run(self) -> None:
        while True:
            segment = self.queue.get()
            if segment is None:
                return
            cooldown = float(self.config.get("cooldown_seconds", 1.2))
            now = time.time()
            if now - self.last_detected_at < cooldown:
                continue
            result = self.matcher.match(self.keyword, segment)
            print(
                f"Voice trigger score {self.keyword}: {result.score:.2f} / {result.threshold:.2f}",
                flush=True,
            )
            if result.matched:
                self.last_detected_at = now
                self.on_detect(result)


class WakeMonitor:
    def __init__(
        self,
        matcher: KeywordMatcher,
        keyword: str,
        config: dict[str, Any],
        device: int | str | None,
        channels: int,
        on_detect: Callable[[MatchResult], None],
    ) -> None:
        self.matcher = matcher
        self.keyword = keyword
        self.config = config
        self.device = device
        self.channels = channels
        self.detector = StreamingKeywordDetector(matcher, keyword, config, on_detect)
        self.stream: sd.InputStream | None = None

    def start(self) -> None:
        if self.stream is not None:
            return
        if not self.matcher.has_keyword(self.keyword):
            print(f"Voice trigger template missing: {self.keyword}", flush=True)
            return
        self.detector.start()

        def callback(indata, frames, time_info, status):
            if status:
                print(f"Voice trigger audio warning: {status}", flush=True)
            self.detector.accept_audio(indata.copy())

        self.stream = sd.InputStream(
            samplerate=self.matcher.sample_rate,
            channels=self.channels,
            dtype="float32",
            device=self.device,
            callback=callback,
        )
        self.stream.start()
        print(f"Voice trigger listening for {self.keyword}.", flush=True)

    def stop(self) -> None:
        stream = self.stream
        self.stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self.detector.stop()


def record_fixed_duration(
    sample_rate: int,
    channels: int,
    device: int | str | None,
    seconds: float,
) -> np.ndarray:
    frames: list[np.ndarray] = []

    def callback(indata, frame_count, time_info, status):
        if status:
            print(f"Enrollment audio warning: {status}", flush=True)
        frames.append(indata.copy())

    with sd.InputStream(
        samplerate=sample_rate,
        channels=channels,
        dtype="float32",
        device=device,
        callback=callback,
    ):
        time.sleep(seconds)

    audio = np.concatenate(frames) if frames else np.array([], dtype=np.float32)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return audio.astype(np.float32)


def save_keyword_template(
    templates_dir: Path,
    keyword: str,
    audio: np.ndarray,
    sample_rate: int,
    index: int,
) -> Path:
    keyword_dir = templates_dir / keyword
    keyword_dir.mkdir(parents=True, exist_ok=True)
    features = mfcc_features(audio, sample_rate)
    path = keyword_dir / f"sample_{index:02d}.npy"
    np.save(path, features)
    return path
