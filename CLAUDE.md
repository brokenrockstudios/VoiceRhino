# VoiceRhino

Standalone Windows live captions (Python 3.12, PySide6, sherpa-onnx) for deaf gamers who talk on
Discord. v1 is shipped and in use. **Read `HANDOFF.md` first** (state,
architecture, decisions, gotchas); `internal/tasks.md` is the local backlog and `internal/docs/RESEARCH.md`
explains the choices (`internal/` is gitignored, so it may be absent).

## Commands (this PC)

- uv isn't on PATH: use `python -m uv …`. Sync with `python -m uv sync --extra gpu --inexact` —
  without `--inexact` it strips the GPU extra and pytest.
- Run: `.venv\Scripts\voicerhino-gui.exe` (demo: `--file tests\_out\conversation.wav`);
  terminal captions: `.venv/Scripts/python -m voicerhino --console`.
- Tests: `.venv/Scripts/python -m pytest -q -p no:cacheprovider` (add
  `--ignore=tests/test_process_loopback.py` to skip the ones that play quiet tones).
- Benchmark: `.venv/Scripts/python tools/bench.py parakeet-tdt-0.6b-v2-int8 --live nemotron-streaming-en-160ms-int8`
  (add `--realtime` for latencies as seen on screen; the default hides decode time).
- Release zip: `git archive --format=zip --prefix=VoiceRhino/ -o VoiceRhino.zip HEAD`

## Rules

- Ask the user before downloading anything new (model or package): name the file, source and size.
- Timestamps are `time.monotonic()`. `AudioSource` callbacks must return fast (per-app capture
  drops audio if the GIL is held > 30 ms).
- New config fields need defaults; the loader ignores unknown and missing keys.
- Offscreen Qt needs `QT_QPA_PLATFORM=offscreen` and `QT_QPA_FONTDIR=C:\Windows\Fonts`.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
