#!/usr/bin/env python3
"""The local, raw-transcript worker used by Voice Flow.app."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
import wave
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyperclip
import sounddevice as sd


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
TMP_DIR = BASE_DIR / "tmp"
LOG_DIR = BASE_DIR / "logs"
RUNTIME_DIR = BASE_DIR / "runtime"
LOCK_PATH = RUNTIME_DIR / "voice_flow.lock"
PID_PATH = RUNTIME_DIR / "voice_flow.pid"
STATUS_PATH = RUNTIME_DIR / "voice_flow_status.json"
PASTE_REQUEST_PATH = RUNTIME_DIR / "paste_request.json"


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def retention_config(config: dict[str, Any]) -> dict[str, Any]:
    defaults = {
        "keep_audio": True,
        "max_audio_files": 3,
        "keep_failed_audio": True,
        "keep_logs": True,
        "max_logs": 500,
        "max_tmp_files": 20,
        "max_age_days": 7,
    }
    defaults.update(config.get("retention", {}))
    return defaults


def ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(exist_ok=True)


def notify(message: str, config: dict[str, Any]) -> None:
    print(message, flush=True)
    if not config.get("notify", False):
        return
    safe_message = message.replace('"', "'")
    subprocess.run(
        ["osascript", "-e", f'display notification "{safe_message}" with title "Voice Flow"'],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def write_status(
    state: str,
    config: dict[str, Any],
    level: float = 0.0,
    message: str = "",
    elapsed_seconds: float | None = None,
) -> None:
    if not config.get("hud", {}).get("enabled", True):
        return
    payload: dict[str, Any] = {
        "state": state,
        "level": max(0.0, min(float(level), 1.0)),
        "message": message,
        "updated_at": time.time(),
    }
    if elapsed_seconds is not None:
        payload["elapsed_seconds"] = max(0.0, float(elapsed_seconds))
    try:
        temporary = STATUS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(STATUS_PATH)
    except OSError:
        pass


def run_osascript(script: str, timeout: float = 2.0) -> str:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def get_system_output_volume() -> int | None:
    output = run_osascript("output volume of (get volume settings)", timeout=1.0)
    try:
        return max(0, min(int(output), 100))
    except (TypeError, ValueError):
        return None


def set_system_output_volume(volume: int) -> None:
    run_osascript(f"set volume output volume {max(0, min(int(volume), 100))}", timeout=1.0)


def safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def cleanup_old_files(directory: Path, patterns: list[str], max_files: int, max_age_days: float) -> None:
    files: list[tuple[Path, float]] = []
    for pattern in patterns:
        for path in directory.glob(pattern):
            if path.is_file():
                try:
                    files.append((path, path.stat().st_mtime))
                except OSError:
                    pass
    now = time.time()
    max_age_seconds = max_age_days * 86400
    for index, (path, modified_at) in enumerate(sorted(files, key=lambda item: item[1], reverse=True)):
        if (max_files >= 0 and index >= max_files) or (max_age_seconds >= 0 and now - modified_at > max_age_seconds):
            safe_unlink(path)


def cleanup_runtime_files(config: dict[str, Any]) -> None:
    retention = retention_config(config)
    TMP_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)
    age_days = float(retention.get("max_age_days", 7))
    audio_limit = int(retention.get("max_audio_files", 3)) if retention.get("keep_audio", True) else 0
    cleanup_old_files(TMP_DIR, ["voice_*.wav"], audio_limit, age_days)
    cleanup_old_files(TMP_DIR, ["asr_*.txt"], int(retention.get("max_tmp_files", 20)), age_days)
    log_limit = int(retention.get("max_logs", 500)) if retention.get("keep_logs", True) else 0
    cleanup_old_files(LOG_DIR, ["*.json"], log_limit, age_days)


def wav_duration_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        return handle.getnframes() / rate if rate > 0 else 0.0


def split_wav(path: Path, chunk_seconds: float) -> list[Path]:
    chunks: list[Path] = []
    with wave.open(str(path), "rb") as source:
        frames_per_chunk = max(1, int(source.getframerate() * chunk_seconds))
        parameters = source.getparams()
        index = 0
        while frames := source.readframes(frames_per_chunk):
            chunk = TMP_DIR / f"asr_chunk_{path.stem}_{index:03d}_{uuid.uuid4().hex[:8]}.wav"
            with wave.open(str(chunk), "wb") as destination:
                destination.setparams(parameters)
                destination.writeframes(frames)
            chunks.append(chunk)
            index += 1
    return chunks


def write_run_log(audio_path: Path, payload: dict[str, Any], config: dict[str, Any]) -> None:
    if not retention_config(config).get("keep_logs", True):
        return
    LOG_DIR.mkdir(exist_ok=True)
    (LOG_DIR / f"{audio_path.stem}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def asr_context(config: dict[str, Any], asr_config: dict[str, Any]) -> str:
    hotwords = ", ".join(config.get("hotwords", []))
    if str(asr_config.get("language", "zh")).lower().startswith("en"):
        return f"English speech. Transcribe accurately. Preserve important terms and names: {hotwords}."
    return f"中文为主，中英混杂。优先正确识别这些术语：{hotwords}。"


def run_qwen3_asr(audio_path: Path, config: dict[str, Any], asr_config: dict[str, Any], env: dict[str, str]) -> str:
    output_base = TMP_DIR / f"asr_{audio_path.stem}_{uuid.uuid4().hex[:8]}"
    command = [
        str(BASE_DIR / ".venv/bin/python"),
        "-m", "mlx_audio.stt.generate",
        "--model", str(asr_config["model"]),
        "--audio", str(audio_path),
        "--output-path", str(output_base),
        "--format", "txt",
        "--language", str(asr_config.get("language", "zh")),
        "--max-tokens", str(asr_config.get("max_tokens", 8192)),
        "--chunk-duration", str(asr_config.get("chunk_duration", 30)),
        "--context", asr_context(config, asr_config),
    ]
    try:
        subprocess.run(
            command,
            cwd=BASE_DIR,
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=float(asr_config.get("timeout_seconds", 3600)),
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ASR failed: {exc.stderr or exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ASR timed out after {exc.timeout} seconds") from exc

    output_path = output_base.with_suffix(".txt")
    if not output_path.exists():
        output_path = Path(f"{output_base}.txt")
    try:
        return output_path.read_text(encoding="utf-8").strip()
    finally:
        safe_unlink(output_path)


def run_asr(audio_path: Path, config: dict[str, Any]) -> str:
    asr_config = config["asr"]
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    split_seconds = float(asr_config.get("long_audio_split_seconds", 0) or 0)
    if split_seconds > 0 and wav_duration_seconds(audio_path) > split_seconds:
        chunks = split_wav(audio_path, split_seconds)
        try:
            return "\n".join(
                text for chunk in chunks if (text := run_qwen3_asr(chunk, config, asr_config, env).strip())
            )
        finally:
            for chunk in chunks:
                safe_unlink(chunk)
    return run_qwen3_asr(audio_path, config, asr_config, env)


def apply_corrections(text: str, config: dict[str, Any]) -> str:
    for wrong, right in config.get("corrections", {}).items():
        text = re.sub(re.escape(wrong), right, text, flags=re.IGNORECASE)
    return text.strip()


def copy_and_maybe_paste(text: str, paste: bool, copy: bool = True) -> None:
    if not copy:
        return
    pyperclip.copy(text)
    if not paste:
        return
    if os.environ.get("VOICE_FLOW_NATIVE_PASTE") == "1":
        ensure_runtime_dir()
        request = {"id": uuid.uuid4().hex, "updated_at": time.time()}
        temporary = PASTE_REQUEST_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(request), encoding="utf-8")
        temporary.replace(PASTE_REQUEST_PATH)
        return
    run_osascript('tell application "System Events" to keystroke "v" using command down')


def process_audio(audio_path: Path, config: dict[str, Any], paste: bool, copy: bool = True) -> str:
    started_at = time.time()
    transcript = apply_corrections(run_asr(audio_path, config), config)
    copy_and_maybe_paste(transcript, paste=paste, copy=copy)
    write_run_log(
        audio_path,
        {
            "status": "ok",
            "audio": str(audio_path),
            "model": config["asr"]["model"],
            "transcript": transcript,
            "seconds": round(time.time() - started_at, 3),
        },
        config,
    )
    cleanup_runtime_files(config)
    return transcript


class Recorder:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.sample_rate = int(config.get("sample_rate", 16000))
        self.channels = int(config.get("channels", 1))
        self.device, self.device_label = resolve_audio_device(config)
        self.stream: sd.InputStream | None = None
        self.wav_file: wave.Wave_write | None = None
        self.audio_path: Path | None = None
        self.lock = threading.Lock()
        self.file_lock = threading.Lock()
        self.recording = False
        self.processing = False
        self.paste_when_done = bool(config.get("paste_after_stop", True))
        self.started_at = 0.0
        self.samples_written = 0
        self.last_level_update = 0.0
        self.original_output_volume: int | None = None
        self.max_record_seconds = float(config.get("max_record_seconds", 1800))
        self.min_record_seconds = float(config.get("min_record_seconds", 0.35))
        ensure_runtime_dir()
        TMP_DIR.mkdir(exist_ok=True)
        cleanup_runtime_files(config)

    def toggle(self, paste: bool) -> None:
        with self.lock:
            if self.processing:
                notify("Still processing the previous recording.", self.config)
            elif self.recording:
                self.stop_locked(paste)
            else:
                self.start_locked(paste)

    def duck_system_audio(self) -> None:
        ducking = self.config.get("audio_ducking", {})
        if not ducking.get("enabled", False):
            return
        current = get_system_output_volume()
        if current is None:
            return
        target = int(round(current * max(0.0, min(float(ducking.get("factor", 0.0)), 1.0))))
        self.original_output_volume = current
        if target != current:
            set_system_output_volume(target)

    def restore_system_audio(self) -> None:
        original = self.original_output_volume
        self.original_output_volume = None
        if original is not None and self.config.get("audio_ducking", {}).get("restore_on_stop", True):
            set_system_output_volume(original)

    def start_locked(self, paste: bool) -> None:
        self.paste_when_done = paste
        self.samples_written = 0
        self.started_at = time.time()
        self.duck_system_audio()
        self.device, self.device_label = resolve_audio_device(self.config, validate=True)
        try:
            self.audio_path = TMP_DIR / f"voice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
            self.wav_file = wave.open(str(self.audio_path), "wb")
            self.wav_file.setnchannels(1)
            self.wav_file.setsampwidth(2)
            self.wav_file.setframerate(self.sample_rate)

            def callback(indata: np.ndarray, frames: int, time_info: Any, status: sd.CallbackFlags) -> None:
                del frames, time_info
                if status:
                    print(f"Audio warning: {status}", flush=True)
                mono = indata.mean(axis=1) if indata.ndim > 1 else indata
                pcm = (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16)
                with self.file_lock:
                    if self.wav_file is None:
                        return
                    self.wav_file.writeframes(pcm.tobytes())
                    self.samples_written += len(pcm)
                    reached_limit = self.max_record_seconds > 0 and self.samples_written >= self.sample_rate * self.max_record_seconds
                now = time.time()
                if now - self.last_level_update >= 0.06:
                    level = min(1.0, float(np.sqrt(np.mean(np.square(mono.astype(np.float32))))) * 9.0)
                    write_status("recording", self.config, level=level, elapsed_seconds=now - self.started_at)
                    self.last_level_update = now
                if reached_limit:
                    threading.Thread(target=lambda: self.toggle(self.paste_when_done), daemon=True).start()

            self.stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="float32",
                device=self.device,
                callback=callback,
            )
            self.stream.start()
            self.recording = True
            write_status("recording", self.config, elapsed_seconds=0.0)
            notify(f"Recording. Input: {self.device_label}", self.config)
        except Exception:
            self._close_capture()
            if self.audio_path is not None:
                safe_unlink(self.audio_path)
                self.audio_path = None
            self.restore_system_audio()
            raise

    def _close_capture(self) -> None:
        stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        with self.file_lock:
            wav_file, self.wav_file = self.wav_file, None
            if wav_file is not None:
                try:
                    wav_file.close()
                except Exception:
                    pass

    def stop_locked(self, paste: bool) -> None:
        self.recording = False
        audio_path, self.audio_path = self.audio_path, None
        self._close_capture()
        duration = self.samples_written / self.sample_rate if self.sample_rate else 0.0
        if audio_path is None or self.samples_written == 0 or duration < self.min_record_seconds:
            if audio_path is not None:
                safe_unlink(audio_path)
            self.restore_system_audio()
            write_status("idle", self.config)
            notify("No audio captured.", self.config)
            return

        self.restore_system_audio()
        self.processing = True
        write_status("processing", self.config, elapsed_seconds=duration)
        threading.Thread(target=self._process_background, args=(audio_path, paste), daemon=True).start()

    def _process_background(self, audio_path: Path, paste: bool) -> None:
        try:
            transcript = process_audio(audio_path, self.config, paste=paste)
            write_status("done", self.config)
            notify("Done: pasted." if paste else "Done: copied.", self.config)
            print(transcript, flush=True)
        except Exception as exc:
            write_run_log(audio_path, {"status": "failed", "audio": str(audio_path), "error": str(exc)}, self.config)
            if not retention_config(self.config).get("keep_failed_audio", True):
                safe_unlink(audio_path)
            write_status("failed", self.config, message=str(exc))
            notify(f"Failed: {exc}", self.config)
        finally:
            cleanup_runtime_files(self.config)
            with self.lock:
                self.processing = False
            threading.Timer(0.8, write_status, args=("idle", self.config)).start()


def list_devices() -> None:
    print(sd.query_devices())


def input_level(device: int | str | None, sample_rate: int, channels: int, seconds: float) -> tuple[float, float]:
    audio = sd.rec(max(1, int(sample_rate * seconds)), samplerate=sample_rate, channels=channels, dtype="float32", device=device)
    sd.wait()
    mono = audio.mean(axis=1) if audio.ndim > 1 else audio
    rms = float(np.sqrt(np.mean(np.square(mono.astype(np.float32)))))
    return rms, float(np.max(np.abs(mono))) if len(mono) else 0.0


def add_input_candidate(candidates: list[tuple[int | str | None, str]], seen: set[str], device: int | str | None, label: str) -> None:
    key = "system-default" if device is None else str(device)
    if key not in seen:
        seen.add(key)
        candidates.append((device, label))


def input_device_candidates(config: dict[str, Any]) -> list[tuple[int | str | None, str]]:
    candidates: list[tuple[int | str | None, str]] = []
    seen: set[str] = set()
    configured = config.get("audio_device")
    if configured is not None:
        add_input_candidate(candidates, seen, configured, f"configured device: {configured}")
    try:
        devices = sd.query_devices()
    except Exception:
        return candidates or [(None, "system default input")]
    if config.get("prefer_system_default_input", True):
        try:
            default_input = sd.query_devices(kind="input")
            index = int(default_input.get("index", -1))
            name = str(default_input.get("name", "system default input"))
            if index >= 0 and int(default_input.get("max_input_channels", 0)) > 0:
                add_input_candidate(candidates, seen, None, f"{name} (system default)")
                add_input_candidate(candidates, seen, index, f"{name} (default index)")
        except Exception:
            add_input_candidate(candidates, seen, None, "system default input")
    preferred = [name.lower() for name in config.get("preferred_input_names", [])]
    for index, device in enumerate(devices):
        if int(device.get("max_input_channels", 0)) > 0 and any(name in str(device.get("name", "")).lower() for name in preferred):
            add_input_candidate(candidates, seen, index, f"{device.get('name', index)} (preferred)")
    for index, device in enumerate(devices):
        if int(device.get("max_input_channels", 0)) > 0:
            add_input_candidate(candidates, seen, index, str(device.get("name", index)))
    return candidates or [(None, "system default input")]


def resolve_audio_device(config: dict[str, Any], validate: bool = False) -> tuple[int | str | None, str]:
    candidates = input_device_candidates(config)
    probe = config.get("input_probe", {})
    if not validate or not probe.get("enabled", True):
        return candidates[0]
    seconds = max(0.05, min(float(probe.get("seconds", 0.25)), 1.0))
    threshold = max(0.0, float(probe.get("threshold", 0.00001)))
    best, best_peak = candidates[0], -1.0
    for device, label in candidates:
        try:
            rms, peak = input_level(device, int(config.get("sample_rate", 16000)), int(config.get("channels", 1)), seconds)
        except Exception as exc:
            print(f"Input probe failed for {label}: {exc}", flush=True)
            continue
        if peak > best_peak:
            best, best_peak = (device, label), peak
        if rms >= threshold or peak >= threshold:
            return device, f"{label} (live input)"
    return best[0], f"{best[1]} (silent fallback)"


def run_signal_server(config: dict[str, Any]) -> None:
    ensure_runtime_dir()
    lock_file = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        notify("Voice Flow is already running.", config)
        return

    recorder = Recorder(config)
    write_status("idle", config)
    pending: deque[bool] = deque()
    event = threading.Event()

    def enqueue(paste: bool) -> None:
        pending.append(paste)
        event.set()

    def stop_server(*_: Any) -> None:
        recorder.restore_system_audio()
        raise KeyboardInterrupt

    signal.signal(signal.SIGUSR1, lambda *_: enqueue(True))
    signal.signal(signal.SIGUSR2, lambda *_: enqueue(False))
    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    notify(f"Ready. Input: {recorder.device_label}; Page Down records; Page Up copies only.", config)
    try:
        while True:
            event.wait()
            while pending:
                recorder.toggle(pending.popleft())
            event.clear()
    except KeyboardInterrupt:
        notify("Stopped.", config)
    finally:
        recorder.restore_system_audio()
        safe_unlink(PID_PATH)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Local raw voice dictation for Voice Flow.app.")
    parser.add_argument("--once", type=Path, help="Transcribe one existing audio file.")
    parser.add_argument("--no-paste", action="store_true", help="Copy but do not paste.")
    parser.add_argument("--no-clipboard", action="store_true", help="Print only.")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices.")
    parser.add_argument("--signal-server", action="store_true", help="Run the app worker.")
    args = parser.parse_args()
    if args.list_devices:
        list_devices()
        return 0
    config = load_config()
    if args.once:
        text = process_audio(args.once.resolve(), config, paste=not args.no_paste and not args.no_clipboard, copy=not args.no_clipboard)
        if args.no_clipboard:
            print(text)
        return 0
    if not args.signal_server:
        parser.error("Voice Flow.app starts this worker with --signal-server.")
    run_signal_server(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
