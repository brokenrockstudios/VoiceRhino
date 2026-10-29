"""Render every Settings tab offscreen to PNGs for a visual check.

Writes tests/_out/settings_<tab>.png with a dark Fusion palette (like the app),
plus a few extra states: a single-app source, a history window selected and
the speaker options switched on. --light renders Qt's default light palette to
settings_<tab>_light.png instead. Uses this PC's real audio devices and apps.

    python tools/screenshot_settings.py [--light] [--size 720x560]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":  # the offscreen plugin has no font database of its own on Windows
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "_out"
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from voicerhino.config import AppConfig  # noqa: E402
from voicerhino.pipeline import SourceLevel  # noqa: E402
from voicerhino.ui.settings import SettingsDialog  # noqa: E402


def dark_palette() -> QPalette:
    """A typical dark Fusion palette (stands in for the one the app sets)."""
    text = QColor(230, 230, 230)
    disabled = QColor(120, 120, 120)
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, QColor(37, 37, 38))
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, QColor(28, 28, 30))
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(45, 45, 48))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(50, 50, 52))
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, QColor(50, 50, 53))
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.BrightText, QColor(255, 80, 80))
    p.setColor(QPalette.ColorRole.Link, QColor(110, 170, 240))
    p.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(140, 140, 140))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    return p


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Render the Settings dialog tabs to PNGs.")
    parser.add_argument("--light", action="store_true", help="use Qt's default light palette")
    parser.add_argument("--size", default="720x560", help="dialog size, default the minimum 720x560")
    args = parser.parse_args(argv)
    width, height = (int(v) for v in args.size.lower().split("x"))

    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 9))
    if not args.light:
        app.setPalette(dark_palette())
    suffix = "_light" if args.light else ""
    OUT.mkdir(parents=True, exist_ok=True)

    # Pretend a source is running so the Audio tab shows its level meter.
    running = [SourceLevel("Default output", -24.0, 0.0, -22.0)]
    dialog = SettingsDialog(AppConfig(), levels=lambda: running)
    dialog.resize(width, height)
    dialog.show()

    def shot(name: str) -> None:
        app.processEvents()
        path = OUT / f"settings_{name}{suffix}.png"
        dialog.grab().save(str(path))
        print(path)

    for index in range(dialog.tabs.count()):
        dialog.tabs.setCurrentIndex(index)
        shot(dialog.tabs.tabText(index).lower())

    audio = dialog.audio_tab
    dialog.tabs.setCurrentWidget(audio)
    audio.kind_combo.setCurrentIndex(audio.kind_combo.findData("app"))
    running[:] = [SourceLevel("Discord", -58.0, 24.0, -60.0)]  # very quiet even with the boost
    audio.refresh_levels()
    shot("audio_app")
    dialog.tabs.setCurrentWidget(dialog.windows_tab)
    dialog.windows_tab.view_list.setCurrentRow(1)
    shot("windows_history")
    dialog.tabs.setCurrentWidget(dialog.speakers_tab)
    dialog.speakers_tab.voice_check.setChecked(True)
    dialog.speakers_tab.discord_check.setChecked(True)
    shot("speakers_on")
    dialog.tabs.setCurrentWidget(audio)  # a second source (a mic), left out of the caption window
    audio.add_button.click()
    audio.kind_combo.setCurrentIndex(audio.kind_combo.findData("input"))
    dialog.tabs.setCurrentWidget(dialog.windows_tab)
    dialog.windows_tab.view_list.setCurrentRow(0)
    dialog.windows_tab.source_checks[-1].setChecked(False)
    shot("windows_sources")
    dialog.close()


if __name__ == "__main__":
    main()
