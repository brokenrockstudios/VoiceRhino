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


def test_model_loading_status_stays_until_ready(tmp_path, monkeypatch):
    """Download progress, then unpacking/loading, are shown until captions start (no blank gap)."""
    monkeypatch.setenv("VOICERHINO_CONFIG", str(tmp_path))
    from voicerhino.ui import app as app_module
    from voicerhino.ui.app import LOADING_SECONDS, Controller

    shown: list[tuple[str, float]] = []
    real_set_status = Controller._set_status
    monkeypatch.setattr(Controller, "_set_status",
                        lambda self, text, seconds=8.0: (shown.append((text, seconds)),
                                                         real_set_status(self, text, seconds)))

    def fake_load(spec, options, progress):
        progress(0, 100_000_000)
        progress(100_000_000, 100_000_000)  # always reported, even right after the previous update
        return SimpleNamespace(close=lambda: None, spec=spec)

    monkeypatch.setattr(app_module, "ensure_download", lambda url: tmp_path)
    monkeypatch.setattr(app_module, "load_engine", fake_load)
    app = QApplication.instance() or QApplication([])
    controller = Controller(app, AppConfig(sources=[]), persist=False)
    controller._load_engine()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and controller.engine is None:
        app.processEvents()
        time.sleep(0.01)
    texts = [text for text, _ in shown]
    assert texts[0].startswith("Loading the speech model (NVIDIA Parakeet TDT 0.6B v2)")
    assert texts[1].startswith("Downloading the speech model") and "0% of 100 MB" in texts[1]
    assert texts[2].startswith("Setting up the speech model")
    assert all(seconds == LOADING_SECONDS for _, seconds in shown[:3])
    assert texts[3].startswith("No audio source enabled")  # replaces the loading message
    controller.shutdown()


def test_first_run_ready_hint_until_first_caption(tmp_path, monkeypatch):
    """The very first run says it's ready (an empty window looks broken) until a caption shows."""
    monkeypatch.setenv("VOICERHINO_CONFIG", str(tmp_path))
    from voicerhino.transcript import Utterance
    from voicerhino.ui import app as app_module
    from voicerhino.ui.app import Controller

    monkeypatch.setattr(app_module, "ensure_download", lambda url: tmp_path)
    monkeypatch.setattr(app_module, "load_engine",
                        lambda spec, options, progress: SimpleNamespace(close=lambda: None, spec=spec))
    monkeypatch.setattr(app_module, "make_source", lambda sc: SimpleNamespace(label=sc.describe()))
    monkeypatch.setattr(app_module, "CaptionPipeline",
                        lambda *args, **kwargs: SimpleNamespace(start=lambda: None, stop=lambda: None))
    app = QApplication.instance() or QApplication([])
    controller = Controller(app, AppConfig(sources=[SourceConfig(kind="app")]), persist=False)
    controller._first_run_hint = True  # as start(first_run=True) sets it
    controller._sync_windows()
    controller._load_engine()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and controller.engine is None:
        app.processEvents()
        time.sleep(0.01)
    hint = "Ready! Listening to Discord · NVIDIA Parakeet TDT 0.6B v2. Captions appear here when someone talks."
    assert controller.status == hint
    assert all(w._status == hint for w in controller.windows.values())
    controller._on_utterance(Utterance(1, "Discord", "hello", time.monotonic(), time.monotonic(), True))
    assert not controller._first_run_hint
    assert all(w._status == "" for w in controller.windows.values())
    controller._start_pipelines()  # e.g. after a settings change: back to the usual short status
    assert controller.status.startswith("Listening to Discord")
    controller.shutdown()
