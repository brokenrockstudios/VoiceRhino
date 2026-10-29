"""Render caption + history overlays offscreen over a fake game frame -> PNG.

    python tools/screenshot_overlays.py tests/_out/overlays.png
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from voicerhino.config import ViewConfig  # noqa: E402
from voicerhino.transcript import TranscriptStore, Utterance  # noqa: E402
from voicerhino.ui.overlay import CaptionWindow, HistoryWindow  # noqa: E402

LINES = [
    ("discord:1", "Ryan", "Okay, I'm going to push top lane. Can somebody cover the dragon?"),
    ("discord:2", "Sam", "Yeah, I've got it. Give me 10 seconds to finish this camp."),
    ("discord:3", "Alex", "Their jungler just showed up bottom. Be careful."),
    ("discord:1", "Ryan", "Did you see that? I almost died to the turret again."),
]


def main(out: str, warning: bool = True) -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    store = TranscriptStore()
    now = time.monotonic()
    for i, (key, name, text) in enumerate(LINES):
        store.push(Utterance(i + 1, "main", text, now - 20 + i * 4, now - 17 + i * 4, True, key, name))
    store.push(Utterance(99, "main", "You always say that, just back off and", now - 1, now, False))

    canvas = QPixmap(1600, 900)
    p = QPainter(canvas)
    grad = QLinearGradient(0, 0, 1600, 900)
    grad.setColorAt(0, QColor("#3b6e8f"))
    grad.setColorAt(0.5, QColor("#c9a86a"))
    grad.setColorAt(1, QColor("#2d4a2b"))
    p.fillRect(canvas.rect(), grad)

    caption = CaptionWindow(ViewConfig(x=150, y=650, w=1100, h=170), store)
    caption.set_locked(True)
    if warning:
        caption.set_warning("⚠ Discord is very quiet (-58 dB): turn its volume up")
    history = HistoryWindow(ViewConfig.default_history(), store)
    history.view.x, history.view.y = 1060, 60
    history.apply_view(history.view)
    unlocked = CaptionWindow(ViewConfig(x=100, y=60, w=800, h=150, font_size=28), store)
    for w in (caption, history, unlocked):
        w._last_activity = time.monotonic()
        w.show()
    app.processEvents()
    for w in (caption, history, unlocked):
        p.drawPixmap(QPoint(w.x(), w.y()), w.grab())
    p.end()
    canvas.save(out)
    print("wrote", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tests/_out/overlays.png")
