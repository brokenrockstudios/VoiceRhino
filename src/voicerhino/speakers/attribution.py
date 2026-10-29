"""Who said each utterance: Discord names when available, voice clusters otherwise.

A SpeakerAttributor is the pipeline's speaker_fn: (audio, start, end) ->
(speaker_key, display_name) or None. Called from pipeline threads.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

import numpy as np

from ..config import SpeakerConfig

log = logging.getLogger(__name__)

# Discord raises "speaking" when packets arrive, a little before we hear them.
DISCORD_LEAD = 0.4
# A line too short/unsure to place on its own still counts as the previous
# speaker if it sounds at least this close to them (cosine, default model).
CONTINUE_SIMILARITY = 0.4


class SpeakerAttributor:
    def __init__(self, settings: SpeakerConfig, on_status: Callable[[str], None] | None = None):
        self._lock = threading.Lock()
        self._on_status = on_status
        self._voice = None
        self._voice_threshold: float | None = None
        self._discord = None
        self._discord_creds: tuple[str, str] | None = None
        self._timeline = None
        self._last_cluster: int | None = None
        self._cluster_keys: dict[int, str] = {}  # voice cluster -> Discord speaker key
        self.configure(settings)

    def configure(self, s: SpeakerConfig) -> None:
        with self._lock:
            self._configure_voice(s)
            stale = self._configure_discord(s)
        if stale is not None:  # stopping can take a few seconds; never under the lock
            stale.stop()

    def _configure_voice(self, s: SpeakerConfig) -> None:
        if not s.voice_enabled:
            self._voice = None
            return
        threshold = s.voice_threshold or None
        if self._voice is not None and threshold == self._voice_threshold:
            return
        from .voice_cluster import VoiceClusterer, ensure_speaker_model

        kwargs = {"threshold": threshold} if threshold else {}
        self._voice = VoiceClusterer(ensure_speaker_model(), **kwargs)
        self._voice_threshold = threshold
        self._cluster_keys.clear()
        self._last_cluster = None

    def _configure_discord(self, s: SpeakerConfig):
        """Start/replace the Discord client; returns a client that must be stopped."""
        creds = (s.discord_client_id.strip(), s.discord_client_secret.strip())
        wanted = s.discord_enabled and all(creds)
        stale = None
        if self._discord is not None and (not wanted or creds != self._discord_creds):
            stale, self._discord, self._discord_creds = self._discord, None, None
        if wanted and self._discord is None:
            from .discord_rpc import DiscordSpeakingClient, SpeakingTimeline

            self._timeline = SpeakingTimeline()
            self._discord = DiscordSpeakingClient(creds[0], creds[1], on_event=self._timeline.add,
                                                  on_status=self._discord_status)
            self._discord.start()
            self._discord_creds = creds
        return stale

    def _discord_status(self, text: str) -> None:
        if self._on_status is not None:
            self._on_status(text if text.lower().startswith("discord") else f"Discord: {text}")

    def close(self) -> None:
        with self._lock:
            client, self._discord = self._discord, None
        if client is not None:
            client.stop()

    def __call__(self, audio: np.ndarray, start: float, end: float) -> tuple[str, str] | None:
        with self._lock:
            discord = self._from_discord(start, end)
            cluster = None
            if self._voice is not None:
                cluster = self._voice.assign(audio)
                if cluster is None:
                    cluster = self._continuation()
            self._last_cluster = cluster
            if discord is not None:
                if cluster is not None:
                    # Teach the voice cluster who this is, so lines Discord can't
                    # attribute (crosstalk, missed events) keep the name and color.
                    self._voice.set_name(cluster, discord[1])
                    self._cluster_keys[cluster] = discord[0]
                return discord
            if cluster is not None:
                key = self._cluster_keys.get(cluster, f"voice:{cluster}")
                return key, self._voice.name(cluster) or f"Speaker {cluster + 1}"
            return None

    def _continuation(self) -> int | None:
        prev = self._last_cluster
        sims = self._voice.last.similarities
        if prev is None or sims is None or len(sims) <= prev:
            return None
        return prev if sims[prev] >= CONTINUE_SIMILARITY else None

    def live(self, start: float, end: float) -> tuple[str, str] | None:
        """Label for a still-changing partial line: Discord only (voice
        embeddings are too costly to run on every partial)."""
        with self._lock:
            return self._from_discord(start, end)

    def _from_discord(self, start: float, end: float) -> tuple[str, str] | None:
        if self._discord is None or self._timeline is None:
            return None
        self._timeline.prune(end - 120)
        overlaps = self._timeline.overlap(start - DISCORD_LEAD, end)
        if not overlaps:
            return None
        user_id, name, _seconds = overlaps[0]
        return f"discord:{user_id}", name
