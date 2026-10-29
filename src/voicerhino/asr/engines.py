"""Speech recognizers behind one tiny interface: 16 kHz mono float32 in, text out."""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..paths import models_dir
from .catalog import ModelSpec
from .download import Progress, ensure_download

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000


@dataclass
class EngineOptions:
    num_threads: int = 2  # Parakeet int8 is ~RTF 0.04 on 2 Zen 5 threads; more barely helps
    device: str = "cpu"  # "cpu" or "cuda" (faster-whisper only for now)
    language: str = "en"  # "" = auto-detect where the model supports it
    task: str = "transcribe"  # Whisper only: "translate" gives English from any language


class Engine(ABC):
    def __init__(self, spec: ModelSpec, options: EngineOptions):
        self.spec = spec
        self.options = options
        self._lock = threading.Lock()  # one decode at a time per model

    @abstractmethod
    def _transcribe(self, samples: np.ndarray) -> str: ...

    def transcribe(self, samples: np.ndarray) -> str:
        if samples.size == 0:
            return ""
        with self._lock:
            text = self._transcribe(np.ascontiguousarray(samples, dtype=np.float32))
        return _clean(text)

    def close(self) -> None:
        pass


# Whisper invents these from noise/music; drop utterances that are only this.
_HALLUCINATIONS = {
    "thank you.", "thanks for watching!", "thank you for watching.", "you", "bye.",
    "thanks for watching.", "please subscribe.", ".",
}


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if text.lower() in _HALLUCINATIONS:
        return ""
    return text


def _pick(folder: Path, *patterns: str) -> str:
    """First file matching any glob pattern, preferring int8 variants."""
    for pattern in patterns:
        matches = sorted(folder.glob(pattern), key=lambda p: ("int8" not in p.name, p.name))
        if matches:
            return str(matches[0])
    raise FileNotFoundError(f"no {patterns} in {folder}")


class SherpaEngine(Engine):
    """Offline (non-streaming) sherpa-onnx recognizer; runs on CPU by default."""

    def __init__(self, spec: ModelSpec, options: EngineOptions, folder: Path):
        super().__init__(spec, options)
        import sherpa_onnx as so

        threads = max(1, options.num_threads)
        provider = "cuda" if options.device == "cuda" else "cpu"
        family = spec.family
        # Qwen3-ASR ships a tokenizer/ folder instead of tokens.txt.
        tokens = "" if family == "qwen3_asr" else _pick(folder, "tokens.txt", "*tokens.txt")
        if family == "nemo_transducer":
            self._rec = so.OfflineRecognizer.from_transducer(
                encoder=_pick(folder, "encoder*.onnx"),
                decoder=_pick(folder, "decoder*.onnx"),
                joiner=_pick(folder, "joiner*.onnx"),
                tokens=tokens,
                num_threads=threads,
                provider=provider,
                model_type="nemo_transducer",
            )
        elif family == "moonshine":
            self._rec = so.OfflineRecognizer.from_moonshine(
                preprocessor=_pick(folder, "preprocess*.onnx"),
                encoder=_pick(folder, "encode*.onnx"),
                uncached_decoder=_pick(folder, "uncached_decode*.onnx"),
                cached_decoder=_pick(folder, "cached_decode*.onnx"),
                tokens=tokens,
                num_threads=threads,
                provider=provider,
            )
        elif family == "sense_voice":
            self._rec = so.OfflineRecognizer.from_sense_voice(
                model=_pick(folder, "model*.onnx"),
                tokens=tokens,
                num_threads=threads,
                provider=provider,
                language=options.language if options.language in {"zh", "en", "ja", "ko", "yue"} else "",
                use_itn=True,
            )
        elif family == "qwen3_asr":
            # An LLM decoder: always detects the language itself (it takes no language option).
            self._rec = so.OfflineRecognizer.from_qwen3_asr(
                conv_frontend=_pick(folder, "conv_frontend*.onnx"),
                encoder=_pick(folder, "encoder*.onnx"),
                decoder=_pick(folder, "decoder*.onnx"),
                tokenizer=str(folder / "tokenizer"),
                num_threads=threads,
                provider=provider,
            )
        elif family == "whisper":
            self._rec = so.OfflineRecognizer.from_whisper(
                encoder=_pick(folder, "*encoder*.onnx"),
                decoder=_pick(folder, "*decoder*.onnx"),
                tokens=tokens,
                language=options.language,
                task=options.task,
                num_threads=threads,
                provider=provider,
            )
        else:
            raise ValueError(f"unsupported sherpa-onnx family {family!r}")

    def _transcribe(self, samples: np.ndarray) -> str:
        stream = self._rec.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        self._rec.decode_stream(stream)
        return stream.result.text


def _add_cuda_dll_dirs() -> None:
    """Let CTranslate2 find cuBLAS/cuDNN installed as nvidia-* pip wheels."""
    if sys.platform != "win32":
        return
    base = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    for sub in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc"):
        bin_dir = base / sub / "bin"
        if bin_dir.is_dir():
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


class FasterWhisperEngine(Engine):
    """Whisper via CTranslate2. GPU path; also runs (slowly) on CPU."""

    def __init__(self, spec: ModelSpec, options: EngineOptions):
        super().__init__(spec, options)
        _add_cuda_dll_dirs()
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # optional extra
            raise RuntimeError("faster-whisper isn't installed; run: uv sync --extra gpu") from exc
        device = options.device if options.device in {"cpu", "cuda"} else "cpu"
        compute = "int8_float16" if device == "cuda" else "int8"
        self._model = WhisperModel(
            spec.source,
            device=device,
            compute_type=compute,
            cpu_threads=max(1, options.num_threads),
            download_root=str(models_dir() / "faster-whisper"),
        )

    def _transcribe(self, samples: np.ndarray) -> str:
        segments, _info = self._model.transcribe(
            samples,
            language=self.options.language or None,
            task=self.options.task,
            beam_size=1,
            vad_filter=False,  # our VAD already segmented the audio
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        return " ".join(s.text.strip() for s in segments)


class StreamingEngine:
    """True streaming recognizer for the live line (two-pass captions).

    Each utterance gets its own stream; feed() returns the whole text so far,
    which replaces the live line (it isn't an increment).
    """

    def __init__(self, spec: ModelSpec, folder: Path, num_threads: int = 1):
        import sherpa_onnx as so

        self.spec = spec
        self._lock = threading.Lock()
        self._rec = so.OnlineRecognizer.from_transducer(
            tokens=_pick(folder, "tokens.txt"),
            encoder=_pick(folder, "encoder*.onnx"),
            decoder=_pick(folder, "decoder*.onnx"),
            joiner=_pick(folder, "joiner*.onnx"),
            num_threads=max(1, num_threads),
            provider="cpu",
            decoding_method="greedy_search",
            enable_endpoint_detection=False,  # our VAD decides where lines end
        )

    def new_stream(self):
        return self._rec.create_stream()

    def feed(self, stream, samples: np.ndarray) -> str:
        with self._lock:
            stream.accept_waveform(SAMPLE_RATE, np.ascontiguousarray(samples, dtype=np.float32))
            while self._rec.is_ready(stream):
                self._rec.decode_stream(stream)
            return _clean(self._rec.get_result(stream))


def load_streaming_engine(spec: ModelSpec, num_threads: int = 1, progress: Progress | None = None,
                          cancel: threading.Event | None = None) -> StreamingEngine:
    folder = ensure_download(spec.source, progress, cancel)
    engine = StreamingEngine(spec, folder, num_threads)
    log.info("loaded live model %s (%d threads)", spec.id, num_threads)
    return engine


def load_engine(spec: ModelSpec, options: EngineOptions, progress: Progress | None = None,
                cancel: threading.Event | None = None) -> Engine:
    """Download the model if needed and build its engine (slow: call off the UI thread)."""
    if spec.family == "faster_whisper":
        return FasterWhisperEngine(spec, options)
    folder = ensure_download(spec.source, progress, cancel)
    engine = SherpaEngine(spec, options, folder)
    log.info("loaded %s (%s, %d threads)", spec.id, options.device, options.num_threads)
    return engine
