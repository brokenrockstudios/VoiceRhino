"""Models VoiceRhino knows how to download and run.

`family` picks the engine loader in asr/engines.py. sherpa-onnx models are
archives from the k2-fsa/sherpa-onnx GitHub releases; faster-whisper models are
Hugging Face repo ids fetched by faster-whisper itself.
"""

from __future__ import annotations

from dataclasses import dataclass

SHERPA_ASR = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"


@dataclass(frozen=True)
class ModelSpec:
    id: str  # stable key stored in config.json
    name: str  # dropdown label
    family: str  # loader key, see engines.py
    languages: str  # short human description
    size_mb: int  # approximate download size
    source: str  # archive URL (sherpa) or HF repo id (faster-whisper)
    notes: str = ""
    gpu_recommended: bool = False
    can_translate: bool = False  # Whisper task="translate" works (not turbo/distil: never trained for it)
    tagline: str = ""  # the trade-off in plain words, shown in the live-text dropdown


ASR_MODELS: list[ModelSpec] = [
    ModelSpec(
        id="parakeet-tdt-0.6b-v2-int8",
        name="NVIDIA Parakeet TDT 0.6B v2 (English, CPU) - recommended",
        family="nemo_transducer",
        languages="English",
        size_mb=640,
        source=SHERPA_ASR + "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2",
        notes="More accurate than Whisper Large v3 Turbo on English, runs on CPU: zero GPU use.",
    ),
    ModelSpec(
        id="parakeet-unified-en-0.6b-int8",
        name="NVIDIA Parakeet Unified EN 0.6B (English, CPU) - newer, try vs v2",
        family="nemo_transducer",
        languages="English",
        size_mb=640,
        source=SHERPA_ASR + "sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-non-streaming.tar.bz2",
        notes="April 2026 successor; NVIDIA reports slightly lower error than v2. Same speed.",
    ),
    ModelSpec(
        id="parakeet-tdt-0.6b-v3-int8",
        name="NVIDIA Parakeet TDT 0.6B v3 (25 European languages, CPU)",
        family="nemo_transducer",
        languages="English + 24 European languages (auto-detect)",
        size_mb=640,
        source=SHERPA_ASR + "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2",
    ),
    ModelSpec(
        id="moonshine-base-en-int8",
        name="Moonshine Base (English, very light CPU)",
        family="moonshine",
        languages="English",
        size_mb=180,
        source=SHERPA_ASR + "sherpa-onnx-moonshine-base-en-int8.tar.bz2",
        notes="Lower accuracy than Parakeet, but tiny CPU cost.",
    ),
    ModelSpec(
        id="moonshine-tiny-en-int8",
        name="Moonshine Tiny (English, lightest)",
        family="moonshine",
        languages="English",
        size_mb=100,
        source=SHERPA_ASR + "sherpa-onnx-moonshine-tiny-en-int8.tar.bz2",
    ),
    ModelSpec(
        id="sense-voice-int8",
        name="SenseVoice Small (Chinese/English/Japanese/Korean/Cantonese, CPU)",
        family="sense_voice",
        languages="zh, en, ja, ko, yue",
        size_mb=160,
        source=SHERPA_ASR + "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
    ),
    ModelSpec(
        id="qwen3-asr-0.6b-int8",
        name="Qwen3-ASR 0.6B (30 languages incl. Spanish, Chinese, Korean, Japanese, CPU)",
        family="qwen3_asr",
        languages="30 languages + 22 Chinese dialects (auto-detect)",
        size_mb=940,
        source=SHERPA_ASR + "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2",
        notes="For non-English speech. About 2.5x Parakeet's CPU use (~1.3 cores while people "
              "talk); lines finish ~0.4 s later.",
    ),
    ModelSpec(
        id="fw-large-v3-turbo",
        name="Whisper Large v3 Turbo (GPU, faster-whisper)",
        family="faster_whisper",
        languages="99 languages",
        size_mb=1600,
        source="mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        notes="OpenAI's Whisper, the well-known baseline. Needs the 'gpu' extra; uses ~2 GB VRAM.",
        gpu_recommended=True,
    ),
    ModelSpec(
        id="fw-distil-large-v3.5",
        name="Distil-Whisper Large v3.5 (English, GPU, faster-whisper)",
        family="faster_whisper",
        languages="English",
        size_mb=1500,
        source="distil-whisper/distil-large-v3.5-ct2",
        gpu_recommended=True,
    ),
    ModelSpec(
        id="fw-medium",
        name="Whisper Medium (GPU, can translate X->English)",
        family="faster_whisper",
        languages="99 languages; task=translate supported",
        size_mb=1500,
        source="Systran/faster-whisper-medium",
        gpu_recommended=True,
        can_translate=True,
    ),
    ModelSpec(
        id="fw-large-v3",
        name="Whisper Large v3 (GPU, best Whisper translation X->English)",
        family="faster_whisper",
        languages="99 languages; task=translate supported",
        size_mb=3100,
        source="Systran/faster-whisper-large-v3",
        notes="Heavier than Turbo (~3 GB VRAM) but, unlike Turbo, trained to translate.",
        gpu_recommended=True,
        can_translate=True,
    ),
]

# Optional streaming models for word-by-word live text (two-pass: they write words as they're
# spoken, then the main model above rewrites the finished line). Each one is a second model in
# memory; without one, the main model re-decodes the live line (cheaper, see DEFAULT_LIVE_MODEL_ID).
STREAMING_MODELS: list[ModelSpec] = [
    # The 80 ms variant was tried: no earlier first word on our test lines, twice the CPU.
    ModelSpec(
        id="nemotron-streaming-en-160ms-int8",
        name="NVIDIA Nemotron Streaming EN 160 ms",
        family="online_transducer",
        languages="English",
        size_mb=650,
        source=SHERPA_ASR + "sherpa-onnx-nemotron-speech-streaming-en-0.6b-160ms-int8-2026-04-25.tar.bz2",
        tagline="word-by-word, never rewrites; more CPU, +0.7 GB memory",
    ),
    ModelSpec(
        id="nemotron-streaming-en-560ms-int8",
        name="NVIDIA Nemotron Streaming EN 560 ms",
        family="online_transducer",
        languages="English",
        size_mb=650,
        source=SHERPA_ASR + "sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25.tar.bz2",
        tagline="word-by-word in bigger steps, ~0.4 s later, a bit more accurate; +0.7 GB memory",
    ),
]

# "" = re-decode the live line with the main model: no second model, the least CPU and memory,
# and the quickest first word (0.26 s vs 0.60 s for word-by-word); its live
# words can change as it hears more.
DEFAULT_LIVE_MODEL_ID = ""


def supports_live_model(spec: ModelSpec) -> bool:
    """The streaming live models are English-only, so pair them with English main models."""
    return spec.languages == "English"


VAD_MODELS: dict[str, str] = {
    "silero": SHERPA_ASR + "silero_vad.onnx",
}

# Speaker embedding models for voice colors (speakers/voice_cluster.py).
# Sic: the release tag really is spelled "recongition".
_SPEAKER_RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
SPEAKER_MODELS: dict[str, str] = {
    # 40 MB, 192-d. Best by far in tools/eval_speakers.py; ~6 ms per second of audio.
    "titanet-small": _SPEAKER_RELEASE + "nemo_en_titanet_small.onnx",
    # 27 MB, 256-d. 2.5x slower and worse in background noise, but separates very
    # similar voices better: an alternative if titanet merges two real people.
    "wespeaker-resnet34": _SPEAKER_RELEASE + "wespeaker_en_voxceleb_resnet34.onnx",
}
DEFAULT_SPEAKER_MODEL = "titanet-small"

DEFAULT_MODEL_ID = "parakeet-tdt-0.6b-v2-int8"


def get_model(model_id: str) -> ModelSpec:
    for spec in ASR_MODELS + STREAMING_MODELS:
        if spec.id == model_id:
            return spec
    raise KeyError(f"unknown model id {model_id!r}")
