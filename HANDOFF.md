# VoiceRhino — handoff

_Last updated 2026-10-29. Companions: `CLAUDE.md` (commands, rules), `internal/tasks.md` (open items) and
`internal/docs/RESEARCH.md` (benchmarks, why things are built this way); `internal/` is local-only._

## 1. State

Standalone Windows live-caption app (Python 3.12, PySide6, sherpa-onnx) for **deaf gamers who talk on
Discord**. **v1 is shipped
and in use**, typically with the "Default output" source (all PC audio). All original requirements are built (see the README), including offline translation and
speaker colors/names.

**Verified on this PC** (140 tests, 128 without the tone-playing loopback tests): pipeline accuracy and
latency on synthetic TTS speech, loopback capture, per-app capture of the real Discord, GUI end to end
(offscreen), zip install from a clean copy, Discord RPC handshake and error path.

**Not verified** (say so to the user):

- Discord **names** with a real account (needs their own developer app; `docs/discord-setup.md`).
- Translation beyond Opus-MT zh→English and Hy-MT2. The other Opus-MT repos were only checked to exist;
  Hy-MT2 speed was measured on this 9950X3D only.
- How accuracy feels on real Discord audio. The quiet-audio boost and the −55 dBFS warning were tuned
  on TTS turned down digitally, not on real volume settings.
- `run.bat`'s first-time uv + Python bootstrap (sandbox issue, §3); installs on other PCs suggest it's fine.
- Catalog models never downloaded here: Parakeet v3 / Unified, Moonshine, SenseVoice, Whisper Medium /
  Large v3 / Distil, Nemotron 560 ms. URLs and file patterns were checked, never loaded.

## 2. Layout and flow

```
src/voicerhino/
  __main__.py     CLI: GUI default; --console, --file, --list-devices, --list-models, --live
  config.py       dataclass config <-> %APPDATA%\VoiceRhino\config.json (tolerant loader, _migrate)
  paths.py        config/models/logs dirs (env overrides VOICERHINO_*)
  pipeline.py     CaptionProcessor (pure, sample-clocked) + CaptionPipeline (thread, resample, gap fill)
  transcript.py   Utterance + TranscriptStore (finals + live partial per source; listeners)
  audio/          AudioSource base, devices, wasapi (PyAudioWPatch), process_loopback (ctypes COM),
                  app_source (follow an exe), file_source, gain (AutoGain, levels)
  asr/            catalog (ModelSpecs), download (resumable), engines, storage (models on disk, safe delete)
  speakers/       voice_cluster (TitaNet-small), discord_rpc (named pipe), attribution (merge)
  translate/      worker + language guess, catalog, opus (CTranslate2), llama (hidden Hy-MT2 llama-server)
  ui/             app (Controller, tray, model loading), overlay, settings (7 tabs), winutil, theme
tests/            conftest builds synthetic speech with Windows TTS (tools/make_test_audio.py)
tools/            bench, eval_speakers, try_loopback, try_process_loopback, screenshot_overlays/_settings
```

User data (not in git): config and `discord_token.json` in `%APPDATA%\VoiceRhino`, models in
`%LOCALAPPDATA%\VoiceRhino\models`, technical logs in `...\logs` (never caption text).

```
AudioSource -> CaptionPipeline thread: resample to 16 kHz mono (soxr), fill gaps with silence on a wall clock
  CaptionProcessor: AutoGain -> Silero VAD segments (+0.25 s pre-roll from AudioHistory)
    live line:  re-decode the open segment every 0.4 s (default)  |  two-pass: Nemotron 160 ms chunks, append-only
    final line: main engine (Parakeet) decodes the VAD segment; speaker_fn labels it
  -> TranscriptStore.push(Utterance) -> Qt signal -> CaptionWindow / HistoryWindow (+ TranslationWorker)
```

- Partials and the final of one utterance share an `id`; a final with empty text clears the live line.
- Each source has a stable `SourceConfig.id` (`Utterance.source_id`); windows skip `ViewConfig.hidden_sources`
  and label lines with the source name only when 2+ sources run.
- `Controller` loads engines on a background thread, owns pipelines/windows/tray/hotkey, and runs a watchdog
  (every ~6 s reopen unplugged devices or a switched default output; every 2 s check `level_info()` and show
  "⚠ … is very quiet" via `OverlayWindow.set_warning`).
- Settings edits a deep copy and emits `applied(AppConfig)`; `Controller.apply_config` diffs old vs new.

## 3. Dev environment on this PC

Commands are in `CLAUDE.md`. Extras:

- The venv was made with an explicit interpreter, because `uv sync` can't create uv-managed Python inside
  the sandbox (junction error): `python -m uv venv --python <path to uv's cpython-3.12 python.exe under %APPDATA%\uv\python> .venv`
- `uv sync` fails with "Access is denied" on `voicerhino-gui.exe` while the app runs; close it first.
- Hardware: Ryzen 9 9950X3D, RTX 5090 (CTranslate2 int8_float16 works on sm_120), 96 GB RAM, one output
  (Arctis Nova Pro). Discord runs here. A VoiceRhino instance may be running during benchmarks and adds CPU load.
- `curl` probes of URLs/localhost were denied by the auto-mode classifier; models download through
  `asr.download.ensure_download`.
- Already approved and on disk: Parakeet v2 int8, Silero VAD, TitaNet-small (+ WeSpeaker, CAM++), Whisper
  Large v3 Turbo + CUDA libs, Nemotron streaming 160 ms and 80 ms (the 80 ms one, 633 MB, was rejected and
  isn't in the catalog; the user may delete it), Qwen3-ASR 0.6B, Opus-MT zh, Hy-MT2.
- Release bundle: `.gitattributes` keeps .bat/.txt CRLF and leaves HANDOFF.md/CLAUDE.md out of the zip. Models
  aren't bundled: ~640 MB downloaded on first run (+650 MB for word-by-word live text). If `VoiceRhino.zip` is
  locked by another program, build `VoiceRhino-<commit>.zip` and send the newest.

## 4. Decisions

- **Python, not C#/C++** (widest model choice). Approach: VAD + partial re-decode + final decode.
- **Parakeet v2 int8 on CPU** as main model: 2.1% WER, 88 ms final decode, 0.46 cores, 0 VRAM on the bench
  (Whisper Turbo GPU: 1.1% WER, 1.5 GB VRAM; Turbo on CPU: 5.4 s/line, unusable).
- **Live text defaults to re-decode** (`asr.live_model` = ""): first correct word 0.26 s vs 0.60 s for two-pass,
  fewer cores, no second model (+674 MB RAM, +650 MB download). Two-pass remains for readability (never rewrites
  shown words, silent on game noise). **Configs saved before then are still two-pass.** Nemotron is English-only
  (`supports_live_model()`). Faster two-pass first words were tried and rejected (table in internal/docs/RESEARCH.md).
- **VAD shortest speech 0.1 s** (was 0.25, which dropped short words like "Wait,"); `config._migrate` upgrades old configs.
- **Quiet-audio guard:** `AutoGain` targets −26 dBFS speech, up to +24 dB at 8 dB/s, only when the signal swings
  ≥10 dB (hum isn't pumped), never cuts. Median pre-boost level under `QUIET_DB` = −55 dBFS shows the overlay warning.
- **Speakers:** TitaNet-small, threshold 0.50, new speakers only from ≥2 s lines, level-normalized. Discord name
  first; voice clusters learn Discord names; an unsure short line stays unlabeled (wrong names are worse than none).
- **Discord names:** local RPC (`\\.\pipe\discord-ipc-N`), scopes rpc + rpc.voice.read + identify; AUTHORIZE sends
  `redirect_uri`; token cached and refreshed; own voice ignored.
- **Translation is offline only** ("nothing is ever sent" must stay true; third-party sending raises consent
  questions): Opus-MT X→English (~160 MB each, CTranslate2) or Hy-MT2 1.8B Q4_K_M via a hidden llama-server.
  Finals only, never audio. English and unsure lines are skipped.
- **No saved transcripts** (user's decision; recording laws need all-party consent). Captions live in
  `TranscriptStore` memory only. Don't add export/save back without the user deciding it.

## 5. Gotchas

- **Audio:** loopback delivers nothing while silent (hence gap filling). Per-app capture keeps ~30 ms queued, so
  holding the GIL longer drops audio (`dropped_frames`). "Process tree" = target plus direct children; level follows
  the Volume Mixer; needs build ≥ 20348; a dead PID still "captures" silence (psutil checks, `AppSource` re-attaches).
- **Fatal abort:** ctranslate2's first import overlapping sherpa-onnx loading or decoding aborted Python ("Fatal
  Python error: Aborted", no traceback). The Controller imports it first (`translate.preload_runtime`); exact cause unconfirmed.
- **Privacy:** llama-server output may contain caption text, so it's drained into memory, never logged; its HTTP
  calls bypass system proxies (`ProxyHandler({})`).
- **Models:** CAM++ is broken under sherpa-onnx 2.0.1 (not in the catalog). Whisper Turbo/Distil can't translate
  (`ModelSpec.can_translate`). sherpa-onnx ≥ 1.13.4 results have no leading spaces; Nemotron's `get_result` is the
  whole text so far.
- **Discord RPC:** ~2 connections/minute; the authorize popup is invisible in exclusive-fullscreen games.
- **Overlay/Qt:** only `x == y == -1` means auto-place (negative coords are real monitors). Tray changes made while
  Settings is open survive `apply_config`. Remove widgets with `setParent(None)` before `deleteLater()`, or they stay painted.
- **Benchmarks:** `bench.py` without `--realtime` hides decode time (two-pass looked like 0.46 s to first word, is 0.72 s
  on screen). Use `--realtime` for anything a user sees, on an idle machine. "First word" = first live line starting
  with the right word; "first text" counts anything. Nemotron 160 ms costs ~100 ms CPU per chunk; Parakeet partials from
  <0.3 s of speech often say "Mm-hmm." and invent words on game noise.
- **Tests:** `test_settings_dialog.py` hardcodes the tab list. Settings tests point `VOICERHINO_MODELS` at a temp
  folder (autouse); no test may touch the real models folder.
