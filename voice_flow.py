#!/usr/bin/env python3
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
from pynput import keyboard


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
TMP_DIR = BASE_DIR / "tmp"
LOG_DIR = BASE_DIR / "logs"
RUNTIME_DIR = BASE_DIR / "runtime"
LOCK_PATH = RUNTIME_DIR / "voice_flow.lock"
PID_PATH = RUNTIME_DIR / "voice_flow.pid"
STATUS_PATH = RUNTIME_DIR / "voice_flow_status.json"
PASTE_REQUEST_PATH = RUNTIME_DIR / "paste_request.json"


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


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(exist_ok=True)


def voice_trigger_config(config: dict[str, Any]) -> dict[str, Any]:
    defaults = {
        "enabled": True,
        "wake_phrases": ["hey siri", "hei siri", "siri"],
        "stop_phrases": [
            "siri over",
            "siri out",
            "siri stop",
            "stop siri",
            "stop recording",
            "done recording",
            "finish recording",
            "结束",
        ],
        "stop_phrase": "结束",
    }
    defaults.update(config.get("voice_trigger", {}))
    return defaults


def wake_control_phrases(trigger_config: dict[str, Any]) -> list[str]:
    return control_phrases(trigger_config, "wake_phrases", "wake_phrase", ["hey siri", "siri"])


def stop_control_phrases(trigger_config: dict[str, Any]) -> list[str]:
    return control_phrases(
        trigger_config,
        "stop_phrases",
        "stop_phrase",
        ["siri over", "siri out", "siri stop", "stop siri", "stop recording", "done recording", "finish recording", "结束"],
    )


def control_phrases(
    trigger_config: dict[str, Any],
    array_key: str,
    legacy_key: str,
    fallback: list[str],
) -> list[str]:
    phrases: list[str] = []
    configured = trigger_config.get(array_key, [])
    if isinstance(configured, list):
        phrases.extend(str(item) for item in configured if str(item).strip())
    legacy = str(trigger_config.get(legacy_key, "")).strip()
    if legacy:
        phrases.append(legacy)
    deduped: list[str] = []
    for phrase in phrases:
        if phrase not in deduped:
            deduped.append(phrase)
    return deduped or fallback


def phrase_to_edge_pattern(phrase: str) -> str:
    phrase = phrase.strip()
    ascii_tokens = phrase.split()
    if ascii_tokens and all(re.fullmatch(r"[A-Za-z0-9]+", token) for token in ascii_tokens):
        separator = r"[\s,，.。!！?？:：;；、\-_'\"]+"
        body = separator.join(re.escape(token) for token in ascii_tokens)
        return rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])"
    return rf"(?<![A-Za-z0-9\u4e00-\u9fff]){re.escape(phrase)}(?![A-Za-z0-9\u4e00-\u9fff])"


def strip_edge_control_phrases(text: str, phrases: list[str], side: str) -> str:
    phrases = [phrase.strip() for phrase in phrases if phrase.strip()]
    if not phrases:
        return text.strip()
    alternatives = "|".join(phrase_to_edge_pattern(phrase) for phrase in sorted(phrases, key=len, reverse=True))
    edge = r"[\s,，.。!！?？:：;；、\-_'\"]*"
    if side == "start":
        pattern = re.compile(rf"^{edge}(?:{alternatives}){edge}", flags=re.IGNORECASE)
    else:
        pattern = re.compile(rf"{edge}(?:{alternatives}){edge}$", flags=re.IGNORECASE)

    previous = None
    cleaned = text.strip()
    while cleaned and cleaned != previous:
        previous = cleaned
        cleaned = pattern.sub("", cleaned).strip()
    return cleaned


def strip_voice_control_phrases(text: str, config: dict[str, Any]) -> str:
    trigger_config = voice_trigger_config(config)
    cleaned = strip_edge_control_phrases(text, stop_control_phrases(trigger_config), "end")
    return strip_edge_control_phrases(cleaned, wake_control_phrases(trigger_config), "start")


def notify(message: str, config: dict[str, Any]) -> None:
    print(message, flush=True)
    if not config.get("notify", True):
        return
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{message.replace(chr(34), chr(39))}" with title "Voice Flow"',
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def write_status(
    state: str,
    config: dict[str, Any],
    level: float = 0.0,
    message: str = "",
    elapsed_seconds: float | None = None,
) -> None:
    if not config.get("hud", {}).get("enabled", True):
        return
    payload = {
        "state": state,
        "level": max(0.0, min(float(level), 1.0)),
        "message": message,
        "updated_at": time.time(),
    }
    if elapsed_seconds is not None:
        payload["elapsed_seconds"] = max(0.0, float(elapsed_seconds))
    tmp_path = STATUS_PATH.with_suffix(".tmp")
    try:
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        tmp_path.replace(STATUS_PATH)
    except Exception:
        pass


def apple_script_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


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
    volume = max(0, min(int(volume), 100))
    run_osascript(f"set volume output volume {volume}", timeout=1.0)


def get_active_context() -> dict[str, str]:
    app = run_osascript('tell application "System Events" to get name of first application process whose frontmost is true')
    title = ""
    url = ""

    chromium_apps = {
        "Google Chrome",
        "Chromium",
        "Microsoft Edge",
        "Brave Browser",
        "Arc",
    }
    if app in chromium_apps:
        quoted = apple_script_string(app)
        url = run_osascript(f"tell application {quoted} to get URL of active tab of front window")
        title = run_osascript(f"tell application {quoted} to get title of active tab of front window")
    elif app == "Safari":
        url = run_osascript('tell application "Safari" to get URL of front document')
        title = run_osascript('tell application "Safari" to get name of front document')

    if app and not title:
        quoted = apple_script_string(app)
        title = run_osascript(
            f'tell application "System Events" to tell process {quoted} to get name of front window'
        )

    return {"app": app, "title": title, "url": url}


def normalize_mode(mode: str) -> str:
    if mode == "code_prompt":
        return "code_prompt_en"
    return mode


def resolve_output_mode(config: dict[str, Any], mode: str | None = None) -> tuple[str, dict[str, str]]:
    requested = normalize_mode(mode or config.get("output_mode", "translate_en"))
    if requested != "auto":
        return requested, {}

    context = get_active_context()
    return match_context_mode(config, context), context


def match_context_mode(config: dict[str, Any], context: dict[str, str]) -> str:
    haystack = " ".join(context.values()).lower()
    for rule in config.get("context_rules", []):
        for pattern in rule.get("match_any", []):
            if pattern.lower() in haystack:
                return normalize_mode(rule.get("output_mode", config.get("fallback_output_mode", "code_prompt_zh")))

    return normalize_mode(config.get("fallback_output_mode", "code_prompt_zh"))


def save_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    audio = np.clip(audio, -1.0, 1.0)
    pcm = (audio * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(pcm.tobytes())


def safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def cleanup_old_files(directory: Path, patterns: list[str], max_files: int, max_age_days: int | float) -> None:
    if not directory.exists():
        return
    files: dict[Path, float] = {}
    for pattern in patterns:
        for path in directory.glob(pattern):
            if path.is_file():
                try:
                    files[path] = path.stat().st_mtime
                except OSError:
                    pass
    if not files:
        return

    now = time.time()
    max_age_seconds = float(max_age_days) * 86400
    ordered = sorted(files.items(), key=lambda item: item[1], reverse=True)
    for index, (path, mtime) in enumerate(ordered):
        too_many = max_files >= 0 and index >= max_files
        too_old = max_age_seconds >= 0 and now - mtime > max_age_seconds
        if too_many or too_old:
            safe_unlink(path)


def cleanup_runtime_files(config: dict[str, Any]) -> None:
    retention = retention_config(config)
    audio_limit = int(retention.get("max_audio_files", 3)) if retention.get("keep_audio", True) else 0
    cleanup_old_files(
        TMP_DIR,
        ["voice_*.wav"],
        audio_limit,
        retention.get("max_age_days", 7),
    )
    cleanup_old_files(
        TMP_DIR,
        ["asr_*.txt"],
        int(retention.get("max_tmp_files", 20)),
        retention.get("max_age_days", 7),
    )
    if retention.get("keep_logs", True):
        cleanup_old_files(
            LOG_DIR,
            ["*.json"],
            int(retention.get("max_logs", 50)),
            retention.get("max_age_days", 7),
        )
    else:
        cleanup_old_files(LOG_DIR, ["*.json"], 0, 0)


def write_run_log(audio_path: Path, payload: dict[str, Any], config: dict[str, Any]) -> None:
    if not retention_config(config).get("keep_logs", True):
        return
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"{audio_path.stem}.json"
    log_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_asr(audio_path: Path, config: dict[str, Any], profile: str | None = None) -> str:
    profile = profile or config["asr_profile"]
    asr_cfg = config["asr"][profile]
    engine = asr_cfg["engine"]
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")

    if engine == "qwen3":
        out_base = TMP_DIR / f"asr_{audio_path.stem}_{uuid.uuid4().hex[:8]}"
        context = "中文为主，中英混杂。优先正确识别这些术语：" + ", ".join(config.get("hotwords", [])) + "。"
        cmd = [
            str(BASE_DIR / ".venv/bin/python"),
            "-m",
            "mlx_audio.stt.generate",
            "--model",
            asr_cfg["model"],
            "--audio",
            str(audio_path),
            "--output-path",
            str(out_base),
            "--format",
            "txt",
            "--language",
            asr_cfg.get("language", "zh"),
            "--max-tokens",
            str(asr_cfg.get("max_tokens", 1024)),
            "--chunk-duration",
            str(asr_cfg.get("chunk_duration", 30)),
            "--context",
            context,
        ]
        subprocess.run(
            cmd,
            cwd=BASE_DIR,
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=float(config.get("asr_timeout_seconds", 300)),
        )
        out_path = out_base.with_suffix(".txt")
        if not out_path.exists():
            out_path = Path(str(out_base) + ".txt")
        transcript = out_path.read_text(encoding="utf-8").strip()
        safe_unlink(out_path)
        return transcript

    raise ValueError(f"Unknown ASR engine: {engine}")


def build_prompt(transcript: str, config: dict[str, Any], mode: str) -> str:
    hotwords = ", ".join(config.get("hotwords", []))
    detail_level = config.get("detail_preservation", "high")
    trigger_config = voice_trigger_config(config)
    wake_phrases = "、".join(wake_control_phrases(trigger_config))
    stop_phrases = "、".join(stop_control_phrases(trigger_config))
    if mode == "raw":
        return transcript
    if mode == "polish_zh":
        task = "把语音识别稿做忠实清理，输出接近原话的中文文本。只去掉语气词、无意义停顿、明显重复和明显 ASR 错词；保留说话顺序、语气、约束、例子和中英文夹杂内容。"
    elif mode == "bilingual":
        task = "先给出整理后的中文，再给出自然英文翻译。格式固定为两行：中文：... 英文：... 两种语言都要保留原始细节。"
    elif mode == "code_prompt_zh":
        task = "把语音识别稿做忠实清理，输出接近原话的中文 coding 输入。只去掉语气词、无意义停顿、明显重复和明显 ASR 错词；保留说话顺序、语气、约束、例子和中英文夹杂内容。"
    elif mode in {"code_prompt", "code_prompt_en"}:
        task = "把语音识别稿忠实翻译成英文 coding 输入。只去掉语气词、无意义停顿、明显重复和明显 ASR 错词；保留说话顺序、语气、约束、例子和中英文夹杂中的技术术语。"
    else:
        task = "把语音识别稿完整翻译成自然、简洁的英文聊天消息。不要保留中文短语，专有名词除外。"
    return f"""你是本地语音输入的后处理器。
任务：{task}
要求：
- 只输出最终可粘贴文本，不解释，不加标题。
- 根据上下文修正明显 ASR 错误。
- 技术术语拼写参考：{hotwords}
- 中英文夹杂时保留常见英文术语的标准写法。
- 术语表只用于拼写纠错；如果原文没有说到某个术语、框架、语言、输出格式或指标，绝对不要新增。
- 如果模式要求英文输出，必须把普通中文翻译成英文；如果模式要求中文输出，保留中文表达。
- 细节保留等级：{detail_level}。不要总结，不要压缩成任务清单，不要替用户规划方案；保留用户明确说出的目标、约束、原因、担忧、例子、对比、偏好、否定条件和不确定性。
- 可以合并相邻的重复短句，但不能删除任何实质性信息。
- 可以整理标点和分段，但不要改变原意和信息顺序。
- 如果语音稿开头或结尾包含语音控制词“{wake_phrases}”或“{stop_phrases}”，删除这些控制词，不要把它们当作用户正文。
- 严禁添加用户没有明确说出的实现语言、框架、测试指标、JSON 格式、API 设计、验收标准、实验计划或新需求。

语音识别稿：
{transcript}
"""


def apply_corrections(text: str, config: dict[str, Any]) -> str:
    for wrong, right in config.get("corrections", {}).items():
        text = re.sub(re.escape(wrong), right, text, flags=re.IGNORECASE)
    return text


def run_llm(text: str, config: dict[str, Any], mode: str | None = None) -> str:
    mode = normalize_mode(mode or config.get("output_mode", "translate_en"))
    if mode == "auto":
        mode, _ = resolve_output_mode(config, mode)
    if mode == "raw" or not config.get("llm", {}).get("enabled", True):
        return text.strip()

    llm_cfg = config["llm"]
    prompt = build_prompt(text, config, mode)
    models = [llm_cfg["model"]]
    fallback = llm_cfg.get("fallback_model")
    if fallback and fallback not in models:
        models.append(fallback)

    last_error = None
    for model in models:
        cmd = [
            str(BASE_DIR / ".venv/bin/python"),
            "-m",
            "mlx_lm",
            "generate",
            "--model",
            model,
            "--prompt",
            "-",
            "--max-tokens",
            str(llm_cfg.get("max_tokens", 320)),
            "--temp",
            str(llm_cfg.get("temperature", 0)),
            "--verbose",
            "False",
        ]
        try:
            result = subprocess.run(
                cmd,
                cwd=BASE_DIR,
                input=prompt,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=float(llm_cfg.get("timeout_seconds", config.get("llm_timeout_seconds", 120))),
            )
            output = result.stdout.strip()
            return strip_chatty_prefix(output) or text.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            last_error = exc.stderr or str(exc)

    raise RuntimeError(f"LLM postprocess failed: {last_error}")


def strip_chatty_prefix(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:text)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip().strip('"')


def copy_and_maybe_paste(text: str, paste: bool, copy: bool = True) -> None:
    if not copy:
        return
    pyperclip.copy(text)
    if not paste:
        return
    if os.environ.get("VOICE_FLOW_NATIVE_PASTE") == "1":
        ensure_runtime_dir()
        payload = {
            "id": uuid.uuid4().hex,
            "updated_at": time.time(),
        }
        tmp_path = PASTE_REQUEST_PATH.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        tmp_path.replace(PASTE_REQUEST_PATH)
        return
    time.sleep(0.15)
    controller = keyboard.Controller()
    with controller.pressed(keyboard.Key.cmd):
        controller.press("v")
        controller.release("v")


def process_audio(
    audio_path: Path,
    config: dict[str, Any],
    paste: bool,
    profile: str | None,
    mode: str | None,
    copy: bool = True,
    active_context: dict[str, str] | None = None,
) -> str:
    start = time.time()
    resolved_mode, detected_context = resolve_output_mode(config, mode)
    context = active_context if active_context is not None else detected_context
    transcript = run_asr(audio_path, config, profile=profile)
    transcript = apply_corrections(transcript, config)
    transcript = strip_voice_control_phrases(transcript, config)
    final_text = run_llm(transcript, config, mode=resolved_mode)
    final_text = strip_voice_control_phrases(final_text, config)
    copy_and_maybe_paste(final_text, paste, copy=copy)

    write_run_log(
        audio_path,
        {
            "status": "ok",
            "audio": str(audio_path),
            "profile": profile or config["asr_profile"],
            "requested_mode": mode or config.get("output_mode", "translate_en"),
            "resolved_mode": resolved_mode,
            "active_context": context,
            "transcript": transcript,
            "final_text": final_text,
            "seconds": round(time.time() - start, 3),
        },
        config,
    )
    cleanup_runtime_files(config)
    return final_text


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
        self.current_mode = ""
        self.current_context: dict[str, str] = {}
        self.original_output_volume: int | None = None
        self.ducked_output_volume: int | None = None
        self.max_record_seconds = float(config.get("max_record_seconds", 180))
        self.min_record_seconds = float(config.get("min_record_seconds", 0.35))
        ensure_runtime_dir()
        TMP_DIR.mkdir(exist_ok=True)
        cleanup_runtime_files(config)

    def toggle(self, paste: bool) -> None:
        with self.lock:
            if self.processing:
                notify("Still processing previous recording.", self.config)
                return
            if self.recording:
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
        factor = max(0.0, min(float(ducking.get("factor", 0.5)), 1.0))
        target = int(round(current * factor))
        if current > 0:
            target = max(int(ducking.get("min_nonzero_volume", 1)), target)
        target = min(current, max(0, min(target, 100)))
        self.original_output_volume = current
        self.ducked_output_volume = target
        if target != current:
            set_system_output_volume(target)

    def restore_system_audio(self) -> None:
        ducking = self.config.get("audio_ducking", {})
        original = self.original_output_volume
        self.original_output_volume = None
        self.ducked_output_volume = None
        if original is None or not ducking.get("restore_on_stop", True):
            return
        set_system_output_volume(original)

    def start_locked(self, paste: bool) -> None:
        self.paste_when_done = paste
        self.samples_written = 0
        self.started_at = time.time()
        self.current_mode, self.current_context = resolve_output_mode(self.config, None)
        self.duck_system_audio()
        try:
            self.audio_path = TMP_DIR / f"voice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
            self.wav_file = wave.open(str(self.audio_path), "wb")
            self.wav_file.setnchannels(1)
            self.wav_file.setsampwidth(2)
            self.wav_file.setframerate(self.sample_rate)

            def callback(indata: np.ndarray, frames: int, time_info: Any, status: sd.CallbackFlags) -> None:
                if status:
                    print(f"Audio warning: {status}", flush=True)
                mono = indata
                if mono.ndim > 1:
                    mono = mono.mean(axis=1)
                pcm = (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16)
                should_stop = False
                with self.file_lock:
                    wav_file = self.wav_file
                    if wav_file is None:
                        return
                    wav_file.writeframes(pcm.tobytes())
                    self.samples_written += len(pcm)
                    should_stop = self.max_record_seconds > 0 and self.samples_written >= self.sample_rate * self.max_record_seconds
                now = time.time()
                if now - self.last_level_update >= 0.06:
                    rms = float(np.sqrt(np.mean(np.square(mono.astype(np.float32)))))
                    level = min(1.0, rms * 9.0)
                    write_status(
                        "recording",
                        self.config,
                        level=level,
                        elapsed_seconds=now - self.started_at,
                    )
                    self.last_level_update = now
                if should_stop:
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
            write_status("recording", self.config, level=0.0, elapsed_seconds=0.0)
            notify(f"Recording as {self.current_mode}...", self.config)
        except Exception:
            stream = self.stream
            self.stream = None
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            with self.file_lock:
                wav_file = self.wav_file
                self.wav_file = None
                if wav_file is not None:
                    try:
                        wav_file.close()
                    except Exception:
                        pass
            if self.audio_path is not None:
                safe_unlink(self.audio_path)
                self.audio_path = None
            self.restore_system_audio()
            raise

    def stop_locked(self, paste: bool) -> None:
        stream = self.stream
        self.stream = None
        self.recording = False
        audio_path = self.audio_path
        self.audio_path = None
        if stream is not None:
            stream.stop()
            stream.close()
        with self.file_lock:
            wav_file = self.wav_file
            self.wav_file = None
            if wav_file is not None:
                wav_file.close()

        duration = self.samples_written / self.sample_rate if self.sample_rate else 0
        if not audio_path or self.samples_written == 0 or duration < self.min_record_seconds:
            if audio_path:
                safe_unlink(audio_path)
            self.restore_system_audio()
            write_status("idle", self.config)
            notify("No audio captured.", self.config)
            return

        self.restore_system_audio()
        self.processing = True
        write_status("processing", self.config, level=0.0, elapsed_seconds=duration)
        notify(f"Processing as {self.current_mode}...", self.config)
        threading.Thread(
            target=self._process_background,
            args=(audio_path, paste, self.current_mode, dict(self.current_context)),
            daemon=True,
        ).start()

    def _process_background(
        self,
        audio_path: Path,
        paste: bool,
        mode: str,
        active_context: dict[str, str],
    ) -> None:
        try:
            final_text = process_audio(
                audio_path,
                self.config,
                paste=paste,
                profile=None,
                mode=mode,
                active_context=active_context,
            )
            action = "pasted" if paste else "copied"
            write_status("done", self.config, level=0.0)
            notify(f"Done: {action}.", self.config)
            print(final_text, flush=True)
        except Exception as exc:
            write_run_log(
                audio_path,
                {
                    "status": "failed",
                    "audio": str(audio_path),
                    "error": str(exc),
                    "seconds": None,
                },
                self.config,
            )
            if not retention_config(self.config).get("keep_failed_audio", True):
                safe_unlink(audio_path)
            write_status("failed", self.config, level=0.0, message=str(exc))
            notify(f"Failed: {exc}", self.config)
        finally:
            cleanup_runtime_files(self.config)
            with self.lock:
                self.processing = False
            if STATUS_PATH.exists():
                threading.Timer(0.8, write_status, args=("idle", self.config, 0.0)).start()


def list_devices() -> None:
    print(sd.query_devices())


def resolve_audio_device(config: dict[str, Any]) -> tuple[int | str | None, str]:
    configured = config.get("audio_device")
    if configured is not None:
        return configured, f"configured device: {configured}"

    preferred_names = [name.lower() for name in config.get("preferred_input_names", [])]
    try:
        devices = sd.query_devices()
    except Exception:
        return None, "system default input"

    for index, device in enumerate(devices):
        if int(device.get("max_input_channels", 0)) <= 0:
            continue
        name = str(device.get("name", ""))
        lowered = name.lower()
        if any(preferred in lowered for preferred in preferred_names):
            return index, f"{name} (auto-selected)"

    return None, "system default input"


def ensure_accessibility_prompt() -> None:
    try:
        from ApplicationServices import (  # type: ignore
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )
    except Exception:
        return

    try:
        trusted = AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
    except Exception:
        return

    if not trusted:
        print(
            "Voice Flow needs Accessibility/Input Monitoring permission for global hotkeys.",
            flush=True,
        )


def run_hotkeys(config: dict[str, Any]) -> None:
    ensure_runtime_dir()
    lock_file = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        notify("Voice Flow is already running.", config)
        return

    recorder = Recorder(config)
    write_status("idle", config)
    record_hotkey = config.get("record_hotkey", "<cmd>+<shift>+<space>")
    copy_only_hotkey = config.get("copy_only_hotkey", "<cmd>+<shift>+c")

    def stop_hotkeys(*_: Any) -> None:
        recorder.restore_system_audio()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_hotkeys)
    signal.signal(signal.SIGINT, stop_hotkeys)

    notify(
        f"Ready. Input: {recorder.device_label}; paste hotkey: {record_hotkey}; copy-only: {copy_only_hotkey}",
        config,
    )
    ensure_accessibility_prompt()
    try:
        with keyboard.GlobalHotKeys(
            {
                record_hotkey: lambda: recorder.toggle(True),
                copy_only_hotkey: lambda: recorder.toggle(False),
            }
        ) as listener:
            listener.join()
    except KeyboardInterrupt:
        notify("Stopped.", config)
    finally:
        recorder.restore_system_audio()
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


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
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")

    pending: deque[bool] = deque()
    event = threading.Event()

    def enqueue(paste: bool) -> None:
        pending.append(paste)
        event.set()

    signal.signal(signal.SIGUSR1, lambda *_: enqueue(True))
    signal.signal(signal.SIGUSR2, lambda *_: enqueue(False))

    def stop_server(*_: Any) -> None:
        recorder.restore_system_audio()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)

    notify(
        f"Ready. Input: {recorder.device_label}; native paste hotkey: <page_down>; native copy-only: <page_up>",
        config,
    )
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
        try:
            PID_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Local hotkey voice input: record, transcribe, translate, copy/paste.")
    parser.add_argument("--once", type=Path, help="Process one existing audio file instead of starting hotkeys.")
    parser.add_argument("--profile", help="Override ASR profile from config.json.")
    parser.add_argument(
        "--mode",
        choices=["auto", "code_prompt", "code_prompt_en", "code_prompt_zh", "translate_en", "polish_zh", "bilingual", "raw"],
        help="Override output mode.",
    )
    parser.add_argument("--no-paste", action="store_true", help="Do not paste; only copy to clipboard.")
    parser.add_argument("--no-clipboard", action="store_true", help="Print only; do not copy or paste.")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit.")
    parser.add_argument("--signal-server", action="store_true", help="Use macOS app-native hotkeys via signals.")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return 0

    config = load_config()
    if args.profile:
        if args.profile not in config.get("asr", {}):
            parser.error(f"unknown ASR profile: {args.profile}")
        config["asr_profile"] = args.profile
    if args.mode:
        config["output_mode"] = args.mode

    if args.once:
        text = process_audio(
            args.once.resolve(),
            config,
            paste=not args.no_paste and not args.no_clipboard,
            profile=config["asr_profile"],
            mode=config["output_mode"],
            copy=not args.no_clipboard,
        )
        if args.no_clipboard:
            print(text)
        return 0

    if args.signal_server:
        run_signal_server(config)
    else:
        run_hotkeys(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
