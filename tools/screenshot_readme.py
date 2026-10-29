"""Render the screenshots the README shows -> docs/images/*.png.

A curated, machine-neutral set: the overlays over a fake game frame and a few
Settings tabs in the dark palette, with generic source names instead of this
PC's devices and no quiet-audio warnings. For a check of every tab and state,
use screenshot_settings.py instead.

    python tools/screenshot_readme.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":  # the offscreen plugin has no font database of its own on Windows
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import screenshot_overlays  # noqa: E402
from screenshot_settings import dark_palette  # noqa: E402
from voicerhino.config import AppConfig, SourceConfig  # noqa: E402
from voicerhino.pipeline import SourceLevel  # noqa: E402
from voicerhino.ui.settings import SettingsDialog  # noqa: E402


def demo_config() -> AppConfig:
    """Discord-only capture plus a microphone that's left out of the caption window."""
    cfg = AppConfig()
    discord = SourceConfig(kind="app", app="Discord.exe")
    mic = SourceConfig(kind="device", device="input:My microphone", name="My microphone")
    cfg.sources = [discord, mic]
    cfg.views[0].hidden_sources = [mic.id]  # the default caption window
    return cfg


def shoot(dialog: SettingsDialog, tab, name: str) -> None:
    dialog.tabs.setCurrentWidget(tab)
    QApplication.processEvents()
    path = OUT / f"settings-{name}.png"
    dialog.grab().save(str(path))
    print(path)


def main() -> None:
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 9))
    app.setPalette(dark_palette())
    OUT.mkdir(parents=True, exist_ok=True)

    screenshot_overlays.main(str(OUT / "overlays.png"), warning=False)

    levels = [SourceLevel("Discord", -24.0, 0.0, -22.0), SourceLevel("My microphone", -31.0, 6.0, -28.0)]
    dialog = SettingsDialog(demo_config(), levels=lambda: levels)
    dialog.resize(720, 560)
    dialog.show()
    shoot(dialog, dialog.audio_tab, "audio")
    shoot(dialog, dialog.recognition_tab, "recognition")
    dialog.windows_tab.view_list.setCurrentRow(0)
    shoot(dialog, dialog.windows_tab, "windows")
    dialog.speakers_tab.voice_check.setChecked(True)
    shoot(dialog, dialog.speakers_tab, "speakers")
    dialog.close()

    # Translation: Chinese speech recognized by SenseVoice, translated to English by Opus-MT.
    cfg = AppConfig()
    cfg.asr.model, cfg.asr.language = "sense-voice-int8", ""
    cfg.translation.model = "opus-mt-zh-en"
    dialog = SettingsDialog(cfg)
    dialog.resize(720, 560)
    dialog.show()
    shoot(dialog, dialog.translation_tab, "translation")
    dialog.close()


if __name__ == "__main__":
    main()
