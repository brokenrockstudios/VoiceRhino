"""Settings dialog: edits a copy of AppConfig; Apply/OK emit it for the app to apply live.

Tested with tests/test_settings_dialog.py (offscreen Qt: drives the widgets
programmatically and checks Apply/OK/Cancel, the unchanged round trip, a failing
device list and the per-app list) and tools/screenshot_settings.py, which
renders every tab with a dark Fusion palette to tests/_out/settings_<tab>.png
for a visual check.

Widgets write into the working copy only on user edits; filling them from the
config never writes back, so opening the dialog and pressing Apply returns the
config unchanged.
"""

from __future__ import annotations

import copy
import dataclasses
import importlib.util
import logging
import os
import re
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Callable, Iterator

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPalette,
    QPixmap,
    QScreen,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFontComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import voicerhino

from .. import __version__, paths
from ..asr import download, storage
from ..asr.catalog import ASR_MODELS, STREAMING_MODELS, ModelSpec, get_model, supports_live_model
from ..audio import devices as audio_devices
from ..audio.devices import DEFAULT_OUTPUT_KEY, DeviceInfo
from ..audio.gain import SILENCE_DB
from ..config import AppConfig, SourceConfig, ViewConfig
from ..pipeline import QUIET_DB, PipelineSettings, SourceLevel
from ..translate import is_model_downloaded
from ..translate.catalog import TRANSLATION_MODELS, TranslationModel

LevelProvider = Callable[[], "list[SourceLevel]"]

log = logging.getLogger(__name__)

WINDOW_TITLE = "VoiceRhino Settings"
DEFAULT_APP = "Discord.exe"

# Audio tab: (ui kind, combobox label, tooltip). "output"/"input" are both kind="device".
SOURCE_KINDS: list[tuple[str, str, str]] = [
    ("output", "Output device - what you hear",
     "Captions everything that plays on a speaker or headset (a loopback of that device)."),
    ("input", "Input device / microphone", "Captions a microphone or line-in."),
    ("app", "Single app (e.g. Discord only)",
     "Captions one program's audio (and its child processes) without anything else, "
     "e.g. Discord without game sounds."),
    ("file", "WAV file (testing)", "Loops a WAV file through the recognizer, for testing."),
]
_KIND_SUFFIX = {"output": "what you hear", "input": "microphone", "app": "app audio", "file": "WAV file"}

LANGUAGES: list[tuple[str, str]] = [
    ("English (en)", "en"),
    ("Auto-detect", ""),
    ("Chinese (zh)", "zh"),
    ("Japanese (ja)", "ja"),
    ("Korean (ko)", "ko"),
    ("Spanish (es)", "es"),
    ("French (fr)", "fr"),
    ("German (de)", "de"),
    ("Cantonese (yue)", "yue"),
]
TASKS = [("Transcribe", "transcribe"), ("Translate to English", "translate")]
REDECODE_LABEL = "Re-decode with the main model every 0.4 s (cheapest, quickest first word; words can change)"
ASR_DEVICES = [("CPU", "cpu"), ("GPU (CUDA)", "cuda")]

# Timing tab: (field, label, minimum, maximum, step, decimals, suffix, text at minimum, hint)
TIMING_FIELDS: list[tuple[str, str, float, float, float, int, str, str, str]] = [
    ("partial_interval", "Live update every", 0.0, 2.0, 0.1, 1, " s", "Off",
     "How often the words still being spoken are re-read and shown. Lower feels snappier "
     "but costs more CPU; Off shows finished lines only."),
    ("min_silence", "Pause that ends a line", 0.2, 2.0, 0.05, 2, " s", "",
     "How long a pause finishes the current line. Shorter gives final text sooner but can "
     "split a sentence in two."),
    ("max_utterance", "Longest line", 4.0, 30.0, 1.0, 0, " s", "",
     "A line is finished after this long even if the speaker keeps talking."),
    ("vad_threshold", "Speech detection threshold", 0.1, 0.9, 0.05, 2, "", "",
     "How sure the voice detector must be that it hears speech. Lower catches quiet voices "
     "(and more noise); higher ignores more game sounds."),
    ("min_speech", "Shortest speech", 0.05, 1.0, 0.05, 2, " s", "",
     "Sounds shorter than this are ignored. Lower keeps short words like \"yes\" or \"wait\"; "
     "higher skips more clicks and bumps."),
    ("pre_roll", "Audio kept before speech", 0.0, 1.0, 0.05, 2, " s", "",
     "Extra audio taken from just before speech was detected, so first words aren't cut off."),
]

_ACTIVE_ROLE = Qt.ItemDataRole.UserRole + 1
_NO_INPUT = "input:"  # "a microphone" while none is listed; replaced once one shows up


# ---------------------------------------------------------------- helpers ---


def discord_setup_doc() -> Path:
    """docs/discord-setup.md in the checkout VoiceRhino runs from."""
    return Path(voicerhino.__file__).resolve().parents[2] / "docs" / "discord-setup.md"


def model_status(spec: ModelSpec) -> str:
    if spec.family == "faster_whisper":
        if importlib.util.find_spec("faster_whisper") is None:
            return "Needs the GPU extra (uv sync --extra gpu); downloads on first use"
        return f"GPU extra installed; downloads ~{spec.size_mb} MB on first use"
    try:
        if download.is_downloaded(spec.source):
            return "Downloaded"
    except Exception:
        log.exception("couldn't check whether %s is downloaded", spec.id)
    return f"Will download ~{spec.size_mb} MB when applied"


def place_view(view: ViewConfig, area: QRect) -> None:
    """Fit a window onto a screen's usable area (taskbar excluded).

    Captions: ~70% of the screen wide, centered, near the bottom. History: at the
    right edge, vertically centered.
    """
    h = min(view.h, area.height())
    if view.kind == "history":
        w = min(view.w, area.width())
        x = area.x() + area.width() - w - max(16, area.width() // 60)
        y = area.y() + (area.height() - h) // 2
    else:
        w = round(area.width() * 0.7)
        x = area.x() + (area.width() - w) // 2
        y = max(area.y(), area.y() + area.height() - h - max(16, area.height() // 12))
    if (x, y) == (-1, -1):  # that pair means "place automatically"
        x = -2
    view.x, view.y, view.w, view.h = int(x), int(y), int(w), int(h)


def _running_process_names() -> list[str]:
    """Unique .exe names of running programs, Windows' own system processes left out."""
    try:
        import psutil

        windir = os.environ.get("SystemRoot", r"C:\Windows").rstrip("\\").lower() + "\\"
        names: dict[str, str] = {}
        for proc in psutil.process_iter(["name", "exe"]):
            name = proc.info.get("name") or ""
            exe = proc.info.get("exe") or ""
            if name.lower().endswith(".exe") and exe and not exe.lower().startswith(windir):
                names.setdefault(name.lower(), name)
        return sorted(names.values(), key=str.lower)
    except Exception:
        log.exception("couldn't list running processes")
        return []


def list_app_choices() -> list[tuple[str, bool]]:
    """(exe name, playing audio now) to offer for single-app capture, active first.

    Uses audio.process_loopback.list_audio_apps() (apps with an audio session)
    when that module is available, else every running program via psutil.
    """
    try:
        from ..audio.process_loopback import list_audio_apps
    except Exception:  # not written yet / its Windows APIs failed to import
        list_audio_apps = None
    if list_audio_apps is not None:
        try:
            apps: dict[str, tuple[str, bool]] = {}
            for app in list_audio_apps():
                exe = Path(str(getattr(app, "exe", "") or "")).name
                if not exe:
                    continue
                active = bool(getattr(app, "active", False))
                name, was_active = apps.get(exe.lower(), (exe, False))
                apps[exe.lower()] = (name, active or was_active)
            if apps:
                return sorted(apps.values(), key=lambda a: (not a[1], a[0].lower()))
        except Exception:
            log.exception("list_audio_apps failed; listing running processes instead")
    return [(name, False) for name in _running_process_names()]


def parse_language(text: str) -> str:
    """Combobox text -> language code: 'English (en)' -> 'en', 'Auto-detect' -> '', 'it' -> 'it'."""
    t = text.strip()
    low = t.lower()
    for label, code in LANGUAGES:
        if low in (label.lower(), label.split(" (")[0].lower()) or (code and low == code):
            return code
    if low in ("auto", "autodetect", "detect"):
        return ""
    if m := re.fullmatch(r".*\(\s*([A-Za-z-]+)\s*\)", t):
        return m.group(1).lower()
    return low


def _ui_kind(src: SourceConfig) -> str:
    if src.kind in ("app", "file"):
        return src.kind
    return "input" if src.device.startswith("input:") else "output"


def _source_title(src: SourceConfig) -> str:
    kind = _ui_kind(src)
    name = src.describe() or ("Not picked yet" if kind == "input" else "(no name)")
    return f"{name} - {_KIND_SUFFIX[kind]}"


def _view_titles(views: list[ViewConfig]) -> list[str]:
    counts = {"caption": 0, "history": 0}
    titles = []
    for view in views:
        kind = "history" if view.kind == "history" else "caption"
        counts[kind] += 1
        titles.append(f"{kind.capitalize()} window {counts[kind]}")
    return titles


def _screen_text(index: int, screen: QScreen) -> str:
    name = screen.name().removeprefix("\\\\.\\").strip()
    ratio = screen.devicePixelRatio()
    size = screen.size()
    text = f"Screen {index + 1}" + (f": {name}" if name else "")
    text += f" ({round(size.width() * ratio)} x {round(size.height() * ratio)}"
    if screen == QGuiApplication.primaryScreen():
        text += ", main"
    return text + ")"


def _int(value, fallback: int) -> int:
    """Hand-edited config values may be floats or strings; show something sane instead of crashing."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback


def _float(value, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _blend(a: QColor, b: QColor, t: float) -> QColor:
    return QColor.fromRgbF(
        a.redF() + (b.redF() - a.redF()) * t,
        a.greenF() + (b.greenF() - a.greenF()) * t,
        a.blueF() + (b.blueF() - a.blueF()) * t,
    )


def _open_folder(folder: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


# ---------------------------------------------------------------- widgets ---


class Hint(QLabel):
    """Small explanatory text, muted against whatever palette is active (light or dark)."""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setWordWrap(True)
        font = self.font()
        if font.pointSizeF() > 0:
            font.setPointSizeF(font.pointSizeF() * 0.92)
            self.setFont(font)
        self._muting = False
        self._mute()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ParentChange) and not self._muting:
            self._mute()

    def _mute(self) -> None:
        # Blend text toward the background instead of trusting PlaceholderText or the
        # Disabled group: hand-made dark palettes often leave those at light-theme values.
        self._muting = True
        try:
            parent = self.parentWidget()
            source = parent.palette() if parent is not None else QApplication.palette()
            background = source.color(QPalette.ColorRole.Window)
            palette = QPalette()  # sets only WindowText, inherits every other role
            for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
                text = source.color(group, QPalette.ColorRole.WindowText)
                palette.setColor(group, QPalette.ColorRole.WindowText, _blend(text, background, 0.35))
            disabled = source.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText)
            palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText,
                             _blend(disabled, background, 0.5))
            self.setPalette(palette)
        finally:
            self._muting = False


class ColorButton(QPushButton):
    """Shows a color swatch and its hex code; clicking opens a color picker."""

    colorChanged = Signal(str)

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._title = title
        self._color = "#000000"
        self.setIconSize(QSize(30, 14))
        self.setMinimumWidth(130)
        self.setToolTip(f"Choose the {title.lower()}")
        self.clicked.connect(self._choose)
        self.set_color(self._color)

    def color(self) -> str:
        return self._color

    def set_color(self, color: str) -> None:
        """Show color without emitting colorChanged."""
        self._color = color
        qcolor = QColor(color)
        ratio = max(1.0, self.devicePixelRatioF())
        size = self.iconSize()
        pixmap = QPixmap(round(size.width() * ratio), round(size.height() * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(qcolor if qcolor.isValid() else QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setPen(QColor(128, 128, 128))  # visible on both light and dark buttons
        painter.drawRect(QRect(0, 0, size.width() - 1, size.height() - 1))
        painter.end()
        self.setIcon(QIcon(pixmap))
        self.setText(qcolor.name().upper() if qcolor.isValid() else color)

    def choose(self, color: str) -> None:
        """Set a color as if the user picked it (emits colorChanged when it differs)."""
        if QColor(color).isValid() and color.upper() != self._color.upper():
            self.set_color(color)
            self.colorChanged.emit(color)

    def _choose(self) -> None:
        picked = QColorDialog.getColor(QColor(self._color), self, self._title)
        if picked.isValid():
            self.choose(picked.name().upper())


class LevelMeter(QWidget):
    """A horizontal audio level bar from -70 to 0 dB. The "very quiet" zone is shaded."""

    LOW, HIGH = -70.0, 0.0
    FALL_DB = 2.0  # per update: the bar drops back gently instead of flickering

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = self.LOW
        self.setMinimumSize(120, 14)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(220, 14)

    def set_db(self, db: float) -> None:
        self.db = max(min(db, self.HIGH), self.db - self.FALL_DB, self.LOW)
        self.update()

    def _x(self, r: QRectF, db: float) -> float:
        return r.left() + r.width() * (db - self.LOW) / (self.HIGH - self.LOW)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        r = QRectF(self.rect()).adjusted(0.5, 1.5, -0.5, -1.5)
        palette = self.palette()
        p.fillRect(r, palette.color(QPalette.ColorRole.Base))
        quiet = QRectF(r.left(), r.top(), self._x(r, QUIET_DB) - r.left(), r.height())
        p.fillRect(quiet, QColor(217, 163, 59, 45))
        color = QColor("#d9a33b") if self.db < QUIET_DB else QColor("#4caf50") if self.db < -6 else QColor("#e05252")
        p.fillRect(QRectF(r.left(), r.top(), self._x(r, self.db) - r.left(), r.height()), color)
        p.setPen(palette.color(QPalette.ColorRole.Mid))
        p.drawRect(r)


class _AppItemDelegate(QStyledItemDelegate):
    """Marks apps that are playing audio right now in the app dropdown (not in the edit field)."""

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        if index.data(_ACTIVE_ROLE):
            option.text = f"{option.text}   - playing audio now"


def _combo(editable: bool = False) -> QComboBox:
    combo = QComboBox()
    # Long device/model names must not force the dialog wider; the popup still shows them.
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(14)
    combo.setMaxVisibleItems(20)
    if editable:
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    return combo


def _form() -> QFormLayout:
    form = QFormLayout()
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.setHorizontalSpacing(14)
    form.setVerticalSpacing(8)
    return form


def _row(*items: QWidget | int | None, stretch: bool = False) -> QHBoxLayout:
    """Lay widgets out left to right. An int item is that widget's stretch factor for the
    widget before it; stretch=True adds trailing space so the widgets keep their natural size."""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    for item in items:
        if isinstance(item, int):
            row.setStretch(row.count() - 1, item)
        elif item is not None:
            row.addWidget(item)
    if stretch:
        row.addStretch(1)
    return row


def _field(content: QWidget | QLayout, hint: str | QLabel = "", warning: str = "") -> QWidget:
    """One form field: a widget (or row layout) with an optional hint (text or label) underneath,
    and an optional warning on its own line below that, in normal text so it isn't skimmed past."""
    box = QWidget()
    column = QVBoxLayout(box)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(3)
    if isinstance(content, QLayout):
        column.addLayout(content)
    else:
        column.addWidget(content)
    if hint:
        column.addWidget(hint if isinstance(hint, QLabel) else Hint(hint))
    if warning:
        label = QLabel(f"⚠ {warning}")
        label.setWordWrap(True)
        column.addWidget(label)
    return box


_SPIN_MIN_WIDTH = 84


def _spin(spin: QSpinBox | QDoubleSpinBox) -> QSpinBox | QDoubleSpinBox:
    """Give spinboxes one tidy minimum width (Fusion's own hint clips values with a suffix)."""
    spin.setMinimumWidth(max(_SPIN_MIN_WIDTH, spin.sizeHint().width()))
    return spin


def _check_item(text: str, checked: bool, tooltip: str) -> QListWidgetItem:
    item = QListWidgetItem(text)
    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
    item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
    item.setToolTip(tooltip)
    return item


def _set_combo_data(combo: QComboBox, value, missing_text: str | None = None) -> None:
    """Select the item whose data is value, adding one (labelled missing_text) if absent."""
    index = combo.findData(value)
    if index < 0 and missing_text is not None:
        combo.addItem(missing_text, value)
        index = combo.count() - 1
    if index >= 0:
        combo.setCurrentIndex(index)


# ------------------------------------------------------------------- tabs ---


class _Tab(QWidget):
    """A page editing part of the dialog's working AppConfig in place."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._cfg = AppConfig()  # replaced by load()
        self._loading = False

    def load(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        with self._quiet():
            self._load()

    def _load(self) -> None:
        raise NotImplementedError

    @contextmanager
    def _quiet(self) -> Iterator[None]:
        """Fill widgets without their change handlers writing back into the config."""
        previous, self._loading = self._loading, True
        try:
            yield
        finally:
            self._loading = previous


class AudioTab(_Tab):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._devices: list[DeviceInfo] = []
        self._apps: list[tuple[str, bool]] | None = None  # listed when first needed

        self.source_list = QListWidget()
        self.source_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        row_height = max(self.fontMetrics().height(), 16) + 6
        self.source_list.setMaximumHeight(row_height * 5 + 2 * self.source_list.frameWidth())
        self.source_list.setToolTip("Every ticked source is captioned. Most setups need just one.")
        self.source_list.currentRowChanged.connect(self._on_row_changed)
        self.source_list.itemChanged.connect(self._on_item_changed)
        self.add_button = QPushButton("Add")
        self.add_button.setToolTip("Caption another audio source at the same time")
        self.add_button.clicked.connect(self._on_add)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setToolTip("Remove the selected source (one always stays; untick it to pause it)")
        self.remove_button.clicked.connect(self._on_remove)

        self.kind_combo = _combo()
        for key, label, tooltip in SOURCE_KINDS:
            self.kind_combo.addItem(label, key)
            self.kind_combo.setItemData(self.kind_combo.count() - 1, tooltip, Qt.ItemDataRole.ToolTipRole)
        self.kind_combo.setToolTip("What this source listens to")
        self.kind_combo.currentIndexChanged.connect(self._on_kind_changed)

        self.device_combo = _combo()
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        self.app_combo = _combo(editable=True)
        self.app_combo.setItemDelegate(_AppItemDelegate(self.app_combo))
        self.app_combo.setToolTip("Program file name, e.g. Discord.exe. Its child processes are included.")
        self.app_combo.lineEdit().setPlaceholderText(DEFAULT_APP)
        self.app_combo.editTextChanged.connect(self._on_app_changed)
        self.file_edit = QLineEdit()
        self.file_edit.setPlaceholderText("Path to a .wav file")
        self.file_edit.textChanged.connect(self._on_file_changed)
        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self._on_browse)
        self.name_edit = QLineEdit()
        self.name_edit.setToolTip("Short name shown next to captions when several sources run")
        self.name_edit.textChanged.connect(self._on_name_changed)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setToolTip("List audio devices and running apps again")
        self.refresh_button.clicked.connect(self.refresh)
        self.app_refresh_button = QPushButton("Refresh")
        self.app_refresh_button.setToolTip(self.refresh_button.toolTip())
        self.app_refresh_button.clicked.connect(self.refresh)

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        icon = QLabel()
        icon.setPixmap(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning).pixmap(20, 20))
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.error_box = QWidget()
        error_row = QHBoxLayout(self.error_box)
        error_row.setContentsMargins(0, 0, 0, 0)
        error_row.addWidget(icon)
        error_row.addWidget(self.error_label, 1)
        self.error_box.setVisible(False)

        # What the running sources sound like, from the app's level provider (none in tests).
        self.gain_check = QCheckBox("Boost quiet audio automatically")
        self.gain_check.setToolTip("Turns up sources whose voices are very soft (by up to 24 dB) so they "
                                   "still get captioned. Audio at a normal level is left alone.")
        self.gain_check.toggled.connect(self._on_gain_toggled)
        self.idle_label = Hint("Not listening right now: captions are off or no source is ticked "
                               "(new sources start when you press Apply).")
        self.quiet_label = QLabel()
        self.quiet_label.setWordWrap(True)
        self.quiet_label.setVisible(False)
        self._level_grid = QGridLayout()
        self._level_grid.setContentsMargins(0, 0, 0, 0)
        self._level_grid.setHorizontalSpacing(10)
        self._level_grid.setColumnStretch(1, 1)
        self._level_names: list[str] = []
        self._level_rows: list[tuple[LevelMeter, QLabel]] = []
        self._levels: LevelProvider | None = None
        self._level_timer = QTimer(self, interval=100, timeout=self.refresh_levels)

        # layout
        sources = QGroupBox("Sources")
        buttons = QVBoxLayout()
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        source_row = QHBoxLayout(sources)
        source_row.addWidget(self.source_list, 1)
        source_row.addLayout(buttons)

        self.editor = QGroupBox("Selected source")
        form = _form()
        self._form = form
        form.addRow("Capture:", _field(self.kind_combo))
        self._device_field = _field(_row(self.device_combo, 1, self.refresh_button))
        form.addRow("Device:", self._device_field)
        self._app_field = _field(_row(self.app_combo, 1, self.app_refresh_button),
                                 "Type a program's .exe name, or pick one that's running.")
        form.addRow("App:", self._app_field)
        self._file_field = _field(_row(self.file_edit, 1, self.browse_button))
        form.addRow("File:", self._file_field)
        form.addRow("Label:", _field(self.name_edit))
        self.editor.setLayout(form)

        tip = Hint("Tip: pick the device your Discord plays through, or 'Single app' to caption "
                   "Discord without game sounds.")

        hears = QGroupBox("What VoiceRhino hears")
        hears_column = QVBoxLayout(hears)
        hears_column.addLayout(self._level_grid)
        hears_column.addWidget(self.idle_label)
        hears_column.addWidget(self.quiet_label)
        hears_column.addWidget(self.gain_check)
        hears_column.addWidget(Hint("If voices stay very quiet even with the boost, turn the volume up: "
                                    "Discord's own output volume, or the app's slider in Windows' Volume Mixer."))

        layout = QVBoxLayout(self)
        layout.addWidget(sources)
        layout.addWidget(self.editor)
        layout.addWidget(tip)
        layout.addWidget(self.error_box)
        layout.addWidget(hears)
        layout.addStretch(1)

        self._list_devices()

    # -- data --

    def _list_devices(self) -> None:
        try:
            self._devices = list(audio_devices.list_devices())
            self.error_box.setVisible(False)
        except Exception as exc:
            log.exception("couldn't list audio devices")
            self._devices = []
            self.error_label.setText(
                f"Couldn't list audio devices: {exc or type(exc).__name__}\n"
                "The default output, a single app or a WAV file still work. Press Refresh to try again."
            )
            self.error_box.setVisible(True)

    def refresh(self) -> None:
        """List devices and apps again, keeping the current choices."""
        self._list_devices()
        self._apps = None
        self._show_current()

    # -- levels --

    def set_level_provider(self, provider: LevelProvider | None) -> None:
        """`provider()` lists the running sources' levels; it's polled while this tab shows."""
        self._levels = provider
        self.refresh_levels()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._level_timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._level_timer.stop()

    def refresh_levels(self) -> None:
        infos: list[SourceLevel] = []
        if self._levels is not None:
            try:
                infos = [info for info in self._levels() if info is not None]
            except Exception:
                log.exception("couldn't read audio levels")
        if [info.name for info in infos] != self._level_names:
            self._rebuild_level_rows([info.name for info in infos])
        self.idle_label.setVisible(not infos)
        quiet = []
        for info, (meter, value) in zip(infos, self._level_rows):
            meter.set_db(info.level_db)
            text = "silent" if info.level_db <= SILENCE_DB else f"{info.level_db:.0f} dB".replace("-", "−")
            if info.gain_db >= 0.5:
                text += f" · boost +{info.gain_db:.0f} dB"
            value.setText(text)
            if info.speech_db is not None and info.speech_db < QUIET_DB:
                quiet.append(info.name)
        self.quiet_label.setVisible(bool(quiet))
        if quiet:
            self.quiet_label.setText(f"⚠ {' and '.join(quiet)}: voices are very quiet even with the "
                                     "boost, so captions can miss words. Turn the volume up.")

    def _rebuild_level_rows(self, names: list[str]) -> None:
        while self._level_grid.count():
            widget = self._level_grid.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)  # off screen now; deleteLater alone waits for the event loop
                widget.deleteLater()
        self._level_names, self._level_rows = list(names), []
        for row, name in enumerate(names):
            meter, value = LevelMeter(), QLabel()
            value.setMinimumWidth(value.fontMetrics().horizontalAdvance("−00 dB · boost +00 dB") + 6)
            self._level_grid.addWidget(QLabel(name), row, 0)
            self._level_grid.addWidget(meter, row, 1)
            self._level_grid.addWidget(value, row, 2)
            self._level_rows.append((meter, value))

    def _current(self) -> SourceConfig | None:
        row = self.source_list.currentRow()
        return self._cfg.sources[row] if 0 <= row < len(self._cfg.sources) else None

    def _default_device(self, kind: str) -> str | None:
        if kind == "output":
            return DEFAULT_OUTPUT_KEY
        inputs = [d for d in self._devices if d.kind == "input"]
        preferred = [d for d in inputs if d.is_default] or inputs
        return preferred[0].key if preferred else None

    # -- filling widgets --

    def _load(self) -> None:
        self._rebuild(self.source_list.currentRow())
        self.gain_check.setChecked(bool(self._cfg.pipeline.auto_gain))

    def _rebuild(self, row: int) -> None:
        with self._quiet():
            self.source_list.clear()
            for src in self._cfg.sources:
                self.source_list.addItem(_check_item(_source_title(src), src.enabled,
                                                     "Tick to caption this source"))
            if self._cfg.sources:
                self.source_list.setCurrentRow(min(max(row, 0), len(self._cfg.sources) - 1))
        self.remove_button.setEnabled(len(self._cfg.sources) > 1)
        self._show_current()

    def _show_current(self) -> None:
        src = self._current()
        self.editor.setEnabled(src is not None)
        kind = _ui_kind(src) if src else "output"
        with self._quiet():
            self.kind_combo.setCurrentIndex(self.kind_combo.findData(kind))
            if kind in ("output", "input"):
                self._fill_devices(kind, src.device if src else DEFAULT_OUTPUT_KEY)
                if src is not None and src.device == _NO_INPUT and self.device_combo.currentData():
                    src.device = self.device_combo.currentData()  # a microphone turned up
            if kind == "app":
                self._fill_apps(src.app if src else DEFAULT_APP)
            self.file_edit.setText(src.file if src else "")
            self.name_edit.setText(src.name if src else "")
        self._form.setRowVisible(self._device_field, kind in ("output", "input"))
        self._form.setRowVisible(self._app_field, kind == "app")
        self._form.setRowVisible(self._file_field, kind == "file")
        self._update_labels()

    def _fill_devices(self, kind: str, key: str) -> None:
        combo = self.device_combo
        combo.clear()
        wanted = "loopback" if kind == "output" else "input"
        if kind == "output":
            combo.addItem("Default output (all PC audio)", DEFAULT_OUTPUT_KEY)
            combo.setItemData(0, "Whatever Windows plays on its default output device, "
                                 "following it when the default changes", Qt.ItemDataRole.ToolTipRole)
        for device in self._devices:
            if device.kind == wanted:
                combo.addItem(device.name + (" - Windows default" if device.is_default else ""), device.key)
        index = combo.findData(key)
        if index < 0 and key.startswith(wanted + ":") and key != _NO_INPUT:
            combo.addItem(f"{key.split(':', 1)[1]} (not connected)", key)
            index = combo.count() - 1
        if combo.count() == 0:
            combo.addItem("No microphones found - press Refresh", None)
        combo.setCurrentIndex(max(index, 0))

    def _fill_apps(self, text: str) -> None:
        if self._apps is None:
            self._apps = list_app_choices()
        choices = list(self._apps)
        if not any(exe.lower() == DEFAULT_APP.lower() for exe, _ in choices):
            choices.append((DEFAULT_APP, False))
        choices.sort(key=lambda c: c[0].lower() != DEFAULT_APP.lower())  # Discord first
        running = {exe.lower() for exe, _ in self._apps}
        combo = self.app_combo
        combo.clear()
        for exe, active in choices:
            combo.addItem(exe)
            index = combo.count() - 1
            combo.setItemData(index, active, _ACTIVE_ROLE)
            tooltip = ("Playing audio now" if active else
                       "Running" if exe.lower() in running else "Not running right now")
            combo.setItemData(index, tooltip, Qt.ItemDataRole.ToolTipRole)
        combo.setEditText(text)

    def _update_labels(self) -> None:
        """Refresh the list title and the label placeholder after an edit."""
        src = self._current()
        if src is None:
            self.name_edit.setPlaceholderText("")
            return
        self.name_edit.setPlaceholderText(dataclasses.replace(src, name="").describe())
        item = self.source_list.currentItem()
        if item is not None:
            with self._quiet():
                item.setText(_source_title(src))

    # -- user edits --

    def _on_row_changed(self, _row: int) -> None:
        if not self._loading:
            self._show_current()

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        row = self.source_list.row(item)
        if self._loading or not 0 <= row < len(self._cfg.sources):
            return
        self._cfg.sources[row].enabled = item.checkState() == Qt.CheckState.Checked

    def _on_add(self) -> None:
        self._cfg.sources.append(SourceConfig())
        self._rebuild(len(self._cfg.sources) - 1)

    def _on_remove(self) -> None:
        row = self.source_list.currentRow()
        if len(self._cfg.sources) > 1 and 0 <= row < len(self._cfg.sources):
            del self._cfg.sources[row]
            self._rebuild(row)

    def _on_kind_changed(self, _index: int) -> None:
        src = self._current()
        if self._loading or src is None:
            return
        kind = self.kind_combo.currentData()
        if kind in ("output", "input"):
            src.kind = "device"
            prefix = "loopback:" if kind == "output" else "input:"
            if not src.device.startswith(prefix):
                src.device = self._default_device(kind) or _NO_INPUT
        elif kind == "app":
            src.kind = "app"
            src.app = src.app.strip() or DEFAULT_APP
        else:
            src.kind = "file"
        self._show_current()

    def _on_device_changed(self, _index: int) -> None:
        src = self._current()
        key = self.device_combo.currentData()
        if self._loading or src is None or not key:
            return
        src.device = key
        self._update_labels()

    def _on_app_changed(self, text: str) -> None:
        src = self._current()
        if self._loading or src is None:
            return
        src.app = text.strip()
        self._update_labels()

    def _on_file_changed(self, text: str) -> None:
        src = self._current()
        if self._loading or src is None:
            return
        src.file = text.strip()
        self._update_labels()

    def _on_browse(self) -> None:
        start = self.file_edit.text().strip()
        path, _ = QFileDialog.getOpenFileName(self, "Choose a WAV file", start,
                                              "WAV audio (*.wav);;All files (*)")
        if path:
            self.file_edit.setText(str(Path(path)))

    def _on_name_changed(self, text: str) -> None:
        src = self._current()
        if self._loading or src is None:
            return
        src.name = text.strip()
        self._update_labels()

    def _on_gain_toggled(self, checked: bool) -> None:
        if not self._loading:
            self._cfg.pipeline.auto_gain = bool(checked)


class RecognitionTab(_Tab):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.model_combo = _combo()
        self.model_combo.setToolTip("The speech recognition model. It downloads automatically.")
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        self.languages_label = QLabel()
        self.size_label = QLabel()
        self.status_label = QLabel()
        self.notes_label = Hint()

        self.device_combo = _combo()
        for label, value in ASR_DEVICES:
            self.device_combo.addItem(label, value)
        self.device_combo.setToolTip("Where the model runs")
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 16)
        self.threads_spin.setToolTip("CPU threads the recognizer may use. 2 is plenty for Parakeet; "
                                     "more decodes faster but takes more CPU from the game.")
        self.threads_spin.valueChanged.connect(self._on_threads_changed)
        self.language_combo = _combo(editable=True)
        self.language_combo.setToolTip("The language being spoken. Auto-detect needs a multilingual "
                                       "model; English-only models ignore this. You can type a code, e.g. 'it'.")
        self.language_combo.editTextChanged.connect(self._on_language_changed)
        self.task_combo = _combo()
        for label, value in TASKS:
            self.task_combo.addItem(label, value)
        self.task_combo.currentIndexChanged.connect(self._on_task_changed)
        self.live_combo = _combo()
        self.live_combo.currentIndexChanged.connect(self._on_live_changed)

        # Details sit in the Model field's own column: as a separate label-less form row, the
        # wrapped notes were given their 2-line size hint instead of their real height.
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)
        for row, (title, label) in enumerate([("Languages:", self.languages_label),
                                              ("Download:", self.size_label),
                                              ("Status:", self.status_label)]):
            grid.addWidget(QLabel(title), row, 0)
            grid.addWidget(label, row, 1)
        grid.setColumnStretch(1, 1)
        model_field = QWidget()
        column = QVBoxLayout(model_field)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)
        column.addWidget(self.model_combo)
        column.addSpacing(4)
        column.addLayout(grid)
        column.addWidget(self.notes_label)

        form = _form()
        form.addRow("Model:", model_field)
        form.addRow("Live text:", _field(
            self.live_combo,
            "Re-decode: the model above rereads the line every 0.4 s. It's the lightest (no second model) "
            "and shows the first word sooner (about 0.3 s vs 0.6 s), but words can change as it hears "
            "more, and game noise can flash a stray word. Word-by-word: a second model writes each word "
            "once and never changes it, for about 0.1 more CPU core and 0.7 GB more memory. All of these "
            "run on the CPU, not the GPU. Word-by-word is English only."))
        form.addRow("Run on:", _field(
            _row(self.device_combo, stretch=True),
            "CPU means zero GPU impact while gaming. GPU (CUDA) only speeds up the Whisper "
            "(faster-whisper) models and needs the GPU extra."))
        form.addRow("CPU threads:", _field(_row(_spin(self.threads_spin), stretch=True)))
        form.addRow("Language:", _field(_row(self.language_combo, stretch=True)))
        form.addRow("Task:", _field(
            _row(self.task_combo, stretch=True),
            "Translate turns speech in any language into English text. Whisper models only."))

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addStretch(1)

    def _spec(self) -> ModelSpec | None:
        model_id = self.model_combo.currentData()
        return next((spec for spec in ASR_MODELS if spec.id == model_id), None)

    def _load(self) -> None:
        asr = self._cfg.asr
        self.model_combo.clear()
        for spec in ASR_MODELS:
            self.model_combo.addItem(spec.name, spec.id)
        _set_combo_data(self.model_combo, asr.model, f"{asr.model} (not in this version's list)")
        _set_combo_data(self.device_combo, asr.device, str(asr.device))
        self.threads_spin.setValue(_int(asr.threads, 2))
        self.language_combo.clear()
        for label, code in LANGUAGES:
            self.language_combo.addItem(label, code)
        index = self.language_combo.findData(asr.language)
        if index >= 0:
            self.language_combo.setCurrentIndex(index)
        else:
            self.language_combo.setEditText(str(asr.language))
        _set_combo_data(self.task_combo, asr.task, str(asr.task))
        self._fill_live_combo()
        self._update_details()

    def _fill_live_combo(self) -> None:
        asr = self._cfg.asr
        self.live_combo.clear()
        self.live_combo.addItem(REDECODE_LABEL, "")
        for spec in STREAMING_MODELS:
            have = "" if download.is_downloaded(spec.source) else f" · +{spec.size_mb} MB download"
            self.live_combo.addItem(f"{spec.name} ({spec.tagline}){have}", spec.id)
        _set_combo_data(self.live_combo, asr.live_model, f"{asr.live_model} (not in this version's list)")
        view = self.live_combo.view()  # let the list be wider than the field, so no option is cut off
        view.setMinimumWidth(view.sizeHintForColumn(0) + 2 * view.frameWidth() + 16)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        with self._quiet():  # models may have been deleted on the General tab: redo "Downloaded" labels
            self._fill_live_combo()
        self._update_details()

    def _update_details(self) -> None:
        spec = self._spec()
        if spec is None:
            self.languages_label.setText("-")
            self.size_label.setText("-")
            self.status_label.setText("Unknown model id; pick one from the list")
            self.notes_label.setText("")
        else:
            self.languages_label.setText(spec.languages)
            self.size_label.setText(f"about {spec.size_mb:,} MB")
            self.status_label.setText(model_status(spec))
            self.notes_label.setText(spec.notes)
        self.notes_label.setVisible(bool(self.notes_label.text()))
        live_ok = spec is not None and supports_live_model(spec)
        self.live_combo.setEnabled(live_ok)
        self.live_combo.setToolTip("" if live_ok else
                                   "Word-by-word live text is English-only; this model re-decodes instead.")
        can_translate = spec is not None and spec.can_translate
        self.task_combo.setEnabled(can_translate)
        self.task_combo.setToolTip("Transcribe keeps the spoken language; Translate outputs English."
                                   if can_translate else
                                   "Only Whisper Medium / Large v3 can translate (Turbo and Distil can't).")

    def _on_model_changed(self, _index: int) -> None:
        if self._loading:
            return
        asr = self._cfg.asr
        asr.model = self.model_combo.currentData()
        spec = self._spec()
        if spec is not None:
            # Whisper models are meant for the GPU, sherpa models for the CPU.
            asr.device = "cuda" if spec.gpu_recommended else "cpu"
            if not spec.can_translate:
                asr.task = "transcribe"
            # English (the default) on a multilingual model would force other languages into
            # English words; a language the user picked themselves is kept.
            english_only = spec.languages == "English"
            if english_only or asr.language == "en":
                asr.language = "en" if english_only else ""
            with self._quiet():
                _set_combo_data(self.device_combo, asr.device, asr.device)
                _set_combo_data(self.task_combo, asr.task, asr.task)
                _set_combo_data(self.language_combo, asr.language)  # both are listed items
        self._update_details()

    def _on_device_changed(self, _index: int) -> None:
        if not self._loading and self.device_combo.currentData():
            self._cfg.asr.device = self.device_combo.currentData()

    def _on_threads_changed(self, value: int) -> None:
        if not self._loading:
            self._cfg.asr.threads = int(value)

    def _on_language_changed(self, text: str) -> None:
        if not self._loading:
            self._cfg.asr.language = parse_language(text)

    def _on_task_changed(self, _index: int) -> None:
        if not self._loading and self.task_combo.currentData():
            self._cfg.asr.task = self.task_combo.currentData()

    def _on_live_changed(self, _index: int) -> None:
        if not self._loading and self.live_combo.currentData() is not None:
            self._cfg.asr.live_model = self.live_combo.currentData()


class TimingTab(_Tab):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.spins: dict[str, QDoubleSpinBox] = {}
        form = _form()
        for name, label, low, high, step, decimals, suffix, special, hint in TIMING_FIELDS:
            spin = QDoubleSpinBox()
            spin.setDecimals(decimals)
            spin.setRange(low, high)
            spin.setSingleStep(step)
            spin.setSuffix(suffix)
            spin.setSpecialValueText(special)
            spin.setToolTip(hint)
            spin.valueChanged.connect(partial(self._on_changed, name))
            form.addRow(f"{label}:", _field(_row(_spin(spin), stretch=True), hint))
            self.spins[name] = spin
        self.restore_button = QPushButton("Restore defaults")
        self.restore_button.setToolTip("Put every timing setting back to VoiceRhino's defaults")
        self.restore_button.clicked.connect(self.restore_defaults)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addLayout(_row(self.restore_button, stretch=True))

    def _load(self) -> None:
        defaults = PipelineSettings()
        for name, spin in self.spins.items():
            spin.setValue(_float(getattr(self._cfg.pipeline, name), getattr(defaults, name)))

    def _on_changed(self, name: str, value: float) -> None:
        if not self._loading:
            setattr(self._cfg.pipeline, name, round(float(value), 3))

    def restore_defaults(self) -> None:
        defaults = PipelineSettings()
        for name in self.spins:  # only this tab's settings (the boost lives on the Audio tab)
            setattr(self._cfg.pipeline, name, getattr(defaults, name))
        self.load(self._cfg)


class WindowsTab(_Tab):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.view_list = QListWidget()
        self.view_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view_list.currentRowChanged.connect(self._on_row_changed)
        self.view_list.itemChanged.connect(self._on_item_changed)
        self.add_caption_button = QPushButton("Add caption window")
        self.add_caption_button.setToolTip("Another big caption overlay, e.g. for a second monitor")
        self.add_caption_button.clicked.connect(lambda: self._add(ViewConfig()))
        self.add_history_button = QPushButton("Add history window")
        self.add_history_button.setToolTip("A scrollback window showing the last minute or so")
        self.add_history_button.clicked.connect(lambda: self._add(ViewConfig.default_history()))
        self.remove_button = QPushButton("Remove")
        self.remove_button.setToolTip("Remove the selected window (one always stays; untick it to hide it)")
        self.remove_button.clicked.connect(self._on_remove)

        self.screen_combo = _combo()
        self.screen_combo.setToolTip("Move this window to a screen: captions go bottom-center, "
                                     "history to the right edge")
        self.screen_combo.activated.connect(self._on_screen_picked)
        self.placement_label = Hint()
        self.font_combo = QFontComboBox()
        self.font_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.font_combo.setMinimumContentsLength(10)
        self.font_combo.currentFontChanged.connect(self._on_font_changed)
        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(8, 120)
        self.font_size_spin.setToolTip("Text size")
        self.font_size_spin.valueChanged.connect(partial(self._set, "font_size"))
        self.bold_check = QCheckBox("Bold")
        self.bold_check.toggled.connect(partial(self._set, "bold"))
        self.text_color_button = ColorButton("Text color")
        self.text_color_button.colorChanged.connect(partial(self._set, "text_color"))
        self.partial_color_button = ColorButton("Live text color")
        self.partial_color_button.setToolTip("Color of words still being recognized; they may change "
                                             "until the line is finished")
        self.partial_color_button.colorChanged.connect(partial(self._set, "partial_color"))
        self.background_button = ColorButton("Background color")
        self.background_button.colorChanged.connect(partial(self._set, "background"))
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setPageStep(10)
        self.opacity_slider.setToolTip("0% = no background, only the text (and its outline)")
        self.opacity_slider.valueChanged.connect(self._on_opacity_changed)
        self.opacity_label = QLabel()
        self.opacity_label.setMinimumWidth(self.opacity_label.fontMetrics().horizontalAdvance("100%") + 6)
        self.outline_spin = QSpinBox()
        self.outline_spin.setRange(0, 8)
        self.outline_spin.setSuffix(" px")
        self.outline_spin.setSpecialValueText("None")
        self.outline_spin.setToolTip("Dark outline around letters; keeps text readable over bright game scenes")
        self.outline_spin.valueChanged.connect(partial(self._set, "outline"))
        self.lines_spin = QSpinBox()
        self.lines_spin.setRange(1, 6)
        self.lines_spin.setToolTip("Lines visible at once; older lines roll up and away")
        self.lines_spin.valueChanged.connect(partial(self._set, "lines"))
        self.clear_after_spin = QDoubleSpinBox()
        self.clear_after_spin.setRange(0, 60)
        self.clear_after_spin.setDecimals(0)
        self.clear_after_spin.setSuffix(" s")
        self.clear_after_spin.setSpecialValueText("Never")
        self.clear_after_spin.setToolTip("Hide the captions after this many quiet seconds")
        self.clear_after_spin.valueChanged.connect(partial(self._set, "clear_after"))
        self.history_seconds_spin = QSpinBox()
        self.history_seconds_spin.setRange(10, 900)
        self.history_seconds_spin.setSingleStep(10)
        self.history_seconds_spin.setSuffix(" s")
        self.history_seconds_spin.setToolTip("How far back the history window goes")
        self.history_seconds_spin.valueChanged.connect(partial(self._set, "history_seconds"))
        self.timestamps_check = QCheckBox("Show timestamps")
        self.timestamps_check.toggled.connect(partial(self._set, "show_timestamps"))
        self.speakers_check = QCheckBox("Show speaker names")
        self.speakers_check.setToolTip("Prefix lines with who is talking (see the Speakers tab)")
        self.speakers_check.toggled.connect(partial(self._set, "show_speakers"))
        self.click_through_check = QCheckBox("Click-through when locked")
        self.click_through_check.setToolTip("When the windows are locked, clicks go through this window "
                                            "to the game underneath")
        self.click_through_check.toggled.connect(partial(self._set, "click_through"))
        # One tick box per audio source, rebuilt with the source list (only shown when there are 2+).
        self.sources_box = QWidget()
        self._sources_layout = QVBoxLayout(self.sources_box)
        self._sources_layout.setContentsMargins(0, 0, 0, 0)
        self._sources_layout.setSpacing(4)
        self.source_checks: list[QCheckBox] = []

        form = _form()
        self._form = form
        form.addRow("Move to screen:", _field(self.screen_combo, self.placement_label))
        self._sources_field = _field(self.sources_box, "Untick a source to leave it out of this window, e.g. "
                                     "your own microphone in the big captions but not in the history.")
        form.addRow("Captions from:", self._sources_field)
        form.addRow("Font:", _field(_row(self.font_combo, 1, _spin(self.font_size_spin), self.bold_check)))
        form.addRow("Text color:", _field(_row(self.text_color_button, stretch=True)))
        form.addRow("Live text color:", _field(_row(self.partial_color_button, stretch=True)))
        form.addRow("Text outline:", _field(_row(_spin(self.outline_spin), stretch=True)))
        form.addRow("Background:", _field(_row(self.background_button, stretch=True)))
        form.addRow("Background opacity:", _field(_row(self.opacity_slider, 1, self.opacity_label)))
        self._caption_fields = [
            _field(_row(_spin(self.lines_spin), stretch=True)),
            _field(_row(_spin(self.clear_after_spin), stretch=True)),
        ]
        form.addRow("Lines shown:", self._caption_fields[0])
        form.addRow("Clear after:", self._caption_fields[1])
        self._history_fields = [
            _field(_row(_spin(self.history_seconds_spin), stretch=True),
                   "How far back the history window goes; older lines disappear. It's only shown "
                   "on screen and never saved to disk.",
                   "Ask everyone you're talking with before you use the history window."),
            _field(self.timestamps_check),
        ]
        form.addRow("History length:", self._history_fields[0])
        form.addRow("", self._history_fields[1])
        form.addRow("", self.speakers_check)
        form.addRow("", self.click_through_check)

        self.editor = QWidget()
        self.editor.setLayout(form)
        self._scroll = scroll = QScrollArea()
        scroll.setWidget(self.editor)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.editor_group = QGroupBox("Selected window")
        editor_layout = QVBoxLayout(self.editor_group)
        editor_layout.setContentsMargins(4, 4, 4, 4)
        editor_layout.addWidget(scroll)

        left_box = QGroupBox("Windows")
        left = QVBoxLayout(left_box)
        left.addWidget(self.view_list, 1)
        left.addWidget(self.add_caption_button)
        left.addWidget(self.add_history_button)
        left.addWidget(self.remove_button)
        left.addWidget(Hint("Tick a window to show it. Drag windows into place while unlocked."))
        left_box.setFixedWidth(max(200, self.add_history_button.sizeHint().width() + 40))

        layout = QHBoxLayout(self)
        layout.addWidget(left_box)
        layout.addWidget(self.editor_group, 1)

    def _current(self) -> ViewConfig | None:
        row = self.view_list.currentRow()
        return self._cfg.views[row] if 0 <= row < len(self._cfg.views) else None

    def _load(self) -> None:
        self._rebuild(self.view_list.currentRow())

    def _rebuild(self, row: int) -> None:
        with self._quiet():
            self.view_list.clear()
            for view, title in zip(self._cfg.views, _view_titles(self._cfg.views)):
                self.view_list.addItem(_check_item(title, view.visible, "Tick to show this window"))
            if self._cfg.views:
                self.view_list.setCurrentRow(min(max(row, 0), len(self._cfg.views) - 1))
        self.remove_button.setEnabled(len(self._cfg.views) > 1)
        self._show_current()

    def _show_current(self) -> None:
        view = self._current()
        self.editor_group.setEnabled(view is not None)
        if view is None:
            return
        d = ViewConfig  # class-level defaults, for values a hand-edited config got wrong
        with self._quiet():
            self.font_combo.setCurrentFont(QFont(str(view.font_family)))
            self.font_size_spin.setValue(_int(view.font_size, d.font_size))
            self.bold_check.setChecked(bool(view.bold))
            self.text_color_button.set_color(str(view.text_color))
            self.partial_color_button.set_color(str(view.partial_color))
            self.background_button.set_color(str(view.background))
            self.opacity_slider.setValue(round(_float(view.background_opacity, d.background_opacity) * 100))
            self.outline_spin.setValue(_int(view.outline, d.outline))
            self.lines_spin.setValue(_int(view.lines, d.lines))
            self.clear_after_spin.setValue(_float(view.clear_after, d.clear_after))
            self.history_seconds_spin.setValue(_int(view.history_seconds, d.history_seconds))
            self.timestamps_check.setChecked(bool(view.show_timestamps))
            self.speakers_check.setChecked(bool(view.show_speakers))
            self.click_through_check.setChecked(bool(view.click_through))
        self._update_opacity_label()
        history = view.kind == "history"
        for field in self._caption_fields:
            self._form.setRowVisible(field, not history)
        for field in self._history_fields:
            self._form.setRowVisible(field, history)
        self._fill_sources()
        self.refresh_placement()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fill_sources()  # sources may have been added or renamed on the Audio tab

    def _fill_sources(self) -> None:
        view = self._current()
        for box in self.source_checks:
            box.setParent(None)
            box.deleteLater()
        self.source_checks = []
        for src in self._cfg.sources:
            box = QCheckBox(_source_title(src) + ("" if src.enabled else " (off)"))
            box.setChecked(view is not None and src.id not in view.hidden_sources)
            box.toggled.connect(partial(self._on_source_toggled, src.id))
            self._sources_layout.addWidget(box)
            box.show()  # now, not on a later pass, so the size below already counts it
            self.source_checks.append(box)
        self._form.setRowVisible(self._sources_field, len(self._cfg.sources) > 1)
        # Rows came and went: make the scroll area re-fit the editor now (it doesn't by itself,
        # and squeezes the rows on top of each other once they're taller than the visible area).
        QApplication.sendEvent(self._scroll, QEvent(QEvent.Type.LayoutRequest))

    def refresh_placement(self) -> None:
        """Show which screen the selected window is on and where."""
        view = self._current()
        screens = QGuiApplication.screens()
        with self._quiet():
            self.screen_combo.clear()
            self.screen_combo.addItem("", None)
            for i, screen in enumerate(screens):
                self.screen_combo.addItem(_screen_text(i, screen), i)
            if view is None:
                return
            x, y, w, h = (_int(v, -1) for v in (view.x, view.y, view.w, view.h))
            if (x, y) == (-1, -1):
                self.screen_combo.setItemText(0, "Automatic - pick a screen to move it")
                self.placement_label.setText("Placed automatically the first time it shows.")
                self.screen_combo.setCurrentIndex(0)
                return
            center = QPoint(x + w // 2, y + h // 2)
            screen = QGuiApplication.screenAt(center)
            index = screens.index(screen) + 1 if screen in screens else 0
            self.screen_combo.setItemText(0, "Not on any screen - pick one to move it" if index == 0
                                          else "Pick a screen...")
            self.screen_combo.setCurrentIndex(index)
            self.placement_label.setText(f"{w} x {h} at {x}, {y}")

    # -- user edits --

    def _on_row_changed(self, _row: int) -> None:
        if not self._loading:
            self._show_current()

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        row = self.view_list.row(item)
        if self._loading or not 0 <= row < len(self._cfg.views):
            return
        self._cfg.views[row].visible = item.checkState() == Qt.CheckState.Checked

    def _add(self, view: ViewConfig) -> None:
        self._cfg.views.append(view)
        self._rebuild(len(self._cfg.views) - 1)

    def _on_remove(self) -> None:
        row = self.view_list.currentRow()
        if len(self._cfg.views) > 1 and 0 <= row < len(self._cfg.views):
            del self._cfg.views[row]
            self._rebuild(row)

    def _set(self, name: str, value) -> None:
        """Store a widget's value (spinbox int/float, checkbox bool, color str) in the selected view."""
        view = self._current()
        if not self._loading and view is not None:
            setattr(view, name, value)

    def _on_source_toggled(self, source_id: str, checked: bool) -> None:
        view = self._current()
        if self._loading or view is None:
            return
        hidden = [i for i in view.hidden_sources if i != source_id]
        view.hidden_sources = hidden if checked else hidden + [source_id]

    def _on_font_changed(self, font: QFont) -> None:
        self._set("font_family", font.family())

    def _on_opacity_changed(self, value: int) -> None:
        self._update_opacity_label()
        self._set("background_opacity", round(value / 100, 2))

    def _update_opacity_label(self) -> None:
        self.opacity_label.setText(f"{self.opacity_slider.value()}%")

    def _on_screen_picked(self, index: int) -> None:
        view = self._current()
        screen_index = self.screen_combo.itemData(index)
        screens = QGuiApplication.screens()
        if view is None or screen_index is None or not 0 <= screen_index < len(screens):
            return
        place_view(view, screens[screen_index].availableGeometry())
        self.refresh_placement()


class SpeakersTab(_Tab):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.voice_check = QCheckBox("Color speakers by voice")
        self.voice_check.toggled.connect(self._on_voice_toggled)
        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setDecimals(2)
        self.threshold_spin.setRange(0.0, 1.0)
        self.threshold_spin.setSingleStep(0.05)
        self.threshold_spin.setSpecialValueText("Model default")
        self.threshold_spin.setToolTip("Higher splits voices into more speakers; lower merges similar voices")
        self.threshold_spin.valueChanged.connect(self._on_threshold_changed)

        self.discord_check = QCheckBox("Use Discord names")
        self.discord_check.toggled.connect(self._on_discord_toggled)
        self.client_id_edit = QLineEdit()
        self.client_id_edit.setPlaceholderText("From your Discord developer application")
        self.client_id_edit.textChanged.connect(partial(self._set_text, "discord_client_id"))
        self.secret_edit = QLineEdit()
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_edit.textChanged.connect(partial(self._set_text, "discord_client_secret"))
        self.show_secret_button = QPushButton("Show")
        self.show_secret_button.setCheckable(True)
        self.show_secret_button.setToolTip("Show or hide the secret")
        self.show_secret_button.toggled.connect(self._on_show_secret)
        self.help_button = QPushButton("How to set this up")
        self.help_button.setToolTip(str(discord_setup_doc()))
        self.help_button.clicked.connect(self.open_setup_doc)

        voice = QGroupBox("Voices")
        voice_form = _form()
        voice_form.addRow(_field(self.voice_check,
                                 "Gives each voice its own color, on any audio source. It can't know "
                                 "names, only that voices differ."))
        voice_form.addRow("Similarity threshold:", _field(
            _row(_spin(self.threshold_spin), stretch=True),
            "How alike two lines must sound to count as the same person. Leave at Model default "
            "unless one person keeps getting two colors (lower it) or two people share one (raise it)."))
        voice.setLayout(voice_form)

        discord = QGroupBox("Discord names")
        discord_form = _form()
        discord_form.addRow(_field(self.discord_check,
                                   "Shows who is talking in your Discord voice call, using Discord's "
                                   "own speaking indicator. Needs a free Discord developer app (one-time setup)."))
        discord_form.addRow("Client ID:", _field(self.client_id_edit))
        discord_form.addRow("Client secret:", _field(_row(self.secret_edit, 1, self.show_secret_button),
                                                     "Stays on this PC, in VoiceRhino's config file."))
        discord_form.addRow("", _field(_row(self.help_button, stretch=True)))
        discord.setLayout(discord_form)

        layout = QVBoxLayout(self)
        layout.addWidget(voice)
        layout.addWidget(discord)
        layout.addStretch(1)

    def _load(self) -> None:
        s = self._cfg.speakers
        self.voice_check.setChecked(bool(s.voice_enabled))
        self.threshold_spin.setValue(_float(s.voice_threshold, 0.0))
        self.discord_check.setChecked(bool(s.discord_enabled))
        self.client_id_edit.setText(str(s.discord_client_id or ""))  # JSON may hold it as a number
        self.secret_edit.setText(str(s.discord_client_secret or ""))
        self.show_secret_button.setChecked(False)  # hidden again whenever the dialog reloads
        self._update_enabled()

    def _update_enabled(self) -> None:
        self.threshold_spin.setEnabled(self.voice_check.isChecked())
        for widget in (self.client_id_edit, self.secret_edit, self.show_secret_button):
            widget.setEnabled(self.discord_check.isChecked())

    def _on_voice_toggled(self, checked: bool) -> None:
        self._update_enabled()
        if not self._loading:
            self._cfg.speakers.voice_enabled = checked

    def _on_threshold_changed(self, value: float) -> None:
        if not self._loading:
            self._cfg.speakers.voice_threshold = round(float(value), 3)

    def _on_discord_toggled(self, checked: bool) -> None:
        self._update_enabled()
        if not self._loading:
            self._cfg.speakers.discord_enabled = checked

    def _set_text(self, name: str, text: str) -> None:
        if not self._loading:
            setattr(self._cfg.speakers, name, text.strip())

    def _on_show_secret(self, shown: bool) -> None:
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)
        self.show_secret_button.setText("Hide" if shown else "Show")

    def open_setup_doc(self) -> None:
        doc = discord_setup_doc()
        if doc.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(doc)))
        else:
            QMessageBox.information(
                self, "Discord setup",
                "The setup guide isn't there yet:\n"
                f"{doc}\n\n"
                "In short: create an application at discord.com/developers/applications, "
                "then paste its Client ID and Client Secret here.")


class TranslationTab(_Tab):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.model_combo = _combo()
        self.model_combo.setToolTip("Translates other languages into English on this PC. "
                                    "The model downloads the first time you pick it.")
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        self.size_label = QLabel()
        self.status_label = QLabel()
        self.license_label = QLabel()
        self.notes_label = Hint()
        self.recognition_warning = QLabel()
        self.recognition_warning.setWordWrap(True)
        self.foreign_check = QCheckBox("Skip lines that are already in English")
        self.foreign_check.toggled.connect(self._on_foreign_toggled)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)
        for row, (title, label) in enumerate([("Download:", self.size_label), ("Status:", self.status_label),
                                              ("License:", self.license_label)]):
            grid.addWidget(QLabel(title), row, 0)
            grid.addWidget(label, row, 1)
        grid.setColumnStretch(1, 1)
        self.details = QWidget()
        details = QVBoxLayout(self.details)
        details.setContentsMargins(0, 4, 0, 0)
        details.setSpacing(3)
        details.addLayout(grid)
        details.addWidget(self.notes_label)
        model_field = QWidget()
        column = QVBoxLayout(model_field)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)
        column.addWidget(self.model_combo)
        column.addWidget(self.details)
        column.addWidget(self.recognition_warning)

        form = _form()
        form.addRow("Translate:", model_field)
        form.addRow("", _field(self.foreign_check,
                               "Each line's language is guessed from its letters and common words. Untick to "
                               "translate every line, e.g. if lines in that language are being skipped."))
        privacy = Hint("Translation runs on this PC. The model downloads once (from Hugging Face; Hy-MT2 "
                       "also gets llama.cpp from GitHub), then caption text never leaves this PC. No "
                       "accounts or keys. Finished lines show their translation under (history) or "
                       "instead of (captions) the original.")
        tip = Hint("With a GPU, Whisper Medium or Large v3 can also translate as it listens: pick one in "
                   "Recognition and set Task to 'Translate to English'. Whisper Turbo can't translate.")
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(privacy)
        layout.addWidget(tip)
        layout.addStretch(1)

    def _spec(self) -> TranslationModel | None:
        model_id = self.model_combo.currentData()
        return next((m for m in TRANSLATION_MODELS if m.id == model_id), None)

    def _load(self) -> None:
        t = self._cfg.translation
        self._fill_models()
        self.foreign_check.setChecked(bool(t.only_foreign))
        self._update_details()

    def _fill_models(self) -> None:
        self.model_combo.clear()
        self.model_combo.addItem("Off", "")
        for spec in TRANSLATION_MODELS:
            have = "" if is_model_downloaded(spec) else f" · {spec.size_mb:,} MB download"
            self.model_combo.addItem(spec.name + have, spec.id)
        # A model this version doesn't know (or an old setting) shows as Off; Apply saves that.
        index = self.model_combo.findData(self._cfg.translation.model)
        self.model_combo.setCurrentIndex(max(index, 0))
        self._cfg.translation.model = self.model_combo.currentData()
        view = self.model_combo.view()  # let the list be wider than the field, so no option is cut off
        view.setMinimumWidth(view.sizeHintForColumn(0) + 2 * view.frameWidth() + 16)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        with self._quiet():  # models may have been deleted on the General tab, or Recognition changed
            self._fill_models()
        self._update_details()

    def _update_details(self) -> None:
        spec = self._spec()
        self.details.setVisible(spec is not None)
        self.foreign_check.setEnabled(spec is not None)
        if spec is not None:
            language = spec.name.split(" →")[0]
            self.foreign_check.setText(f"Only {language} lines (skip English and other languages)"
                                       if spec.language else "Skip lines that are already in English")
            self.size_label.setText(f"about {spec.size_mb:,} MB")
            self.status_label.setText("Downloaded" if is_model_downloaded(spec)
                                      else f"Will download ~{spec.size_mb:,} MB when applied")
            self.license_label.setText(spec.license)
            self.notes_label.setText(spec.notes)
        warning = self._recognition_problem() if spec is not None else ""
        self.recognition_warning.setText(f"⚠ {warning}" if warning else "")
        self.recognition_warning.setVisible(bool(warning))

    def _recognition_problem(self) -> str:
        """Why the captions can't contain anything to translate, if they can't."""
        asr = self._cfg.asr
        try:
            spec = get_model(asr.model)
        except KeyError:
            return ""
        if asr.task == "translate" and spec.can_translate:
            return ("Whisper already turns the speech into English (Recognition › Task), so there's "
                    "nothing left to translate.")
        if spec.languages == "English":
            return (f"The recognition model ({spec.name.split(' (')[0]}) only writes English, so there's "
                    "nothing to translate. In Recognition, pick one for the language being spoken: "
                    "SenseVoice for Chinese, Japanese or Korean, Parakeet v3 for European languages, or Whisper.")
        if asr.language == "en":
            return ("Recognition › Language is English, so other languages come out as English-sounding words. "
                    "Set it to Auto-detect or to the language being spoken.")
        return ""

    def _on_model_changed(self, _index: int) -> None:
        if not self._loading:
            self._cfg.translation.model = self.model_combo.currentData() or ""
        self._update_details()

    def _on_foreign_toggled(self, checked: bool) -> None:
        if not self._loading:
            self._cfg.translation.only_foreign = checked


class GeneralTab(_Tab):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.captions_check = QCheckBox("On")
        self.captions_check.setToolTip("Turn live captioning on or off")
        self.captions_check.toggled.connect(partial(self._set, "captions_enabled"))
        self.hotkey_edit = QKeySequenceEdit()
        self.hotkey_edit.setMaximumSequenceLength(1)
        self.hotkey_edit.setClearButtonEnabled(True)
        self.hotkey_edit.setToolTip("Click, then press the key combination you want")
        self.hotkey_edit.keySequenceChanged.connect(self._on_hotkey_changed)
        self.config_button = QPushButton("Open config folder")
        self.config_button.clicked.connect(lambda: _open_folder(paths.config_dir()))
        self.log_button = QPushButton("Open log folder")
        self.log_button.setToolTip("The technical log (status and errors, never what anyone said), for troubleshooting")
        self.log_button.clicked.connect(lambda: _open_folder(paths.logs_dir()))

        # Downloaded models: what takes disk space, and removing what the settings don't use.
        self.models_tree = QTreeWidget()
        self.models_tree.setHeaderLabels(["Model", "Size", "Status"])
        self.models_tree.setRootIsDecorated(False)
        self.models_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.models_tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.models_tree.itemSelectionChanged.connect(self._update_model_buttons)
        self.models_total = QLabel()
        self.delete_model_button = QPushButton("Delete")
        self.delete_model_button.setToolTip("Remove the selected models from this PC")
        self.delete_model_button.clicked.connect(lambda: self._delete(self._selected_downloads()))
        self.delete_unused_button = QPushButton("Delete all unused")
        self.delete_unused_button.setToolTip("Remove every model the settings don't use")
        self.delete_unused_button.clicked.connect(lambda: self._delete(self._deletable()))
        self.models_button = QPushButton("Open folder")
        self.models_button.setToolTip(str(paths.models_dir()))
        self.models_button.clicked.connect(lambda: _open_folder(paths.models_dir()))
        self._downloads: list[storage.Download] = []
        self.saved_config: Callable[[], AppConfig] | None = None  # the applied settings, which the app runs

        form = _form()
        form.addRow("Captions:", _field(
            self.captions_check,
            "Captions are shown live and kept in memory only: nothing anyone says is saved to disk."))
        form.addRow("Lock hotkey:", _field(
            _row(self.hotkey_edit, stretch=True),
            "Locks or unlocks the caption windows from anywhere, even in a game. Locked windows "
            "are click-through; unlocked ones can be dragged and resized. Include Ctrl, Alt or "
            "Shift so it doesn't fire while typing."))
        form.addRow("Folders:", _field(_row(self.config_button, self.log_button, stretch=True)))
        form.addRow("Version:", QLabel(f"VoiceRhino {__version__}"))

        models = QGroupBox("Downloaded models")
        models_column = QVBoxLayout(models)
        models_column.addWidget(self.models_tree, 1)
        models_column.addLayout(_row(self.models_total, 1, self.delete_model_button,
                                     self.delete_unused_button, self.models_button))
        models_column.addWidget(Hint("Models the settings don't use can be deleted to free disk space. "
                                     "A deleted model downloads again if you pick it later."))

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(models, 1)

    def _load(self) -> None:
        self.captions_check.setChecked(bool(self._cfg.captions_enabled))
        self.hotkey_edit.setKeySequence(QKeySequence.fromString(
            str(self._cfg.lock_hotkey or ""), QKeySequence.SequenceFormat.PortableText))
        self.refresh_downloads()

    # -- downloaded models --

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh_downloads()  # the model choice may have changed on the Recognition tab

    def _in_use(self) -> set[str]:
        keys = storage.used_keys(self._cfg)
        if self.saved_config is not None:  # the running app still uses these until Apply
            keys |= storage.used_keys(self.saved_config())
        return keys

    def _deletable(self) -> list[storage.Download]:
        used = self._in_use()
        return [d for d in self._downloads if d.unfinished or d.key not in used]

    def refresh_downloads(self) -> None:
        try:
            self._downloads = storage.list_downloads()
        except OSError:
            log.exception("couldn't list the models folder")
            self._downloads = []
        used = self._in_use()
        self.models_tree.clear()
        for d in self._downloads:
            status = ("Unfinished download" if d.unfinished else "In use" if d.key in used
                      else "Not in use" if d.key else "Not used by this version")
            item = QTreeWidgetItem([d.name, storage.format_size(d.size), status])
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item.setToolTip(0, str(d.path))
            if status == "In use":
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.models_tree.addTopLevelItem(item)
        unused = sum(d.size for d in self._deletable())
        self.models_total.setText(f"{storage.format_size(sum(d.size for d in self._downloads))} in total, "
                                  f"{storage.format_size(unused)} not in use")
        self._update_model_buttons()

    def _selected_downloads(self) -> list[storage.Download]:
        deletable = {id(d) for d in self._deletable()}
        rows = sorted(self.models_tree.indexOfTopLevelItem(i) for i in self.models_tree.selectedItems())
        return [self._downloads[r] for r in rows if id(self._downloads[r]) in deletable]

    def _update_model_buttons(self) -> None:
        self.delete_model_button.setEnabled(bool(self._selected_downloads()))
        self.delete_unused_button.setEnabled(bool(self._deletable()))

    def _delete(self, items: list[storage.Download]) -> None:
        if not items:
            return
        listing = "\n".join(f"• {d.name} ({storage.format_size(d.size)})" for d in items)
        total = storage.format_size(sum(d.size for d in items))
        answer = QMessageBox.question(self, "Delete downloaded models",
                                      f"Delete these from this PC?\n\n{listing}\n\nThis frees {total}. "
                                      "A model downloads again if you pick it later.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        failed = []
        for d in items:
            try:
                storage.delete_download(d.path)
            except (OSError, ValueError) as exc:
                log.warning("couldn't delete %s: %s", d.path, exc)
                failed.append(f"{d.name}: {exc}")
        self.refresh_downloads()
        if failed:
            QMessageBox.warning(self, "Couldn't delete everything", "\n".join(failed))

    def _set(self, name: str, value: bool) -> None:
        if not self._loading:
            setattr(self._cfg, name, bool(value))

    def _on_hotkey_changed(self, sequence: QKeySequence) -> None:
        if not self._loading:
            self._cfg.lock_hotkey = sequence.toString(QKeySequence.SequenceFormat.PortableText)


# ----------------------------------------------------------------- dialog ---


class SettingsDialog(QDialog):
    """Edits a deep copy of the config. Apply and OK emit `applied(AppConfig)`.

    Works modal (exec) or modeless (show). Cancel / closing discards edits made
    since the last Apply, so showing the same dialog again starts clean.
    """

    applied = Signal(object)

    def __init__(self, config: AppConfig, parent: QWidget | None = None, levels: LevelProvider | None = None):
        super().__init__(parent)
        self.setWindowTitle(WINDOW_TITLE)
        self.setMinimumSize(720, 560)
        self.resize(780, 620)
        self.setSizeGripEnabled(True)
        self._saved = copy.deepcopy(config)  # what Cancel returns to
        self._config = copy.deepcopy(config)  # the working copy the tabs edit

        self.audio_tab = AudioTab()
        self.recognition_tab = RecognitionTab()
        self.timing_tab = TimingTab()
        self.windows_tab = WindowsTab()
        self.speakers_tab = SpeakersTab()
        self.translation_tab = TranslationTab()
        self.general_tab = GeneralTab()
        self.general_tab.saved_config = lambda: self._saved
        self._pages: list[_Tab] = [self.audio_tab, self.recognition_tab, self.timing_tab,
                                   self.windows_tab, self.speakers_tab, self.translation_tab,
                                   self.general_tab]
        self.tabs = QTabWidget()
        titles = ["Audio", "Recognition", "Timing", "Windows", "Speakers", "Translation", "General"]
        for page, title in zip(self._pages, titles):
            self.tabs.addTab(page, title)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
        )
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        self.button_box.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.button_box)
        self._load()
        self.audio_tab.set_level_provider(levels)

    def _load(self) -> None:
        for page in self._pages:
            page.load(self._config)

    def result_config(self) -> AppConfig:
        """A copy of the edited config (later edits don't change it)."""
        return copy.deepcopy(self._config)

    def set_config(self, config: AppConfig) -> None:
        """Replace what the dialog edits (e.g. the app's current config before show())."""
        self._saved = copy.deepcopy(config)
        self._config = copy.deepcopy(config)
        self._load()

    def update_view_geometry(self, view_id: str, x: int, y: int, w: int, h: int) -> None:
        """Record a window the user dragged/resized while this dialog is open, so Apply keeps it."""
        for config in (self._config, self._saved):
            for view in config.views:
                if view.id == view_id:
                    view.x, view.y, view.w, view.h = x, y, w, h
        self.windows_tab.refresh_placement()

    def apply(self) -> None:
        config = self.result_config()
        self._saved = copy.deepcopy(config)
        self.applied.emit(config)

    def accept(self) -> None:
        self.apply()
        super().accept()

    def reject(self) -> None:
        self._config = copy.deepcopy(self._saved)
        self._load()
        super().reject()
