"""End-to-end, offscreen: controller -> model load -> pipeline -> overlay windows."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

import pytest  # noqa: E402
from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtGui import QColor, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from conftest import OUT  # noqa: E402
from voicerhino.asr.catalog import DEFAULT_MODEL_ID, get_model  # noqa: E402
from voicerhino.asr.download import is_downloaded  # noqa: E402
from voicerhino.config import AppConfig, SourceConfig  # noqa: E402
from voicerhino.pipeline import SourceLevel  # noqa: E402


@pytest.mark.skipif(not is_downloaded(get_model(DEFAULT_MODEL_ID).source), reason="model not downloaded")
def test_gui_captions_file(conversation, tmp_path, monkeypatch):
    monkeypatch.setenv("VOICERHINO_CONFIG", str(tmp_path))
    from voicerhino.ui.app import Controller

    app = QApplication.instance() or QApplication([])
    wav, _ = conversation
    config = AppConfig(sources=[SourceConfig(kind="file", file=str(wav))], locked=True)
    controller = Controller(app, config, persist=False)
    controller.start(first_run=False)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and len(controller.store.finals()) < 3:
        app.processEvents()
        time.sleep(0.02)
    finals = controller.store.finals()
    canvas = QPixmap(1920, 1080)
    canvas.fill(QColor("#35506b"))
    painter = QPainter(canvas)
    for w in controller.windows.values():
        w.repaint()
        painter.drawPixmap(QPoint(w.x(), w.y()), w.grab())
    painter.end()
    canvas.save(str(OUT / "gui_smoke.png"))
    controller.shutdown()
    assert len(finals) >= 3, f"status: {controller.status}"
    assert "top lane" in finals[0].text.lower()
    assert controller.status.startswith("Listening")


def test_quiet_warning_reaches_every_window(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICERHINO_CONFIG", str(tmp_path))
    from voicerhino.ui.app import Controller

    app = QApplication.instance() or QApplication([])
    controller = Controller(app, AppConfig(), persist=False)
    controller._sync_windows()
    level = SourceLevel("Discord", -70.0, 24.0, -60.0)
    controller.pipelines = [SimpleNamespace(level_info=lambda: level)]
    controller._check_quiet()
    assert controller.warning and "Discord is very quiet (-60 dB)" in controller.warning
    controller.add_view("caption")  # windows added later show it too
    assert len(controller.windows) == 3
    assert all(w.warning == controller.warning for w in controller.windows.values())
    level = SourceLevel("Discord", -20.0, 0.0, -30.0)  # someone turned Discord back up
    controller._check_quiet()
    assert controller.warning is None and all(w.warning is None for w in controller.windows.values())
    assert controller.levels() == [level]
    controller.pipelines = []
    controller.shutdown()
