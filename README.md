# Voice Flow

Voice Flow is a local macOS voice-input app for high-accuracy Chinese and
Chinese-English mixed dictation. It records with a hotkey, transcribes locally
with an MLX ASR model, optionally cleans or translates the transcript with a
local MLX language model, copies the final text to the clipboard, and pastes it
into the currently focused text box.

The default workflow is designed for coding assistants and chat apps:

- Press `Page Down` to start recording.
- Speak naturally, mainly in Chinese, with English technical terms when needed.
- Press `Page Down` again to stop.
- Voice Flow transcribes, cleans, copies, and pastes the text.
- Press `Page Up` for copy-only mode.
- While recording, the system output volume is temporarily ducked to make the
  recording state obvious and reduce background media noise.
- The HUD shows a live audio waveform and elapsed recording time.
- Experimental voice trigger mode can wake recording with `hey siri` / `siri` and stop
  with `结束`.

## One-Command Install

Requirements:

- macOS on Apple Silicon.
- Xcode Command Line Tools.
- Internet access for the first model and Python dependency download.

```bash
git clone https://github.com/songdc98/voice-flow-local-asr.git
cd voice-flow-local-asr
./scripts/install_macos.sh
```

The installer will:

1. Install `uv` if it is missing.
2. Create a local `.venv`.
3. Install Python dependencies.
4. Generate app icons.
5. Compile `Voice Flow.app`.
6. Install it to `~/Applications/Voice Flow.app`.
7. Sign the app locally.
8. Open the app.

macOS privacy permissions cannot be granted by a script. After the app opens,
grant:

- Microphone permission.
- Speech Recognition permission.
- Accessibility permission in System Settings -> Privacy & Security -> Accessibility.

## Daily Use

1. Open `~/Applications/Voice Flow.app`.
2. Keep it running in the Dock.
3. Click any target input box.
4. Press `Page Down` to record.
5. Press `Page Down` again to stop and paste.
6. Quit the app from the Dock when you want the background service to stop.

Voice Flow does not show a menu bar icon. The running indicator is the Dock dot.

## Default Models

ASR:

- `mlx-community/Qwen3-ASR-1.7B-8bit`

Post-processing:

- `mlx-community/Qwen3-4B-Instruct-2507-4bit`
- fallback: `mlx-community/Qwen3-1.7B-4bit`

The model files are downloaded by the MLX tooling on first use. They are not
committed to this repository.

## Output Modes

The default mode is `auto`.

Voice Flow inspects the active application or browser context and chooses a
post-processing style:

- Claude, Claude Code, Cursor, Windsurf, v0, and Lovable: faithful English coding prompt.
- Codex, ChatGPT, OpenAI, and unknown contexts: faithful Chinese cleanup.

Supported explicit modes:

- `auto`: choose by active app/window context.
- `code_prompt_en`: convert Chinese speech into a clean English coding prompt.
- `code_prompt_zh`: clean the transcript into a Chinese coding prompt.
- `polish_zh`: clean Chinese speech while preserving detail.
- `translate_en`: translate into natural English chat text.
- `bilingual`: output Chinese plus English.
- `raw`: ASR transcript only.

Edit `config.json` to change the default mode, hotwords, corrections, retention
policy, volume ducking, or model settings.

The default maximum recording length is `1200` seconds, or 20 minutes. Change
`max_record_seconds` if you need a shorter or longer safety limit.

The `audio_ducking` block controls the recording-time system volume reduction:

- `enabled`: turn the feature on or off.
- `factor`: target fraction of the current output volume while recording.
- `restore_on_stop`: restore the previous output volume when recording stops.

## Experimental Voice Trigger

The wake-word prototype is built into `Voice Flow.app`. It uses macOS command
recognition for two commands and does not save wake-word audio, templates, or
trigger logs:

- wake phrases: `hey siri`, `hei siri`, `siri`
- stop phrase: `结束`

The intended flow is:

1. Click the target text box.
2. Say `hey siri` or `siri`.
3. Wait for the HUD and volume ducking.
4. Speak the message.
5. Say `结束`.
6. Voice Flow stops recording, transcribes, cleans, copies, and pastes.

`Page Down` and `Page Up` remain available as reliable fallback controls. The
voice trigger is still experimental: speaker audio, room acoustics, microphone
placement, and macOS command-recognition behavior can affect false wakes and
missed detections.

## Key Modules

- `voice_flow.py`: recording, audio retention, ASR invocation, LLM
  post-processing, clipboard handling, paste requests, and CLI entry points.
- `voice_flow_menu_app.py`: PyObjC helper process that launches the worker,
  manages the lightweight recording HUD, and keeps the Python service alive.
- `macos/voice_flow_app_launcher.m`: native Cocoa Dock app. It requests
  microphone and speech-recognition permissions, registers Page Down/Page Up
  global hotkeys, listens for the two local voice commands, sends signals to
  the Python worker, watches paste requests, and performs Accessibility-based
  paste with a Command-V fallback.
- `macos/generate_voice_flow_icons.py`: creates the app icon and status assets
  from the checked-in source image.
- `scripts/install_macos.sh`: one-command local installer.
- `scripts/build_macos_app.sh`: rebuilds the macOS app bundle after source
  changes.
- `scripts/uninstall_macos.sh`: removes the installed app and local runtime
  state files.
- `docs/EXPLORATION_LOG.md`: public-safe development log summarizing model,
  workflow, UX, and packaging decisions.

## Technical Path

Voice Flow uses a small native macOS launcher and a Python ML worker instead of
a full Electron-style application.

The native app handles OS integration:

- Dock presence.
- Microphone permission.
- Speech Recognition permission for the two fixed voice commands.
- native Carbon global hotkeys.
- Accessibility paste.
- app lifecycle and quit behavior.

The Python worker handles the ML pipeline:

1. Record mono 16 kHz audio through `sounddevice`.
2. Save a temporary WAV file.
3. Run Qwen3-ASR through `mlx_audio.stt.generate`.
4. Apply local correction rules.
5. Run local LLM cleanup/translation through `mlx_lm`.
6. Copy the final text through `pyperclip`.
7. Ask the native app to paste into the focused field.
8. Keep transcript logs and clean old audio according to `config.json`.

This split keeps UI and permissions stable while leaving the ASR and language
processing easy to change.

## Storage and Privacy

- Converted text logs are kept locally in `logs/` when enabled.
- Raw runtime logs are ignored by Git because they can contain private dictated
  content and local paths.
- Temporary audio is written to `tmp/`.
- By default, only the newest three `voice_*.wav` files are retained.
- No paid cloud ASR or LLM API is used by the default pipeline.
- Hugging Face / MLX model downloads are still external network downloads on
  first use.

## Troubleshooting

If recording works but text does not paste:

1. Confirm Voice Flow is enabled in Accessibility.
2. Quit and reopen `Voice Flow.app`.
3. Click the target text box before pressing `Page Down`.
4. Check `paste_status.json` for the last paste method and Accessibility status.

If the app does not start:

```bash
./scripts/install_macos.sh
open "$HOME/Applications/Voice Flow.app"
```

If you changed source code and only need to rebuild the app bundle:

```bash
./scripts/build_macos_app.sh
```

## Publishing Your Fork

If you want to publish your own copy after editing:

```bash
gh auth login
./scripts/publish_github.sh
```

By default this creates or updates a public repository named
`voice-flow-local-asr` under the authenticated GitHub account. To publish under a
specific account or organization:

```bash
VOICE_FLOW_GITHUB_OWNER=your-github-name ./scripts/publish_github.sh
```

## Disclaimer

Voice Flow is an experimental local dictation utility. It may transcribe,
translate, clean, or paste text incorrectly. Always review generated text before
sending it, especially for code, legal, medical, financial, academic, or
security-sensitive content.

This project is not affiliated with Apple, DJI, OpenAI, Qwen, MLX, Hugging Face,
or any chat/coding product mentioned in the documentation. Model names and
product names belong to their respective owners.

The included icon is a generated project asset. Replace it before redistribution
if your use case requires a different branding or legal review.

## License

MIT License. See `LICENSE`.
