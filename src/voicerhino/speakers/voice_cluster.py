"""Online voice clustering: a stable speaker id for each finalized utterance.

Utterance audio -> sherpa-onnx speaker embedding (L2-normalized) -> cosine similarity
to running per-speaker centroids -> best match if >= threshold, else a new cluster.
Ids are never renumbered, so a caller can attach display names (e.g. Discord
usernames) with set_name(). Not thread-safe: call it from one (the pipeline) thread.

Tested: tests/test_voice_cluster.py + tools/eval_speakers.py on Windows-TTS speech (3
voices, 33 utterances, 50 orders): 100% pure speaker<->cluster mapping clean, 48 kHz
round trip, +-level, -20 dB pink/game noise and -10 dB pink noise; ~15 ms/utterance.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import sherpa_onnx

from ..asr.catalog import DEFAULT_SPEAKER_MODEL, SPEAKER_MODELS
from ..asr.download import Progress, ensure_download

# Cosine thresholds tuned by tools/eval_speakers.py. The models' similarity scales
# differ a lot, so pass the matching one when not using the default model.
SPEAKER_THRESHOLDS: dict[str, float] = {
    "titanet-small": 0.5,
    "wespeaker-resnet34": 0.8,
}

_TARGET_RMS = 0.1  # -20 dBFS, typical speech: utterances are scaled to it before embedding


def ensure_speaker_model(
    name: str = DEFAULT_SPEAKER_MODEL,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    """Return the local .onnx path of a SPEAKER_MODELS entry, downloading it once."""
    if name not in SPEAKER_MODELS:
        raise ValueError(f"unknown speaker model {name!r}; known: {', '.join(SPEAKER_MODELS)}")
    return ensure_download(SPEAKER_MODELS[name], progress=progress, cancel=cancel)


@dataclass
class Assignment:
    """Why assign() decided what it did (VoiceClusterer.last), for debugging and tuning.

    reason: "match" (joined the best cluster), "new" (opened a cluster), "nearest"
    (max_speakers reached, took the closest), "short" (None: under min_seconds or
    silent) or "unsure" (None: shorter than new_speaker_seconds and nothing matched).
    """

    cluster: int | None
    reason: str
    similarity: float  # cosine to the best centroid before any update; nan if none existed
    similarities: np.ndarray = field(repr=False)  # cosine to every centroid (index = id)
    seconds: float = 0.0  # utterance length
    embed_ms: float = 0.0  # time spent computing the embedding


class VoiceClusterer:
    """Assigns utterances to speaker clusters online (no look-ahead).

    threshold: cosine similarity needed to join a cluster. Below it, a new cluster is
        opened until max_speakers exist; after that the nearest one is used.
    min_seconds: shorter utterances aren't embedded at all; assign() returns None and
        the caller should keep the previous speaker.
    new_speaker_seconds: shorter utterances may join a cluster (if >= threshold) but
        never open one or move a centroid; if nothing matches they return None. Short
        clips score low against their own speaker, so this stops junk clusters.
    max_seconds: longer utterances are embedded from their middle part only, bounding
        the cost (embedding time is linear in length; accuracy saturates).
    history: a centroid is the running mean of its first `history` members, then an
        exponential moving average with weight 1/history (follows slow drift).
    """

    def __init__(
        self,
        model_path: str | Path,
        threshold: float = SPEAKER_THRESHOLDS[DEFAULT_SPEAKER_MODEL],
        max_speakers: int = 8,
        min_seconds: float = 0.5,
        num_threads: int = 1,
        provider: str = "cpu",
        *,
        new_speaker_seconds: float = 2.0,
        max_seconds: float = 6.0,
        history: int = 20,
    ) -> None:
        path = Path(model_path)
        if not path.is_file():
            raise FileNotFoundError(f"speaker model not found: {path} (see ensure_speaker_model)")
        if max_speakers < 1:
            raise ValueError("max_speakers must be >= 1")
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(path), num_threads=num_threads, provider=provider, debug=False
        )
        if not config.validate():
            raise ValueError(f"invalid speaker embedding model config: {path}")
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self.dim: int = self._extractor.dim
        self.threshold = float(threshold)
        self.max_speakers = int(max_speakers)
        self.min_seconds = float(min_seconds)
        self.new_speaker_seconds = float(new_speaker_seconds)
        self.max_seconds = float(max_seconds)
        self.history = max(1, int(history))
        self.last: Assignment | None = None
        self.reset()

    # -- state ---------------------------------------------------------------------

    def reset(self) -> None:
        """Forget all clusters and names (e.g. new call or audio source); ids restart at 0."""
        self._means = np.zeros((self.max_speakers, self.dim), np.float32)
        self._centroids = np.zeros((self.max_speakers, self.dim), np.float32)
        self._counts = np.zeros(self.max_speakers, np.int64)
        self._k = 0
        self._names: dict[int, str] = {}
        self.last = None

    @property
    def num_speakers(self) -> int:
        return self._k

    def counts(self) -> list[int]:
        """Utterances that shaped each cluster's centroid so far (index = cluster id)."""
        return [int(c) for c in self._counts[: self._k]]

    def set_name(self, cluster_id: int, name: str | None) -> None:
        """Attach a display name (e.g. a Discord username) to a cluster; None clears it."""
        if not 0 <= cluster_id < self._k:
            raise KeyError(f"no speaker cluster {cluster_id}")
        if name:
            self._names[cluster_id] = name
        else:
            self._names.pop(cluster_id, None)

    def name(self, cluster_id: int) -> str | None:
        return self._names.get(cluster_id)

    # -- embedding -----------------------------------------------------------------

    def embed(self, samples: np.ndarray, sample_rate: int = 16000) -> np.ndarray | None:
        """L2-normalized speaker embedding, or None if under min_seconds or silent.

        samples: float32 in [-1, 1], mono (frames,) or (frames, channels); any sample
        rate works (sherpa-onnx resamples) but 16 kHz avoids that cost.
        """
        x = np.asarray(samples, dtype=np.float32)
        if x.ndim == 2:
            x = x.mean(axis=1)
        x = x.reshape(-1)
        if x.size == 0 or x.size < self.min_seconds * sample_rate:
            return None
        limit = int(self.max_seconds * sample_rate)
        if self.max_seconds > 0 and x.size > limit:
            start = (x.size - limit) // 2
            x = x[start : start + limit]
        level = float(np.sqrt(np.mean(np.square(x))))
        if level < 1e-4:  # digital silence
            return None
        # Embeddings drift with input level (0.88 self-similarity at -20 dB, 0.76 at
        # -30 dB for titanet), so quiet and loud talkers are normalized to one level.
        x = x * (_TARGET_RMS / level)
        stream = self._extractor.create_stream()
        stream.accept_waveform(sample_rate, np.ascontiguousarray(x))
        stream.input_finished()
        if not self._extractor.is_ready(stream):
            return None
        v = np.asarray(self._extractor.compute(stream), dtype=np.float32)
        norm = float(np.linalg.norm(v))
        if not math.isfinite(norm) or norm < 1e-8:
            return None
        return v / norm

    def scores(self, embedding: np.ndarray) -> np.ndarray:
        """Cosine similarity of an embed() result to each cluster centroid (index = id)."""
        return self._centroids[: self._k] @ np.asarray(embedding, dtype=np.float32)

    # -- clustering ----------------------------------------------------------------

    def assign(self, samples: np.ndarray, sample_rate: int = 16000) -> int | None:
        """Cluster id (0, 1, ...) for one finalized utterance, or None if it's too short
        to tell (keep the previous speaker then). Details are left in self.last."""
        seconds = np.shape(samples)[0] / sample_rate if np.ndim(samples) else 0.0
        t0 = time.perf_counter()
        embedding = self.embed(samples, sample_rate)
        embed_ms = (time.perf_counter() - t0) * 1000.0
        if embedding is None:
            self.last = Assignment(None, "short", math.nan, np.zeros(0, np.float32), seconds, embed_ms)
            return None
        cluster = self.assign_embedding(embedding, seconds)
        assert self.last is not None
        self.last.embed_ms = embed_ms
        return cluster

    def assign_embedding(self, embedding: np.ndarray, seconds: float | None = None) -> int | None:
        """assign() for a precomputed embed() result; `seconds` is the utterance length
        (None = long enough to open a cluster). Lets tools sweep thresholds cheaply."""
        e = np.asarray(embedding, dtype=np.float32)
        sims = self.scores(e)
        best = int(np.argmax(sims)) if sims.size else -1
        best_sim = float(sims[best]) if sims.size else math.nan
        tentative = seconds is not None and seconds < self.new_speaker_seconds
        if sims.size and best_sim >= self.threshold:
            cluster, reason = best, "match"
            if not tentative:
                self._update(cluster, e)
        elif tentative:
            cluster, reason = None, "unsure"
        elif self._k < self.max_speakers:
            cluster, reason = self._k, "new"
            self._k += 1
            self._update(cluster, e)
        else:  # full: take the nearest, but don't blur its centroid with a poor match
            cluster, reason = best, "nearest"
        self.last = Assignment(cluster, reason, best_sim, sims, seconds if seconds is not None else math.nan)
        return cluster

    def _update(self, cluster: int, e: np.ndarray) -> None:
        self._counts[cluster] += 1
        n = min(int(self._counts[cluster]), self.history)
        mean = self._means[cluster]
        mean += (e - mean) / n
        norm = float(np.linalg.norm(mean))
        self._centroids[cluster] = mean / norm if norm > 1e-8 else e
