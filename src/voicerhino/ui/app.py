"""The GUI app: tray icon, overlay windows, and the pipelines feeding them."""

from __future__ import annotations

import copy
import logging
import threading
import time
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from .. import config as config_io
from ..asr.catalog import DEFAULT_MODEL_ID, VAD_MODELS, get_model, supports_live_model
from ..asr.download import ensure_download
from ..asr.engines import Engine, EngineOptions, StreamingEngine, load_engine, load_streaming_engine
from ..audio.base import AudioSource
from ..config import AppConfig, SourceConfig, ViewConfig
from ..paths import data_dir
from ..pipeline import QUIET_DB, CaptionPipeline, SourceLevel
from ..transcript import TranscriptStore, Utterance
from ..translate import preload_runtime as preload_translation_runtime
from .overlay import OverlayWindow, make_window
from .theme import app_icon, apply_dark_theme
from .winutil import GlobalHotkey, autostart_enabled, set_autostart

log = logging.getLogger(__name__)


class _Signals(QObject):
    utterance = Signal(object)  # pipeline threads -> UI thread
    engine_ready = Signal(object, object, object)  # (engine, key, error)
    status = Signal(str)


def make_source(sc: SourceConfig) -> AudioSource:
    if sc.kind == "app":
        from ..audio.app_source import AppSource

        return AppSource(sc.app)
    if sc.kind == "file":
        from ..audio.file_source import FileSource

        return FileSource(Path(sc.file), realtime=True, loop=True)
    from ..audio.wasapi import WasapiSource

    return WasapiSource(sc.device)


class Controller(QObject):
    def __init__(self, app: QApplication, config: AppConfig, persist: bool = True):
        super().__init__()
        self.app = app
        self.config = config
        self.persist = persist  # False in demo mode, so a demo never overwrites real settings
        self.store = TranscriptStore()
        self.signals = _Signals()
        self.store.subscribe(self.signals.utterance.emit)
        self.signals.utterance.connect(self._on_utterance)
        self.signals.engine_ready.connect(self._on_engine_ready)
        self.signals.status.connect(self._set_status)
        self.windows: dict[str, OverlayWindow] = {}
        self.pipelines: list[CaptionPipeline] = []
        self.engine: Engine | None = None
        self.live_engine: StreamingEngine | None = None  # two-pass live line, when enabled
        self._engine_key: tuple | None = None
        self._loading_key: tuple | None = None
        self.speakers = None  # speakers.attribution.SpeakerAttributor when enabled
        self.translator = None  # translate.TranslationWorker when enabled
        preload_translation_runtime()  # before any speech engine: see its docstring
        self.status = "Starting…"
        self.warning: str | None = None  # shown at the top of every window, e.g. very quiet audio
        self._settings = None
        self._settings_snapshot: AppConfig | None = None
        self._save_timer = QTimer(self, singleShot=True, interval=1000, timeout=self.save)
        self._watchdog = QTimer(self, interval=2000, timeout=self._check_pipelines)
        self.hotkey = GlobalHotkey(self.toggle_lock)
        self.tray = QSystemTrayIcon(app_icon(config.captions_enabled), self)
        self.tray.setToolTip("VoiceRhino")
        self.tray.activated.connect(self._on_tray_activated)
        self._menu = QMenu()
        self._menu.aboutToShow.connect(self._build_tray_menu)
        self.tray.setContextMenu(self._menu)

    # --- lifecycle ---------------------------------------------------------
    def start(self, first_run: bool) -> None:
        self._sync_windows()
        if not self.hotkey.register(self.config.lock_hotkey):
            self._set_status(f"Hotkey {self.config.lock_hotkey} is unavailable; use the tray menu to lock")
        self.tray.show()
        self._watchdog.start()
        self._configure_translation()
        self._load_engine()
        if first_run:
            self.tray.showMessage(
                "VoiceRhino is running",
                f"Drag the caption windows where you want them, then press {self.config.lock_hotkey} "
                "to lock them (click-through). Right-click the CC tray icon for settings.",
                app_icon(), 15000,
            )

    def shutdown(self) -> None:
        self._stop_pipelines()
        if self.translator is not None:
            self.translator.stop()
        if self.speakers is not None:
            self.speakers.close()
        self.hotkey.unregister()
        self.save()
        self.tray.hide()

    def save(self) -> None:
        if self.persist:
            config_io.save(self.config)

    def _save_soon(self, *_args) -> None:
        self._save_timer.start()

    # --- status ------------------------------------------------------------
    def _set_status(self, text: str, seconds: float = 8.0) -> None:
        self.status = text
        self.tray.setToolTip(f"VoiceRhino — {text}"[:120])
        log.info("status: %s", text)
        for w in self.windows.values():
            w.show_status(text, seconds)

    # --- recognition engine -----------------------------------------------
    def _engine_options(self) -> EngineOptions:
        a = self.config.asr
        return EngineOptions(num_threads=a.threads, device=a.device, language=a.language, task=a.task)

    def _live_spec(self, spec):
        """The streaming model for two-pass captions, if configured and compatible."""
        if not self.config.asr.live_model or not supports_live_model(spec):
            return None
        try:
            return get_model(self.config.asr.live_model)
        except KeyError:
            return None

    def _load_engine(self) -> None:
        try:
            spec = get_model(self.config.asr.model)
        except KeyError:
            spec = get_model(DEFAULT_MODEL_ID)
        options = self._engine_options()
        live_spec = self._live_spec(spec)
        key = (spec.id, tuple(asdict(options).items()), live_spec.id if live_spec else None)
        if self.engine is not None and key == self._engine_key:
            self._start_pipelines()
            return
        if key == self._loading_key:
            return
        self._loading_key = key
        self._stop_pipelines()
        self._set_status(f"Loading {spec.name}…", 60)

        def work() -> None:
            last = [0.0]

            def progress_for(name: str):
                def progress(done: int, total: int) -> None:
                    if time.monotonic() - last[0] > 0.5 and total:
                        last[0] = time.monotonic()
                        self.signals.status.emit(f"Downloading {name}: {done / total:.0%} of {total / 1e6:.0f} MB")
                return progress

            try:
                ensure_download(VAD_MODELS["silero"])
                engine = load_engine(spec, options, progress_for(spec.name))
            except Exception as exc:
                log.exception("loading %s failed", spec.id)
                self.signals.engine_ready.emit(None, key, str(exc))
                return
            live = None
            if live_spec is not None:
                try:
                    live = load_streaming_engine(live_spec, 1, progress_for(live_spec.name))
                except Exception as exc:  # captions still work without word-by-word live text
                    log.exception("loading %s failed", live_spec.id)
                    self.signals.status.emit(f"Word-by-word live text unavailable: {exc}")
            self.signals.engine_ready.emit((engine, live), key, None)

        threading.Thread(target=work, name="model-loader", daemon=True).start()

    def _on_engine_ready(self, engines: tuple | None, key: tuple, error: str | None) -> None:
        if key != self._loading_key:  # superseded by a newer settings change
            if engines is not None:
                engines[0].close()
            return
        self._loading_key = None
        if engines is None:
            self._set_status(f"Couldn't load the model: {error}", 30)
            return
        if self.engine is not None:
            self.engine.close()
        (self.engine, self.live_engine), self._engine_key = engines, key
        self._start_pipelines()

    # --- pipelines ---------------------------------------------------------
    def _start_pipelines(self) -> None:
        self._stop_pipelines()
        if self.engine is None or not self.config.captions_enabled:
            if not self.config.captions_enabled:
                self._set_status("Captions paused")
            return
        vad = ensure_download(VAD_MODELS["silero"])
        enabled = [s for s in self.config.sources if s.enabled]
        self._update_source_labels()
        speaker_fn = self._speaker_fn()
        started, failed = [], []
        for sc in enabled:
            try:
                pipe = CaptionPipeline(make_source(sc), self.engine, vad, self.config.pipeline,
                                       self.store.push, sc.describe(), speaker_fn, self.live_engine,
                                       source_id=sc.id)
            except Exception as exc:
                log.exception("couldn't create %s", sc.describe())
                failed.append(f"{sc.describe()}: {exc}")
                continue
            try:
                pipe.start()
                started.append(sc.describe())
            except Exception as exc:
                log.warning("couldn't open %s: %s", sc.describe(), exc)
                failed.append(f"{sc.describe()}: {exc}")
                if not hasattr(pipe.source, "stale"):
                    pipe.stop()
                    continue
                # Devices that can come back (unplugged headset) are retried by the watchdog.
            self.pipelines.append(pipe)
        if failed:
            self._set_status("Audio problem — " + "; ".join(failed), 30)
        elif started:
            live = " + word-by-word" if self.live_engine is not None else ""
            self._set_status(f"Listening to {', '.join(started)} · {self.engine.spec.name.split(' (')[0]}{live}")
        else:
            self._set_status("No audio source enabled — open Settings › Audio", 30)

    def _stop_pipelines(self) -> None:
        for pipe in self.pipelines:
            pipe.stop()
        self.pipelines.clear()

    def _check_pipelines(self) -> None:
        self._ticks = getattr(self, "_ticks", 0) + 1
        if self.translator is not None and self.translator.error:
            self._set_status(f"Translation failed: {self.translator.error}", 10)
            self.translator.error = None
        for pipe in self.pipelines:
            if pipe.error:
                self._set_status(f"Captioning stopped: {pipe.error}", 30)
                pipe.error = None
            stale = getattr(pipe.source, "stale", None)
            # Checking the default device enumerates hardware: only every ~6 s.
            if stale is not None and self._ticks % 3 == 0 and stale():
                if pipe.restart_source():
                    self._set_status(f"Reconnected to {pipe.source.label}")
                else:
                    self._set_status(f"Waiting for audio device: {pipe.source.label}", 6)
        self._check_quiet()

    def levels(self) -> list[SourceLevel]:
        """What each running source sounds like (Settings › Audio meter)."""
        return [info for pipe in self.pipelines if (info := pipe.level_info()) is not None]

    def _check_quiet(self) -> None:
        """Warn on every window while recent speech was too quiet even for the boost:
        a deaf user can't hear that Discord or the volume got turned down."""
        quiet = [i for i in self.levels() if i.speech_db is not None and i.speech_db < QUIET_DB]
        warning = None
        if quiet:
            warning = f"⚠ {quiet[0].name} is very quiet ({quiet[0].speech_db:.0f} dB): turn its volume up"
        if warning != self.warning:
            if warning:
                log.info("quiet audio: %s", warning)
            self.warning = warning
            for w in self.windows.values():
                w.set_warning(warning)

    def _speaker_fn(self):
        s = self.config.speakers
        if not (s.voice_enabled or s.discord_enabled):
            self.speakers = None
            return None
        try:
            from ..speakers.attribution import SpeakerAttributor

            if self.speakers is None:
                self.speakers = SpeakerAttributor(s, on_status=self.signals.status.emit)
            else:
                self.speakers.configure(s)
            return self.speakers
        except Exception as exc:
            log.exception("speaker attribution unavailable")
            self._set_status(f"Speaker colors unavailable: {exc}", 20)
            return None

    def _configure_translation(self) -> None:
        if self.translator is not None:
            self.translator.stop()
            self.translator = None
        if self.config.translation.model:
            from ..translate import TranslationWorker

            try:
                self.translator = TranslationWorker(self.store, copy.deepcopy(self.config.translation),
                                                    on_status=self.signals.status.emit)
            except KeyError:
                self._set_status(f"Unknown translation model {self.config.translation.model!r}: "
                                 "pick one in Settings › Translation", 20)

    def _on_utterance(self, utt: Utterance) -> None:
        # Captions only ever live in memory (TranscriptStore): nothing anyone says is written to
        # disk. A "save a transcript" option was removed on purpose (recording-consent laws).
        for w in self.windows.values():
            if w.isVisible():
                w.on_utterance(utt)
        # A fresh final goes to translation; translations come back later as updates.
        if utt.final and utt.text and utt.translation is None and self.translator is not None:
            self.translator.submit(utt)

    # --- windows -----------------------------------------------------------
    def _sync_windows(self) -> None:
        wanted = {v.id: v for v in self.config.views}
        for vid in list(self.windows):
            if vid not in wanted or self.windows[vid].view.kind != wanted[vid].kind:
                self.windows.pop(vid).deleteLater()
        for view in self.config.views:
            w = self.windows.get(view.id)
            if w is None:
                w = make_window(view, self.store)
                w.geometry_saved.connect(self._on_geometry_saved)
                w.menu_requested.connect(self._show_window_menu)
                self.windows[view.id] = w
            w.apply_view(view)
            w.set_locked(self.config.locked)
            w.set_warning(self.warning)
            w.setVisible(view.visible)
        self._update_source_labels()

    def _update_source_labels(self) -> None:
        """Label lines with their source name in windows that show more than one running source."""
        enabled = [s.id for s in self.config.sources if s.enabled]
        for w in self.windows.values():
            w.multi_source = len([i for i in enabled if i not in w.view.hidden_sources]) > 1
            w.update()

    def _on_geometry_saved(self, view: ViewConfig) -> None:
        if self._settings is not None and self._settings.isVisible():
            self._settings.update_view_geometry(view.id, view.x, view.y, view.w, view.h)
        self._save_soon()

    def set_locked(self, locked: bool) -> None:
        self.config.locked = locked
        for w in self.windows.values():
            w.set_locked(locked)
        self._save_soon()

    def toggle_lock(self) -> None:
        self.set_locked(not self.config.locked)

    def set_captions_enabled(self, enabled: bool) -> None:
        self.config.captions_enabled = enabled
        self.tray.setIcon(app_icon(enabled))
        if enabled:
            self._load_engine()
        else:
            self._stop_pipelines()
            self._set_status("Captions paused")
        self._save_soon()

    def add_view(self, kind: str) -> None:
        view = ViewConfig.default_history() if kind == "history" else ViewConfig()
        self.config.views.append(view)
        self.config.locked = False  # new windows need positioning
        self._sync_windows()
        self._save_soon()

    def remove_view(self, window: OverlayWindow) -> None:
        self.config.views = [v for v in self.config.views if v.id != window.view.id]
        self._sync_windows()
        self._save_soon()

    def set_view_visible(self, view: ViewConfig, visible: bool) -> None:
        view.visible = visible
        self.windows[view.id].setVisible(visible)
        self._save_soon()

    def _view_names(self) -> dict[str, str]:
        names, counts = {}, {"caption": 0, "history": 0}
        for v in self.config.views:
            counts[v.kind] += 1
            names[v.id] = f"{'Caption' if v.kind == 'caption' else 'History'} window {counts[v.kind]}"
        return names

    # --- menus -------------------------------------------------------------
    def _common_actions(self, menu: QMenu) -> None:
        lock = menu.addAction(f"Lock windows (click-through)\t{self.config.lock_hotkey}")
        lock.setCheckable(True)
        lock.setChecked(self.config.locked)
        lock.toggled.connect(self.set_locked)
        menu.addAction("Clear captions", self._clear)
        menu.addAction("Settings…", self.open_settings)

    def _clear(self) -> None:
        self.store.clear()
        for w in self.windows.values():
            w.update()

    def _build_tray_menu(self) -> None:
        m = self._menu
        m.clear()
        status = m.addAction(self.status)
        status.setEnabled(False)
        m.addSeparator()
        on = m.addAction("Captions on")
        on.setCheckable(True)
        on.setChecked(self.config.captions_enabled)
        on.toggled.connect(self.set_captions_enabled)
        self._common_actions(m)
        windows = m.addMenu("Windows")
        names = self._view_names()
        for view in self.config.views:
            a = windows.addAction(names[view.id])
            a.setCheckable(True)
            a.setChecked(view.visible)
            a.toggled.connect(lambda checked, v=view: self.set_view_visible(v, checked))
        windows.addSeparator()
        windows.addAction("Add caption window", lambda: self.add_view("caption"))
        windows.addAction("Add history window", lambda: self.add_view("history"))
        auto = m.addAction("Start with Windows")
        auto.setCheckable(True)
        auto.setChecked(autostart_enabled())
        auto.toggled.connect(set_autostart)
        m.addSeparator()
        m.addAction("Quit VoiceRhino", self.app.quit)

    def _show_window_menu(self, window: OverlayWindow, pos) -> None:
        menu = QMenu()
        self._common_actions(menu)
        menu.addSeparator()
        menu.addAction("Hide this window", lambda: self.set_view_visible(window.view, False))
        menu.addAction("Remove this window", lambda: self.remove_view(window))
        menu.exec(pos)

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.DoubleClick:
            self.open_settings()

    # --- settings ----------------------------------------------------------
    def open_settings(self) -> None:
        from .settings import SettingsDialog

        if self._settings is not None and self._settings.isVisible():
            self._settings.raise_()
            self._settings.activateWindow()
            return
        self._settings_snapshot = copy.deepcopy(self.config)
        self._settings = SettingsDialog(self.config, levels=self.levels)
        self._settings.applied.connect(self.apply_config)
        self._settings.show()

    def apply_config(self, new: AppConfig) -> None:
        old = self.config
        # Things changed from the tray/hotkey while the dialog was open win, unless
        # the dialog itself changed them too. (Window drags reach the dialog via
        # update_view_geometry.)
        snap = self._settings_snapshot or old
        if new.locked == snap.locked:
            new.locked = old.locked
        if new.captions_enabled == snap.captions_enabled:
            new.captions_enabled = old.captions_enabled
        if [v.visible for v in new.views] == [v.visible for v in snap.views]:
            live = {v.id: v.visible for v in old.views}
            for v in new.views:
                v.visible = live.get(v.id, v.visible)
        self._settings_snapshot = copy.deepcopy(new)
        self.config = new
        self._sync_windows()
        if new.lock_hotkey != old.lock_hotkey and not self.hotkey.register(new.lock_hotkey):
            self._set_status(f"Hotkey {new.lock_hotkey} is unavailable", 15)
        self.tray.setIcon(app_icon(new.captions_enabled))
        if asdict(new.translation) != asdict(old.translation):
            self._configure_translation()
        if asdict(new.asr) != asdict(old.asr) or (new.captions_enabled and not old.captions_enabled):
            self._load_engine()
        elif (asdict(new.pipeline) != asdict(old.pipeline) or asdict(new.speakers) != asdict(old.speakers)
              or [asdict(s) for s in new.sources] != [asdict(s) for s in old.sources]
              or new.captions_enabled != old.captions_enabled):
            self._start_pipelines()
        self.save()


def run_gui(reset: bool = False, file: Path | None = None) -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("VoiceRhino")
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(app_icon())
    apply_dark_theme(app)

    lock = QLockFile(str(data_dir() / "voicerhino.lock"))
    if not lock.tryLock(100):
        QMessageBox.information(None, "VoiceRhino", "VoiceRhino is already running — look for the CC icon in the tray.")
        return 0

    first_run = reset or not config_io.config_path().exists()
    config = AppConfig() if reset else config_io.load()
    if file is not None:  # demo mode: caption a WAV file on loop instead of live audio
        config.sources = [SourceConfig(kind="file", file=str(file))]
    controller = Controller(app, config, persist=file is None)
    controller.start(first_run)
    app.aboutToQuit.connect(controller.shutdown)
    code = app.exec()
    lock.unlock()
    return code
