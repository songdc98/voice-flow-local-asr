# Exploration Log

This is the public-safe development log for Voice Flow. Raw runtime logs remain
in `logs/` on the developer machine but are ignored by Git because they may
contain private dictated text.

## Stage 1: Requirements

Problem:

- The target workflow required faster and more accurate speech input than normal
  chat-app dictation.
- Main language was Chinese, with frequent English technical terms.
- The app needed to support coding-agent prompts, especially Chinese speech to
  English prompt conversion for English-oriented coding tools.
- The workflow had to avoid paid APIs and run locally.
- The system had to be convenient enough for daily use with a wireless
  microphone.

Decision:

- Build a local macOS utility instead of a cloud service.
- Use a hotkey-controlled workflow rather than microphone hardware buttons.
- Keep the app lightweight at idle and run ML inference only after recording
  stops.

## Stage 2: Model Selection

Problem:

- Very small ASR models were quieter and faster but weaker on Chinese-English
  mixed technical speech.
- Larger ASR models improved accuracy but could make the machine noisy if used
  continuously.

Decision:

- Use `mlx-community/Qwen3-ASR-1.7B-8bit` as the balanced default.
- Use `mlx-community/Qwen3-4B-Instruct-2507-4bit` for local cleanup and
  translation.
- Preserve detail aggressively in prompts and remove only filler, pauses,
  obvious repetition, and obvious ASR mistakes.

Representative output:

- Chinese-English mixed speech preserved terms such as `Qwen ASR`, `Whisper`,
  `API`, `Transformer`, and `OpenAI`.
- Chinese coding requests stayed in Chinese for Codex-like contexts.
- Chinese speech became English coding prompts for Claude-like contexts.

## Stage 3: Interaction Workflow

Problem:

- Terminal-started scripts were inconvenient and visually noisy.
- The user wanted one app that keeps running in the background until quit.
- Command-based hotkeys were unreliable on the user's keyboard.

Decision:

- Package the workflow as `Voice Flow.app`.
- Use the Dock as the visible running indicator.
- Use `Page Down` to start/stop recording.
- Use `Page Up` for copy-only mode.
- Remove top menu bar presence.

Representative output:

- One app launch starts the native app, the PyObjC helper, and the Python
  signal-server worker.
- Quitting the app stops the background service.

## Stage 4: Microphone Workflow

Problem:

- Built-in microphone input was less reliable for daily dictation.
- The user purchased a DJI Mic Mini and wanted the app to select it
  automatically.

Decision:

- Add preferred input names in `config.json`.
- Auto-select devices whose names include `DJI Mic Mini`, `DJI Mic`, `Mic Mini`,
  or `DJI`.
- Keep keyboard hotkeys as the control surface because external microphone
  buttons are not reliable system-wide hotkeys on macOS.

Representative output:

- Logs showed the input device as `DJI MIC MINI (auto-selected)`.

## Stage 5: Desktop App Packaging

Problem:

- AppleScript launcher prototypes created separate start/stop apps and opened
  Terminal windows.
- That did not match normal app behavior.

Decision:

- Replace AppleScript launchers with a native Cocoa Dock app.
- Keep the app running until the user quits it.
- Hide Terminal completely.
- Add microphone permission handling in the native launcher.
- Add stable local signing support through `scripts/build_macos_app.sh`.

Representative output:

- A single `Voice Flow.app` appears in the Dock.
- No Terminal window is opened during normal use.

## Stage 6: Auto-Paste

Problem:

- Copying to the clipboard worked, but pasting into WeChat, Codex, and some chat
  inputs was unreliable.

Decision:

- Let the Python worker copy text and write a paste request.
- Let the native Cocoa app handle paste because it owns the Accessibility
  permission.
- First attempt `AXSelectedText` insertion.
- Fall back to a synthetic Command-V event.
- Write `runtime/paste_status.json` for diagnostics.

Representative output:

- Paste started working after Accessibility was granted to `Voice Flow.app`.
- `runtime/paste_status.json` records the last paste method and trust status.

## Stage 7: HUD and Visual Design

Problem:

- System notifications were too intrusive.
- Early HUD versions were too large, too centered, or visually rough.
- The user wanted a compact visual recording indicator.

Decision:

- Disable routine notifications.
- Add a small bottom-center HUD.
- Use a red/white capture-ball style background.
- Use a single black heartbeat line that is flat when quiet and moves with
  voice level.
- Keep the Dock icon separate from the transient HUD.

Representative output:

- Recording shows a small floating HUD near the bottom center.
- Idle state hides the HUD.

## Stage 8: File Cleanup and Release

Problem:

- Exploration left old FunASR GGUF binaries, temporary audio, prototype
  launchers, sample audio, and runtime state files.
- The public project needed a clean final structure.

Decision:

- Keep the final MLX/Qwen3 pipeline.
- Remove old FunASR runtime binaries and model downloads from the release tree.
- Keep raw local runtime logs on disk but exclude them from Git.
- Move transient PID, HUD status, paste requests, and paste diagnostics into an
  ignored `runtime/` directory.
- Publish a public-safe English exploration log.
- Add a one-command macOS installer.

Representative output:

- Project source shrank from roughly 2 GB to a small source tree, excluding the
  local `.venv`.
- Final release files are source code, macOS app source/assets, installer
  scripts, config, docs, and local-log placeholders.

## Stage 9: Voice Wake Prototype

Problem:

- The user wanted hands-free recording without moving from mouse to keyboard.
- Chinese stop command recognition was unstable in macOS command recognition.
- The stop command is spoken during recording, so it can appear in the ASR
  transcript.
- macOS shows system microphone or speech-recognition indicators while an app is
  listening.

Decision:

- Integrate the listener into `Voice Flow.app` instead of running a separate
  monitor script.
- Use macOS `NSSpeechRecognizer` for fixed commands so the wake layer saves no
  audio, templates, or trigger logs.
- Use English command phrases that the system recognizer handles more reliably:
  `siri`, `hey siri`, `siri over`, `siri stop`, and `stop siri`.
- Prefer `siri over` as the recommended stop command because `over` is a
  distinct two-syllable radio-style ending cue. Keep `siri out` as a semantic
  one-way-session backup, but avoid bare `over` or `out` to reduce false stops.
- Keep `Page Down` as the reliable fallback.
- Strip wake/stop control phrases at transcript boundaries after ASR and again
  after LLM post-processing.
- Add cleanup-only aliases such as `series stop` and `serious stop` because ASR
  can miswrite the spoken command `siri stop`.
- Document that macOS privacy indicators cannot be hidden by the app through
  normal APIs.

Representative output:

- `siri` wakes recording.
- `siri over` and `siri stop` stop recording more reliably than the Chinese
  `结束` command.
- Final pasted text removes boundary control phrases such as `Siri stop`.
