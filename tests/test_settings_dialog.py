"""Settings dialog, driven programmatically with an offscreen Qt (no real windows or audio)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import copy
import sys
import time
import types
from dataclasses import asdict
from pathlib import Path

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QGuiApplication, QKeySequence
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLineEdit

import voicerhino
from voicerhino.asr import download
from voicerhino.audio import devices
from voicerhino.audio.devices import DEFAULT_OUTPUT_KEY, DeviceInfo
from voicerhino.config import AppConfig, SourceConfig, ViewConfig
from voicerhino.paths import models_dir
from voicerhino.pipeline import PipelineSettings, SourceLevel
from voicerhino.resources import ResourceMonitor, Usage
from voicerhino.ui import settings
from voicerhino.ui.settings import LevelMeter, SettingsDialog, parse_language, place_view

APP = QApplication.instance() or QApplication([])
REAL_LIST_APP_CHOICES = settings.list_app_choices

FAKE_DEVICES = [
    DeviceInfo("loopback:Headphones (Arctis)", "Headphones (Arctis)", "loopback", 10, 2, 48000, True),
    DeviceInfo("loopback:VOIPSpeakers (VB-Audio)", "VOIPSpeakers (VB-Audio)", "loopback", 11, 2, 48000, False),
    DeviceInfo("input:Microphone (Arctis)", "Microphone (Arctis)", "input", 9, 1, 48000, True),
    DeviceInfo("input:Line In", "Line In", "input", 8, 2, 44100, False),
]


@pytest.fixture(autouse=True)
def fake_system(monkeypatch, tmp_path):
    """Deterministic devices and running apps; no audio APIs touched. The models folder is a
    temporary one, so nothing here can ever delete a real download."""
    monkeypatch.setenv("VOICERHINO_MODELS", str(tmp_path / "models"))
    monkeypatch.setattr(devices, "list_devices", lambda: list(FAKE_DEVICES))
    monkeypatch.setattr(settings, "list_app_choices", lambda: [("Game.exe", True), ("Discord.exe", False)])


def make_dialog(config: AppConfig) -> tuple[SettingsDialog, list[AppConfig]]:
    dialog = SettingsDialog(config)
    emitted: list[AppConfig] = []
    dialog.applied.connect(emitted.append)
    return dialog, emitted


def button(dialog: SettingsDialog, which: QDialogButtonBox.StandardButton):
    return dialog.button_box.button(which)


def test_apply_emits_edited_copy_and_leaves_original_alone():
    original = AppConfig()
    snapshot = copy.deepcopy(original)
    dialog, emitted = make_dialog(original)
    assert dialog.windowTitle() == "VoiceRhino Settings"
    assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == [
        "Audio", "Recognition", "Timing", "Windows", "Speakers", "Translation", "General"]

    rec = dialog.recognition_tab
    rec.model_combo.setCurrentIndex(rec.model_combo.findData("moonshine-base-en-int8"))
    rec.threads_spin.setValue(4)
    win = dialog.windows_tab
    win.add_history_button.click()  # selects the new window
    win.font_size_spin.setValue(22)
    dialog.speakers_tab.voice_check.setChecked(True)
    audio = dialog.audio_tab
    audio.kind_combo.setCurrentIndex(audio.kind_combo.findData("app"))
    audio.app_combo.setEditText("Game.exe")

    button(dialog, QDialogButtonBox.StandardButton.Apply).click()

    assert len(emitted) == 1
    cfg = emitted[0]
    assert isinstance(cfg, AppConfig) and cfg is not original
    assert cfg.asr.model == "moonshine-base-en-int8" and cfg.asr.device == "cpu"
    assert cfg.asr.threads == 4
    assert [v.kind for v in cfg.views] == ["caption", "history", "history"]
    assert cfg.views[2].font_size == 22
    assert cfg.views[1].font_size == snapshot.views[1].font_size
    assert cfg.speakers.voice_enabled is True
    assert cfg.sources[0].kind == "app" and cfg.sources[0].app == "Game.exe"
    assert asdict(original) == asdict(snapshot)

    # Later edits don't leak into the config that was already emitted.
    rec.threads_spin.setValue(6)
    assert cfg.asr.threads == 4


def test_cancel_discards_edits():
    original = AppConfig()
    snapshot = copy.deepcopy(original)
    dialog, emitted = make_dialog(original)
    dialog.show()
    dialog.recognition_tab.threads_spin.setValue(8)
    dialog.windows_tab.add_caption_button.click()
    dialog.general_tab.captions_check.setChecked(False)
    dialog.audio_tab.add_button.click()

    button(dialog, QDialogButtonBox.StandardButton.Cancel).click()

    assert emitted == []
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert asdict(original) == asdict(snapshot)
    # Showing the same dialog again starts from what was last applied.
    assert asdict(dialog.result_config()) == asdict(snapshot)
    assert dialog.recognition_tab.threads_spin.value() == snapshot.asr.threads
    assert dialog.windows_tab.view_list.count() == len(snapshot.views)
    assert dialog.audio_tab.source_list.count() == len(snapshot.sources)


def test_cancel_after_apply_keeps_applied_state():
    dialog, emitted = make_dialog(AppConfig())
    dialog.recognition_tab.threads_spin.setValue(3)
    dialog.apply()
    dialog.recognition_tab.threads_spin.setValue(9)
    dialog.reject()
    assert len(emitted) == 1
    assert dialog.result_config().asr.threads == 3


def test_ok_applies_and_accepts():
    dialog, emitted = make_dialog(AppConfig())
    dialog.show()
    dialog.timing_tab.spins["min_silence"].setValue(0.8)
    button(dialog, QDialogButtonBox.StandardButton.Ok).click()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert len(emitted) == 1 and emitted[0].pipeline.min_silence == 0.8
    assert asdict(dialog.result_config()) == asdict(emitted[0])


def test_untouched_dialog_round_trips_odd_config():
    cfg = AppConfig(
        sources=[
            SourceConfig(kind="app", app="Discord.exe", name="Friends"),
            SourceConfig(device="input:Unplugged Mic", enabled=False),
            SourceConfig(device="loopback:Old Speakers"),
            SourceConfig(kind="file", file=r"C:\clips\test.wav"),
        ],
        views=[
            ViewConfig(x=100, y=900, font_family="Not A Real Font", background_opacity=0.123,
                       clear_after=7.5, text_color="#abcdef"),
            ViewConfig.default_history(),
        ],
        locked=True,
        lock_hotkey="Ctrl+Shift+F9",
    )
    cfg.asr.language = "it"  # not in the list
    cfg.asr.model = "some-future-model"  # not in the catalog
    cfg.asr.threads = 32  # beyond the spinbox range
    cfg.asr.task = "translate"
    cfg.pipeline.min_silence = 0.123
    cfg.speakers.voice_threshold = 0.42
    snapshot = copy.deepcopy(cfg)
    dialog, emitted = make_dialog(cfg)
    dialog.show()  # late signals (e.g. the font list filling in) must not write either
    for row in range(dialog.audio_tab.source_list.count()):  # fill every editor
        dialog.audio_tab.source_list.setCurrentRow(row)
    for row in range(dialog.windows_tab.view_list.count()):
        dialog.windows_tab.view_list.setCurrentRow(row)
    for index in range(dialog.tabs.count()):
        dialog.tabs.setCurrentIndex(index)
        APP.processEvents()
    dialog.apply()
    assert asdict(emitted[0]) == asdict(snapshot)
    dialog.close()


def test_hand_edited_wrong_types_open_without_crashing():
    cfg = AppConfig(lock_hotkey=None)
    cfg.asr.threads = 2.0
    cfg.speakers.discord_client_id = 1234567890  # JSON number instead of a string
    cfg.views[0].font_size = "40"
    cfg.views[0].background_opacity = "lots"
    cfg.views[0].x, cfg.views[0].y = 10.0, 20.0
    cfg.pipeline.min_silence = None
    snapshot = copy.deepcopy(cfg)
    dialog, emitted = make_dialog(cfg)
    assert dialog.recognition_tab.threads_spin.value() == 2
    assert dialog.speakers_tab.client_id_edit.text() == "1234567890"
    assert dialog.windows_tab.font_size_spin.value() == 40
    assert dialog.windows_tab.opacity_slider.value() == round(ViewConfig.background_opacity * 100)
    dialog.apply()
    assert asdict(emitted[0]) == asdict(snapshot)  # shown, not silently rewritten


def test_device_listing_failure_is_shown_not_raised(monkeypatch):
    def broken():
        raise OSError("PortAudio exploded")

    monkeypatch.setattr(devices, "list_devices", broken)
    dialog, emitted = make_dialog(AppConfig())
    audio = dialog.audio_tab
    assert not audio.error_box.isHidden()
    assert "PortAudio exploded" in audio.error_label.text()
    assert audio.device_combo.currentData() == DEFAULT_OUTPUT_KEY

    # Picking a microphone while none is listed sticks, then resolves after Refresh.
    audio.kind_combo.setCurrentIndex(audio.kind_combo.findData("input"))
    assert audio.kind_combo.currentData() == "input"
    dialog.apply()
    assert emitted[-1].sources[0].device.startswith("input:")

    monkeypatch.setattr(devices, "list_devices", lambda: list(FAKE_DEVICES))
    audio.refresh_button.click()
    assert audio.error_box.isHidden()
    dialog.apply()
    assert emitted[-1].sources[0].device == "input:Microphone (Arctis)"


def test_audio_devices_and_sources():
    dialog, emitted = make_dialog(AppConfig())
    audio = dialog.audio_tab
    combo = audio.device_combo
    texts = [combo.itemText(i) for i in range(combo.count())]
    assert texts[0] == "Default output (all PC audio)" and combo.itemData(0) == DEFAULT_OUTPUT_KEY
    assert "Headphones (Arctis) - Windows default" in texts
    assert "VOIPSpeakers (VB-Audio)" in texts
    assert not any("Microphone" in t for t in texts)
    assert not audio.remove_button.isEnabled()  # the last source stays

    combo.setCurrentIndex(combo.findData("loopback:VOIPSpeakers (VB-Audio)"))
    assert audio.name_edit.placeholderText() == "VOIPSpeakers (VB-Audio)"
    audio.add_button.click()
    assert audio.source_list.count() == 2 and audio.remove_button.isEnabled()
    audio.kind_combo.setCurrentIndex(audio.kind_combo.findData("input"))  # takes the default mic
    audio.name_edit.setText("Me")
    audio.source_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    audio.add_button.click()
    audio.kind_combo.setCurrentIndex(audio.kind_combo.findData("file"))
    audio.file_edit.setText(r"C:\clips\demo.wav")
    dialog.apply()
    sources = emitted[-1].sources
    assert sources[0] == SourceConfig(device="loopback:VOIPSpeakers (VB-Audio)", enabled=False, id=sources[0].id)
    assert len({s.id for s in sources}) == 3  # each new source gets its own id
    assert (sources[1].kind, sources[1].device, sources[1].name) == ("device", "input:Microphone (Arctis)", "Me")
    assert (sources[2].kind, sources[2].file) == ("file", r"C:\clips\demo.wav")
    assert audio.source_list.item(1).text() == "2. Me - microphone"

    audio.source_list.setCurrentRow(1)
    audio.remove_button.click()
    dialog.apply()
    assert [s.kind for s in emitted[-1].sources] == ["device", "file"]


def test_single_app_combo_offers_discord_first():
    dialog, emitted = make_dialog(AppConfig())
    audio = dialog.audio_tab
    audio.kind_combo.setCurrentIndex(audio.kind_combo.findData("app"))
    items = [audio.app_combo.itemText(i) for i in range(audio.app_combo.count())]
    assert items == ["Discord.exe", "Game.exe"]
    assert audio.app_combo.currentText() == "Discord.exe"
    assert audio.app_combo.itemData(1, settings._ACTIVE_ROLE) is True
    assert audio.name_edit.placeholderText() == "Discord"
    dialog.apply()
    assert emitted[-1].sources[0].kind == "app" and emitted[-1].sources[0].app == "Discord.exe"


class _App:
    def __init__(self, exe: str, pid: int, active: bool):
        self.exe, self.pid, self.active = exe, pid, active


def test_app_choices_prefer_process_loopback(monkeypatch):
    fake = types.ModuleType("voicerhino.audio.process_loopback")
    fake.list_audio_apps = lambda: [
        _App("Discord.exe", 1, False), _App("Discord.exe", 2, True),
        _App("chrome.exe", 3, False), _App(r"C:\Games\Game.exe", 4, True),
    ]
    monkeypatch.setitem(sys.modules, "voicerhino.audio.process_loopback", fake)
    assert REAL_LIST_APP_CHOICES() == [("Discord.exe", True), ("Game.exe", True), ("chrome.exe", False)]


@pytest.mark.parametrize("module", [None, "raises", "empty"])
def test_app_choices_fall_back_to_running_processes(monkeypatch, module):
    if module is None:  # import fails (module not written / broken)
        monkeypatch.setitem(sys.modules, "voicerhino.audio.process_loopback", None)
    else:
        fake = types.ModuleType("voicerhino.audio.process_loopback")

        def list_audio_apps():
            if module == "raises":
                raise OSError("no COM today")
            return []

        fake.list_audio_apps = list_audio_apps
        monkeypatch.setitem(sys.modules, "voicerhino.audio.process_loopback", fake)
    monkeypatch.setattr(settings, "_running_process_names", lambda: ["Discord.exe", "game.exe"])
    assert REAL_LIST_APP_CHOICES() == [("Discord.exe", False), ("game.exe", False)]


def test_recognition_details_and_task(monkeypatch):
    monkeypatch.setattr(download, "is_downloaded", lambda url, root=None: False)
    dialog, emitted = make_dialog(AppConfig())
    rec = dialog.recognition_tab
    assert rec.status_label.text() == "Will download ~640 MB when applied"
    assert rec.languages_label.text() == "English"
    assert not rec.task_combo.isEnabled()

    rec.model_combo.setCurrentIndex(rec.model_combo.findData("fw-medium"))
    assert rec.task_combo.isEnabled()
    assert "first use" in rec.status_label.text()
    assert rec.device_combo.currentData() == "cuda"  # Whisper models are meant for the GPU
    rec.task_combo.setCurrentIndex(rec.task_combo.findData("translate"))
    rec.language_combo.setCurrentIndex(rec.language_combo.findData(""))  # Auto-detect
    dialog.apply()
    asr = emitted[-1].asr
    assert (asr.model, asr.device, asr.task, asr.language) == ("fw-medium", "cuda", "translate", "")

    rec.language_combo.setEditText("pt")
    rec.model_combo.setCurrentIndex(rec.model_combo.findData("sense-voice-int8"))
    assert not rec.task_combo.isEnabled()
    dialog.apply()
    asr = emitted[-1].asr
    assert (asr.device, asr.task, asr.language) == ("cpu", "transcribe", "pt")

    monkeypatch.setattr(download, "is_downloaded", lambda url, root=None: True)
    rec.model_combo.setCurrentIndex(rec.model_combo.findData("moonshine-tiny-en-int8"))
    assert rec.status_label.text() == "Downloaded"


def test_multilingual_model_switches_english_to_auto_detect():
    dialog, emitted = make_dialog(AppConfig())
    rec = dialog.recognition_tab
    rec.model_combo.setCurrentIndex(rec.model_combo.findData("qwen3-asr-0.6b-int8"))
    assert rec.language_combo.currentData() == ""  # English would force other languages into English
    rec.language_combo.setCurrentIndex(rec.language_combo.findData("ko"))
    rec.model_combo.setCurrentIndex(rec.model_combo.findData("sense-voice-int8"))
    assert rec.language_combo.currentData() == "ko"  # a language the user picked is kept
    dialog.apply()
    assert emitted[-1].asr.language == "ko"
    rec.model_combo.setCurrentIndex(rec.model_combo.findData("parakeet-tdt-0.6b-v2-int8"))
    dialog.apply()
    assert emitted[-1].asr.language == "en"


def test_parse_language():
    assert parse_language("English (en)") == "en"
    assert parse_language("Auto-detect") == ""
    assert parse_language("japanese") == "ja"
    assert parse_language(" IT ") == "it"
    assert parse_language("Portuguese (pt)") == "pt"


def test_timing_fields_and_restore_defaults():
    cfg = AppConfig()
    cfg.pipeline.partial_interval = 0.0
    cfg.pipeline.max_utterance = 20.0
    dialog, emitted = make_dialog(cfg)
    spins = dialog.timing_tab.spins
    assert spins["partial_interval"].text() == "Off"
    spins["vad_threshold"].setValue(0.65)
    spins["pre_roll"].setValue(0.4)
    dialog.apply()
    pipeline = emitted[-1].pipeline
    assert (pipeline.vad_threshold, pipeline.pre_roll, pipeline.max_utterance) == (0.65, 0.4, 20.0)

    dialog.timing_tab.restore_button.click()
    assert spins["max_utterance"].value() == PipelineSettings().max_utterance
    dialog.apply()
    assert asdict(emitted[-1].pipeline) == asdict(PipelineSettings())


def test_windows_list_editing():
    dialog, emitted = make_dialog(AppConfig())
    win = dialog.windows_tab
    win.add_caption_button.click()
    titles = [win.view_list.item(i).text() for i in range(win.view_list.count())]
    assert titles == ["Caption window 1", "History window 1", "Caption window 2"]
    win.bold_check.setChecked(False)
    win.opacity_slider.setValue(80)
    assert win.opacity_label.text() == "80%"
    win.outline_spin.setValue(0)
    win.text_color_button.choose("#FFFF00")
    win.clear_after_spin.setValue(0)
    win.lines_spin.setValue(3)
    win.click_through_check.setChecked(False)
    win.view_list.item(1).setCheckState(Qt.CheckState.Unchecked)
    dialog.apply()
    views = emitted[-1].views
    new = views[2]
    assert (new.bold, new.background_opacity, new.outline, new.text_color) == (False, 0.8, 0, "#FFFF00")
    assert (new.clear_after, new.lines, new.click_through) == (0.0, 3, False)
    assert views[1].visible is False and views[0].visible is True

    # Caption-only and history-only fields follow the selected window's kind.
    form = win._form
    win.view_list.setCurrentRow(1)
    assert form.isRowVisible(win._history_fields[0]) and not form.isRowVisible(win._caption_fields[0])
    win.history_seconds_spin.setValue(120)
    win.timestamps_check.setChecked(False)
    win.view_list.setCurrentRow(2)
    win.remove_button.click()
    dialog.apply()
    views = emitted[-1].views
    assert len(views) == 2 and views[1].history_seconds == 120 and views[1].show_timestamps is False


def test_place_view_on_a_screen_area():
    area = QRect(-2560, 0, 2560, 1400)  # a monitor left of the main one
    caption = ViewConfig()
    place_view(caption, area)
    assert caption.w == round(2560 * 0.7)
    assert caption.x == area.x() + (area.width() - caption.w) // 2
    assert area.y() + area.height() // 2 < caption.y and caption.y + caption.h < area.y() + area.height()
    history = ViewConfig.default_history()
    place_view(history, area)
    assert history.w == 520 and history.x + history.w < area.x() + area.width()
    assert history.x > area.x() + area.width() // 2
    assert abs(history.y + history.h // 2 - (area.y() + area.height() // 2)) <= 1


def test_move_to_screen_from_the_dialog():
    dialog, emitted = make_dialog(AppConfig())
    win = dialog.windows_tab
    win.view_list.setCurrentRow(0)
    assert win.screen_combo.currentIndex() == 0  # x/y = -1: placed automatically
    win.screen_combo.setCurrentIndex(1)
    win.screen_combo.activated.emit(1)
    dialog.apply()
    view = emitted[-1].views[0]
    area = QGuiApplication.screens()[0].availableGeometry()
    assert view.w == round(area.width() * 0.7)
    assert area.contains(view.x, view.y)
    assert win.screen_combo.currentIndex() == 1 and str(view.w) in win.placement_label.text()


def test_window_geometry_reported_by_the_app_survives_apply():
    cfg = AppConfig()
    dialog, emitted = make_dialog(cfg)
    dialog.update_view_geometry(cfg.views[0].id, 10, 20, 900, 150)
    dialog.windows_tab.view_list.setCurrentRow(0)
    dialog.windows_tab.font_size_spin.setValue(40)
    dialog.apply()
    view = emitted[-1].views[0]
    assert (view.x, view.y, view.w, view.h, view.font_size) == (10, 20, 900, 150, 40)


def test_set_config_reloads_widgets():
    dialog, emitted = make_dialog(AppConfig())
    fresh = AppConfig(captions_enabled=False)
    fresh.asr.threads = 5
    dialog.set_config(fresh)
    assert dialog.recognition_tab.threads_spin.value() == 5
    assert not dialog.general_tab.captions_check.isChecked()
    dialog.reject()
    assert asdict(dialog.result_config()) == asdict(fresh)


def test_speakers_tab(monkeypatch, tmp_path):
    dialog, emitted = make_dialog(AppConfig())
    sp = dialog.speakers_tab
    assert not sp.client_id_edit.isEnabled() and not sp.threshold_spin.isEnabled()
    sp.discord_check.setChecked(True)
    assert sp.client_id_edit.isEnabled()
    sp.client_id_edit.setText(" 1234567890 ")
    sp.secret_edit.setText("s3cret")
    assert sp.secret_edit.echoMode() == QLineEdit.EchoMode.Password
    sp.show_secret_button.click()
    assert sp.secret_edit.echoMode() == QLineEdit.EchoMode.Normal
    sp.voice_check.setChecked(True)
    sp.threshold_spin.setValue(0.55)
    dialog.apply()
    s = emitted[-1].speakers
    assert (s.discord_enabled, s.discord_client_id, s.discord_client_secret) == (True, "1234567890", "s3cret")
    assert (s.voice_enabled, s.voice_threshold) == (True, 0.55)
    dialog.reject()  # reopening must not leave the secret on screen
    assert sp.secret_edit.echoMode() == QLineEdit.EchoMode.Password
    assert sp.secret_edit.text() == "s3cret"  # it was applied before Cancel

    assert settings.discord_setup_doc() == Path(voicerhino.__file__).resolve().parents[2] / "docs" / "discord-setup.md"
    opened, messages = [], []
    monkeypatch.setattr(settings.QDesktopServices, "openUrl", lambda url: opened.append(url) or True)
    monkeypatch.setattr(settings.QMessageBox, "information", lambda *args: messages.append(args))
    monkeypatch.setattr(settings, "discord_setup_doc", lambda: tmp_path / "missing.md")
    sp.help_button.click()
    assert len(messages) == 1 and not opened
    doc = tmp_path / "discord-setup.md"
    doc.write_text("# Discord setup\n", encoding="utf-8")
    monkeypatch.setattr(settings, "discord_setup_doc", lambda: doc)
    sp.help_button.click()
    assert len(opened) == 1 and Path(opened[0].toLocalFile()) == doc


def test_general_tab():
    dialog, emitted = make_dialog(AppConfig())
    general = dialog.general_tab
    assert general.hotkey_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText) == "Ctrl+Alt+C"
    general.hotkey_edit.setKeySequence(QKeySequence("Ctrl+Shift+F9"))
    general.captions_check.setChecked(False)
    dialog.apply()
    cfg = emitted[-1]
    assert (cfg.lock_hotkey, cfg.captions_enabled) == ("Ctrl+Shift+F9", False)
    assert not hasattr(general, "transcript_check")  # saving transcripts was removed on purpose
    general.hotkey_edit.clear()
    dialog.apply()
    assert emitted[-1].lock_hotkey == ""


def test_translation_tab_picks_a_local_model():
    dialog, emitted = make_dialog(AppConfig())
    tab = dialog.translation_tab
    assert tab.model_combo.currentData() == "" and tab.model_combo.currentText() == "Off"
    assert tab.details.isHidden() and not tab.foreign_check.isEnabled()
    assert not hasattr(tab, "url_edit") and not hasattr(tab, "key_edit")  # no servers or API keys
    labels = [tab.model_combo.itemText(i) for i in range(tab.model_combo.count())]
    assert any(t.startswith("Chinese → English") and "160 MB download" in t for t in labels)
    assert any("Hy-MT2" in t and "1,157 MB download" in t for t in labels)
    tab.model_combo.setCurrentIndex(tab.model_combo.findData("hy-mt2-1.8b-q4"))
    assert not tab.details.isHidden() and tab.foreign_check.isEnabled()
    assert tab.status_label.text() == "Will download ~1,157 MB when applied"
    assert "Apache-2.0" in tab.license_label.text() and "CPU cores" in tab.notes_label.text()
    # The default recognizer only writes English: say so, since there'd be nothing to translate.
    assert "only writes English" in tab.recognition_warning.text()
    tab.foreign_check.setChecked(False)
    button(dialog, QDialogButtonBox.StandardButton.Apply).click()
    assert (emitted[-1].translation.model, emitted[-1].translation.only_foreign) == ("hy-mt2-1.8b-q4", False)


def test_translation_tab_shows_downloads_and_recognition_hints():
    folder = models_dir() / "opus-mt-zh-en-ctranslate2"  # the test's temporary models folder
    folder.mkdir(parents=True)
    (folder / ".complete").touch()
    cfg = AppConfig()
    cfg.translation.model = "opus-mt-zh-en"
    cfg.asr.model, cfg.asr.language = "sense-voice-int8", ""
    dialog, _ = make_dialog(cfg)
    tab = dialog.translation_tab
    assert tab.model_combo.currentText() == "Chinese → English (Opus-MT, light)"  # no download note
    assert tab.status_label.text() == "Downloaded" and tab.license_label.text() == "CC-BY-4.0"
    assert tab.recognition_warning.isHidden()
    cfg = AppConfig()
    cfg.translation.model = "qwen2.5:7b"  # an old LLM-server setting: shows and saves as Off
    dialog, emitted = make_dialog(cfg)
    assert dialog.translation_tab.model_combo.currentText() == "Off"
    dialog.apply()
    assert emitted[-1].translation.model == ""


def test_translate_task_only_for_models_that_can_translate():
    dialog, emitted = make_dialog(AppConfig())
    rec = dialog.recognition_tab
    rec.model_combo.setCurrentIndex(rec.model_combo.findData("fw-medium"))
    assert rec.task_combo.isEnabled()
    rec.task_combo.setCurrentIndex(rec.task_combo.findData("translate"))
    rec.model_combo.setCurrentIndex(rec.model_combo.findData("fw-large-v3-turbo"))
    assert not rec.task_combo.isEnabled()
    button(dialog, QDialogButtonBox.StandardButton.Apply).click()
    assert emitted[-1].asr.task == "transcribe"  # Turbo can't translate, so it was reset


def test_live_text_choice():
    dialog, emitted = make_dialog(AppConfig())
    rec = dialog.recognition_tab
    assert rec.live_combo.isEnabled()  # default model is English
    assert rec.live_combo.itemData(0) == rec.live_combo.currentData() == ""  # cheapest: listed first, default
    texts = [rec.live_combo.itemText(i) for i in range(rec.live_combo.count())]
    assert all(t.count("(") == 1 and ")" in t for t in texts)  # each option says its trade-off
    rec.live_combo.setCurrentIndex(rec.live_combo.findData("nemotron-streaming-en-160ms-int8"))
    button(dialog, QDialogButtonBox.StandardButton.Apply).click()
    assert emitted[-1].asr.live_model == "nemotron-streaming-en-160ms-int8"
    rec.model_combo.setCurrentIndex(rec.model_combo.findData("sense-voice-int8"))
    assert not rec.live_combo.isEnabled()  # word-by-word is English-only


def test_level_meters_follow_the_running_sources():
    running = [SourceLevel("Default output", -32.0, 0.0, -30.0), SourceLevel("Discord", -58.0, 24.0, -62.0)]
    dialog = SettingsDialog(AppConfig(), levels=lambda: list(running))
    audio = dialog.audio_tab
    assert [value.text() for _, value in audio._level_rows] == ["−32 dB", "−58 dB · boost +24 dB"]
    assert audio._level_rows[1][0].db == -58.0
    assert audio.quiet_label.isVisibleTo(dialog) and "Discord" in audio.quiet_label.text()
    assert not audio.idle_label.isVisibleTo(dialog)
    running[1] = SourceLevel("Discord", -140.0, 24.0, None)  # silent now, nobody spoke lately
    audio.refresh_levels()
    assert audio._level_rows[1][1].text() == "silent · boost +24 dB"
    assert audio._level_rows[1][0].db == -58.0 - LevelMeter.FALL_DB  # falls back gently
    assert not audio.quiet_label.isVisibleTo(dialog)
    running.clear()  # captions paused
    audio.refresh_levels()
    assert audio._level_rows == [] and audio.idle_label.isVisibleTo(dialog)

    broken = SettingsDialog(AppConfig(), levels=lambda: 1 / 0)  # a failing provider must not break the tab
    assert broken.audio_tab.idle_label.isVisibleTo(broken)
    plain = SettingsDialog(AppConfig())  # no provider at all
    assert plain.audio_tab.idle_label.isVisibleTo(plain)


def test_boost_checkbox_and_shortest_speech():
    dialog, emitted = make_dialog(AppConfig())
    assert dialog.audio_tab.gain_check.isChecked()
    assert dialog.timing_tab.spins["min_speech"].value() == PipelineSettings().min_speech
    dialog.audio_tab.gain_check.setChecked(False)
    dialog.timing_tab.spins["min_speech"].setValue(0.3)
    dialog.apply()
    assert (emitted[-1].pipeline.auto_gain, emitted[-1].pipeline.min_speech) == (False, 0.3)
    dialog.timing_tab.restore_button.click()  # only the Timing tab's own settings
    dialog.apply()
    assert (emitted[-1].pipeline.auto_gain, emitted[-1].pipeline.min_speech) == (False, 0.1)


def test_window_source_choice():
    cfg = AppConfig(sources=[SourceConfig(), SourceConfig(device="input:Microphone (Arctis)")])
    mic = cfg.sources[1].id
    dialog, emitted = make_dialog(cfg)
    win = dialog.windows_tab
    dialog.tabs.setCurrentWidget(win)
    win.view_list.setCurrentRow(0)  # the caption window
    assert win._form.isRowVisible(win._sources_field)
    assert [box.text() for box in win.source_checks] == [
        "1. Default output - what you hear", "2. Microphone (Arctis) - microphone"]
    assert all(box.isChecked() for box in win.source_checks)
    win.source_checks[1].setChecked(False)  # keep my own mic out of the big captions
    dialog.apply()
    assert emitted[-1].views[0].hidden_sources == [mic] and emitted[-1].views[1].hidden_sources == []
    win.view_list.setCurrentRow(1)  # the history window still shows both
    assert all(box.isChecked() for box in win.source_checks)
    win.view_list.setCurrentRow(0)
    assert [box.isChecked() for box in win.source_checks] == [True, False]
    win.source_checks[1].setChecked(True)
    dialog.apply()
    assert emitted[-1].views[0].hidden_sources == []

    # A source added on the Audio tab shows up (ticked) when the Windows tab is shown again.
    dialog.show()
    dialog.tabs.setCurrentWidget(dialog.audio_tab)
    dialog.audio_tab.add_button.click()
    dialog.tabs.setCurrentWidget(win)
    assert len(win.source_checks) == 3 and all(box.isChecked() for box in win.source_checks)
    dialog.close()


def test_window_source_choice_hidden_with_one_source():
    dialog, _ = make_dialog(AppConfig())
    win = dialog.windows_tab
    win.view_list.setCurrentRow(0)
    assert len(win.source_checks) == 1 and not win._form.isRowVisible(win._sources_field)


def test_downloaded_models_list_and_delete(monkeypatch):
    from voicerhino import paths
    from voicerhino.asr.download import local_name

    root = paths.models_dir()  # fake_system's temporary folder, never the real one
    assert root.parent.name != "VoiceRhino"
    assert settings.ASR_MODELS[0].id == AppConfig().asr.model  # the default: in use
    parakeet = root / local_name(settings.ASR_MODELS[0].source)
    nemotron = root / local_name(settings.STREAMING_MODELS[0].source)
    for folder, size in ((parakeet, 5000), (nemotron, 4000), (root / "some-old-model", 2000)):
        folder.mkdir(parents=True)
        (folder / "model.onnx").write_bytes(b"x" * size)
        (folder / ".complete").touch()

    dialog, _ = make_dialog(AppConfig())
    general = dialog.general_tab
    tree = general.models_tree
    rows = {tree.topLevelItem(i).text(0): tree.topLevelItem(i) for i in range(tree.topLevelItemCount())}
    status = {name: item.text(2) for name, item in rows.items()}
    assert status == {settings.ASR_MODELS[0].name: "In use", settings.STREAMING_MODELS[0].name: "Not in use",
                      "some-old-model": "Not used by this version"}
    assert not rows[settings.ASR_MODELS[0].name].flags() & Qt.ItemFlag.ItemIsSelectable  # can't delete it
    assert general.models_total.text() == "11 KB in total, 6 KB not in use"

    answers = []
    monkeypatch.setattr(settings.QMessageBox, "question",
                        lambda *args: answers.append(args[2]) or settings.QMessageBox.StandardButton.No)
    general.delete_unused_button.click()
    assert len(answers) == 1 and "some-old-model" in answers[0] and "frees 6 KB" in answers[0]
    assert nemotron.exists()  # answered No

    monkeypatch.setattr(settings.QMessageBox, "question", lambda *args: settings.QMessageBox.StandardButton.Yes)
    assert not general.delete_model_button.isEnabled()
    rows[settings.STREAMING_MODELS[0].name].setSelected(True)
    assert general.delete_model_button.isEnabled()
    general.delete_model_button.click()
    assert not nemotron.exists() and parakeet.exists() and (root / "some-old-model").exists()
    assert tree.topLevelItemCount() == 2

    # Picking another model in the dialog doesn't free the one the running app still uses.
    rec = dialog.recognition_tab
    rec.model_combo.setCurrentIndex(rec.model_combo.findData("moonshine-base-en-int8"))
    general.refresh_downloads()
    general.delete_unused_button.click()
    assert parakeet.exists() and not (root / "some-old-model").exists()
    assert not general.delete_unused_button.isEnabled()
    dialog.apply()  # now the app runs Moonshine, so Parakeet is free to go
    general.refresh_downloads()
    assert general.delete_unused_button.isEnabled()
    general.delete_unused_button.click()
    assert not parakeet.exists()


def test_window_audio_warnings_choice():
    dialog, emitted = make_dialog(AppConfig())
    win = dialog.windows_tab
    dialog.tabs.setCurrentWidget(win)
    win.view_list.setCurrentRow(0)  # the caption window
    assert win.warnings_check.isChecked()
    win.warnings_check.setChecked(False)
    dialog.apply()
    assert [v.show_warnings for v in emitted[-1].views] == [False, True]
    win.view_list.setCurrentRow(1)
    assert win.warnings_check.isChecked()
    win.view_list.setCurrentRow(0)
    assert not win.warnings_check.isChecked()


def test_window_text_opacity():
    dialog, emitted = make_dialog(AppConfig())
    win = dialog.windows_tab
    win.view_list.setCurrentRow(0)
    assert win.text_opacity_slider.value() == 100 and win.text_opacity_label.text() == "100%"
    assert win.text_opacity_slider.minimum() == 10  # never fully invisible
    win.text_opacity_slider.setValue(60)
    assert win.text_opacity_label.text() == "60%"
    dialog.apply()
    assert [v.text_opacity for v in emitted[-1].views] == [0.6, 1.0]
    win.view_list.setCurrentRow(1)
    assert win.text_opacity_label.text() == "100%"


def test_sources_are_numbered_and_reordered():
    cfg = AppConfig(sources=[SourceConfig(kind="app", app="Discord.exe"),
                             SourceConfig(kind="app", app="chrome.exe")])
    discord, chrome = (s.id for s in cfg.sources)
    dialog, emitted = make_dialog(cfg)
    audio = dialog.audio_tab
    audio.source_list.setCurrentRow(0)
    assert [audio.source_list.item(i).text() for i in range(2)] == [
        "1. Discord - app audio", "2. chrome - app audio"]
    assert not audio.up_button.isEnabled() and audio.down_button.isEnabled()
    audio.down_button.click()
    assert [audio.source_list.item(i).text() for i in range(2)] == [
        "1. chrome - app audio", "2. Discord - app audio"]
    assert audio.source_list.currentRow() == 1  # the moved source stays selected
    assert audio.up_button.isEnabled() and not audio.down_button.isEnabled()
    dialog.apply()
    assert [s.id for s in emitted[-1].sources] == [chrome, discord]
    audio.remove_button.click()  # one source left: nothing to tell apart, so no number
    assert audio.source_list.item(0).text() == "chrome - app audio"
    assert not audio.up_button.isEnabled() and not audio.down_button.isEnabled()


def test_window_source_number_choice():
    dialog, emitted = make_dialog(AppConfig())
    win = dialog.windows_tab
    dialog.tabs.setCurrentWidget(win)
    win.view_list.setCurrentRow(1)  # history
    assert not win.source_number_check.isChecked()
    win.source_number_check.setChecked(True)
    dialog.apply()
    assert [v.show_source_number for v in emitted[-1].views] == [False, True]
    win.view_list.setCurrentRow(0)  # a caption window doesn't offer it
    assert not win._form.isRowVisible(win._history_fields[2])


def test_general_tab_resources():
    dialog, _ = make_dialog(AppConfig())
    general = dialog.general_tab
    assert not general._form.isRowVisible(general._resources_field)  # no monitor handed over
    monitor = ResourceMonitor()
    general.set_resource_monitor(monitor)
    assert general._form.isRowVisible(general._resources_field)
    assert general.resources_label.text() == "Measuring…"
    now = time.monotonic()
    monitor._samples.extend([(now - 120, Usage(1.0, 10**9)), (now - 5, Usage(0.5, 10**9, 0.0, 0))])
    general.refresh_resources()
    assert general.resources_label.text().startswith("CPU 0.5 cores (")
    assert "of your CPU) · RAM" in general.resources_label.text()
    assert "0.5 cores over 1 min, 0.8 cores over 5 min" in general.resources_hint.text()


def test_window_resources_choice():
    dialog, emitted = make_dialog(AppConfig())
    win = dialog.windows_tab
    dialog.tabs.setCurrentWidget(win)
    win.view_list.setCurrentRow(1)  # history
    win.resources_check.setChecked(True)
    dialog.apply()
    assert [v.show_resources for v in emitted[-1].views] == [False, True]
    win.view_list.setCurrentRow(0)
    assert not win._form.isRowVisible(win._history_fields[3])
