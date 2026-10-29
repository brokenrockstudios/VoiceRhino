from __future__ import annotations

import time

from voicerhino.transcript import TranscriptStore, Utterance


def utt(i: int, text: str, final: bool, source: str = "main", age: float = 0.0) -> Utterance:
    now = time.monotonic() - age
    return Utterance(i, source, text, now - 1, now, final)


def test_partial_replaced_by_final():
    store = TranscriptStore()
    seen = []
    store.subscribe(seen.append)
    store.push(utt(1, "hello", False))
    assert [u.text for u in store.partials()] == ["hello"]
    store.push(utt(1, "hello there.", True))
    assert store.partials() == []
    assert [u.text for u in store.finals()] == ["hello there."]
    assert len(seen) == 2


def test_empty_final_clears_partial_without_adding():
    store = TranscriptStore()
    store.push(utt(1, "uh", False))
    store.push(utt(1, "", True))
    assert store.partials() == [] and store.finals() == []


def test_partials_per_source_and_old_finals_pruned():
    store = TranscriptStore(keep_seconds=60)
    store.push(utt(1, "old", True, age=120))
    store.push(utt(2, "a", False, source="discord"))
    store.push(utt(3, "b", False, source="game"))
    store.push(utt(4, "new", True, source="game"))  # a different id leaves game's live partial alone
    assert [u.text for u in store.finals()] == ["new"]
    assert {u.source for u in store.partials()} == {"discord", "game"}


def test_update_amends_final_and_notifies():
    store = TranscriptStore()
    store.push(utt(7, "ni hao", True))
    seen = []
    store.subscribe(seen.append)
    assert store.update(7, translation="hello").translation == "hello"
    assert store.finals()[0].translation == "hello"
    assert seen and seen[0].translation == "hello"
    assert store.update(999, translation="x") is None


def test_finals_since():
    store = TranscriptStore()
    store.push(utt(1, "a", True, age=50))
    store.push(utt(2, "b", True, age=5))
    assert [u.text for u in store.finals(since=time.monotonic() - 30)] == ["b"]


def test_partials_are_kept_per_source_id():
    """Two sources may share a display name (e.g. two "Default output" entries)."""
    store = TranscriptStore()
    now = time.monotonic()
    store.push(Utterance(1, "Default output", "first", now - 1, now, False, source_id="a1"))
    store.push(Utterance(2, "Default output", "second", now - 1, now, False, source_id="b2"))
    assert sorted(u.text for u in store.partials()) == ["first", "second"]
    store.push(Utterance(1, "Default output", "first.", now - 1, now, True, source_id="a1"))
    assert [u.text for u in store.partials()] == ["second"]
