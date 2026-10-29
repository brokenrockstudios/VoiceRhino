"""Speaker colors end to end: conversation -> pipeline -> SpeakerAttributor."""

from __future__ import annotations

import pytest

from voicerhino.asr.catalog import VAD_MODELS
from voicerhino.asr.download import ensure_download, is_downloaded
from voicerhino.audio.file_source import read_wav
from voicerhino.config import SpeakerConfig
from voicerhino.pipeline import CaptionProcessor, PipelineSettings
from voicerhino.speakers.voice_cluster import DEFAULT_SPEAKER_MODEL, SPEAKER_MODELS

pytestmark = pytest.mark.skipif(not is_downloaded(SPEAKER_MODELS[DEFAULT_SPEAKER_MODEL]),
                                reason="speaker model not downloaded")


def _true_speaker(u, segments) -> str:
    overlap = lambda s: min(u.end, s["end"]) - max(u.start, s["start"])
    return max(segments, key=overlap)["speaker"]


def _run(engine, wav, attributor):
    data, _ = read_wav(wav)
    events = []
    proc = CaptionProcessor(engine, ensure_download(VAD_MODELS["silero"]), PipelineSettings(),
                            events.append, start_time=0.0, speaker_fn=attributor)
    for i in range(0, len(data), 320):
        proc.feed(data[i : i + 320, 0])
    proc.flush()
    return [u for u in events if u.final and u.text]


def test_voice_colors_through_pipeline(engine, conversation):
    from voicerhino.speakers.attribution import SpeakerAttributor

    wav, truth = conversation
    finals = _run(engine, wav, SpeakerAttributor(SpeakerConfig(voice_enabled=True)))
    pairs = [(_true_speaker(u, truth["segments"]), u.speaker_key, u.speaker) for u in finals]
    labeled = [p for p in pairs if p[1]]
    print("\n" + "\n".join(f"{t:6} -> {name}" for t, _, name in pairs))
    # A speaker's first short replies stay unlabeled until they've said something
    # long enough to fingerprint (~2 s); a wrong name would be worse than none.
    assert len(labeled) >= 0.7 * len(pairs)
    keys_by_speaker: dict[str, set[str]] = {}
    for true, key, _ in labeled:
        keys_by_speaker.setdefault(true, set()).add(key)
    assert all(len(keys) == 1 for keys in keys_by_speaker.values()), keys_by_speaker
    keys = [next(iter(k)) for k in keys_by_speaker.values()]
    assert len(set(keys)) == len(keys), "two people share a color"


class _FakeDiscord:
    def stop(self):
        pass


def test_discord_names_teach_voice_clusters(engine, conversation):
    """Discord knows who spoke the first 7 lines; the last 2 must still get the
    right names through the voice clusters Discord named earlier."""
    from voicerhino.speakers.attribution import SpeakerAttributor
    from voicerhino.speakers.discord_rpc import SpeakingEvent, SpeakingTimeline

    wav, truth = conversation
    ids = {"Mark": "1", "Zira": "2", "David": "3"}
    known, unknown = truth["segments"][:7], truth["segments"][7:]
    timeline = SpeakingTimeline()
    for seg in known:
        timeline.add(SpeakingEvent(ids[seg["speaker"]], seg["speaker"], True, seg["start"]))
        timeline.add(SpeakingEvent(ids[seg["speaker"]], seg["speaker"], False, seg["end"]))
    attributor = SpeakerAttributor(SpeakerConfig(voice_enabled=True))
    attributor._timeline, attributor._discord = timeline, _FakeDiscord()

    finals = _run(engine, wav, attributor)
    cutoff = unknown[0]["start"] - 0.3
    for u in finals:
        true = _true_speaker(u, truth["segments"])
        if u.end < cutoff:
            assert (u.speaker_key, u.speaker) == (f"discord:{ids[true]}", true), u.text
        elif u.speaker is not None:
            assert (u.speaker_key, u.speaker) == (f"discord:{ids[true]}", true), u.text
    late = [u for u in finals if u.start >= cutoff]
    assert late and sum(u.speaker is not None for u in late) >= len(late) - 1
