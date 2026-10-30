"""Caption and history windows: which sources each one shows (offscreen Qt, no audio)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

import time  # noqa: E402

from PySide6.QtCore import QRectF  # noqa: E402
from PySide6.QtGui import QFontMetricsF, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from voicerhino.config import ViewConfig  # noqa: E402
from voicerhino.transcript import TranscriptStore, Utterance  # noqa: E402
from voicerhino.ui.overlay import CaptionWindow, HistoryWindow  # noqa: E402

APP = QApplication.instance() or QApplication([])


def store_with_two_sources() -> TranscriptStore:
    store = TranscriptStore()
    now = time.monotonic()
    store.push(Utterance(1, "Default output", "push top lane", now - 3, now - 2, True, source_id="out"))
    store.push(Utterance(2, "Microphone", "my own words", now - 2, now - 1, True, source_id="mic"))
    store.push(Utterance(3, "Microphone", "still talking", now - 1, now, False, source_id="mic"))
    store.push(Utterance(4, "Default output", "cover the", now - 1, now, False, source_id="out"))
    return store


def test_caption_window_leaves_out_hidden_sources():
    store = store_with_two_sources()
    everything = CaptionWindow(ViewConfig(), store)
    everything._last_activity = time.monotonic()
    assert [text for _, _, text, _ in everything._items()] == [
        "push top lane", "my own words", "still talking", "cover the"]
    no_mic = CaptionWindow(ViewConfig(hidden_sources=["mic"]), store)
    no_mic._last_activity = time.monotonic()
    assert [text for _, _, text, _ in no_mic._items()] == ["push top lane", "cover the"]


def test_hidden_source_speech_does_not_keep_old_captions_up():
    store = store_with_two_sources()
    window = CaptionWindow(ViewConfig(hidden_sources=["mic"], clear_after=8), store)
    window._last_activity = 0.0
    window.on_utterance(Utterance(6, "Microphone", "me again", 0, 0, False, source_id="mic"))
    assert window._last_activity == 0.0  # my own mic doesn't count as activity here
    window.on_utterance(Utterance(5, "Default output", "hi", 0, 0, False, source_id="out"))
    assert window._last_activity > 0


def test_history_window_leaves_out_hidden_sources():
    store = store_with_two_sources()
    html = HistoryWindow(ViewConfig.default_history(), store)._html()
    assert "my own words" in html and "push top lane" in html
    view = ViewConfig.default_history()
    view.hidden_sources = ["out"]
    html = HistoryWindow(view, store)._html()
    assert "my own words" in html and "still talking" in html
    assert "push top lane" not in html and "cover the" not in html


def painted_warning(window, monkeypatch) -> str | None:
    """The warning a repaint draws (None: none drawn)."""
    drawn: list[str | None] = []
    monkeypatch.setattr(type(window), "_paint_warning", lambda self, p: drawn.append(self._shown_warning()))
    window.grab()
    return drawn[-1] if drawn else None


def test_caption_window_shows_warning_only_with_captions(monkeypatch):
    window = CaptionWindow(ViewConfig(clear_after=8), store_with_two_sources())
    window.set_warning("⚠ Discord is very quiet")
    window._last_activity = time.monotonic()
    assert painted_warning(window, monkeypatch) == "⚠ Discord is very quiet"
    window._last_activity = 0.0  # captions cleared: the bar over the game stays empty
    assert painted_warning(window, monkeypatch) is None
    window._last_activity = time.monotonic()
    window.view.show_warnings = False
    assert painted_warning(window, monkeypatch) is None


def test_history_window_keeps_warning_up_unless_turned_off(monkeypatch):
    window = HistoryWindow(ViewConfig.default_history(), TranscriptStore())  # nothing said lately
    window.set_warning("⚠ Discord is very quiet")
    assert painted_warning(window, monkeypatch) == "⚠ Discord is very quiet"
    window.view.show_warnings = False
    assert painted_warning(window, monkeypatch) is None


def max_alpha(window) -> int:
    image = window.grab().toImage()
    return max(image.pixelColor(x, y).alpha() for x in range(0, image.width(), 2)
               for y in range(0, image.height(), 2))


def test_text_opacity_fades_captions_and_outline_together():
    alphas = {}
    for opacity in (1.0, 0.5, "junk"):  # a hand-edited config can't make the text vanish or crash
        view = ViewConfig(background_opacity=0.0, text_opacity=opacity, outline=3)
        window = CaptionWindow(view, store_with_two_sources())
        window.set_locked(True)  # no dashed frame or hints: only the text has any alpha
        window._last_activity = time.monotonic()
        alphas[opacity] = max_alpha(window)
    assert alphas[1.0] >= 250 and alphas["junk"] == alphas[1.0]
    assert 115 <= alphas[0.5] <= 140  # one fade over text and outline, not two stacked ones


def test_history_shows_source_numbers_in_place_of_names():
    view = ViewConfig.default_history()
    window = HistoryWindow(view, store_with_two_sources())
    window.multi_source = True
    window.source_numbers = {"out": 1, "mic": 2}
    assert "Default output:" in window._html() and "Microphone:" in window._html()
    view.show_source_number = True
    html = window._html()
    assert "Default output:" not in html and "Microphone:" not in html
    assert '<b style="color:#8a8d93">1</b> <span style="color:#FFFFFF">push top lane' in html
    assert '<b style="color:#8a8d93">2</b> <span style="color:#FFFFFF">my own words' in html


def test_history_resource_line_sits_above_the_warning(monkeypatch):
    view = ViewConfig.default_history()
    window = HistoryWindow(view, TranscriptStore())
    window.set_resources("CPU 0.5 cores (3%) · RAM 1.2 GB")
    window.set_warning("⚠ Discord is very quiet")
    assert window._shown_resources() is None  # off by default
    view.show_resources = True
    assert window._shown_resources() == "CPU 0.5 cores (3%) · RAM 1.2 GB"
    tops = []
    monkeypatch.setattr(QFontMetricsF, "elidedText", lambda self, text, mode, width: text)
    real_draw = QPainter.drawText
    monkeypatch.setattr(QPainter, "drawText", lambda p, *args: (
        tops.append((args[0].top(), args[-1])) if isinstance(args[0], QRectF) else None, real_draw(p, *args))[1])
    window.set_locked(True)
    window.grab()
    resource_top = min(t for t, text in tops if text.startswith("CPU"))
    warning_top = min(t for t, text in tops if text.startswith("⚠"))
    assert resource_top < warning_top
    caption = CaptionWindow(ViewConfig(show_resources=True), TranscriptStore())
    caption.set_resources("CPU 0.5 cores")
    assert caption._shown_resources() is None  # history windows only


def test_caption_outline_uses_its_color():
    def outline_pixels(color: str) -> int:
        view = ViewConfig(background_opacity=0.0, outline=4, outline_color=color, text_color="#FFFFFF")
        window = CaptionWindow(view, store_with_two_sources())
        window.set_locked(True)
        window._last_activity = time.monotonic()
        image = window.grab().toImage()
        return sum(1 for x in range(0, image.width(), 2) for y in range(0, image.height(), 2)
                   if (c := image.pixelColor(x, y)).alpha() > 150 and c.red() > 180 and c.green() < 80)

    assert outline_pixels("#FF0000") > 100 and outline_pixels("#000000") == 0
