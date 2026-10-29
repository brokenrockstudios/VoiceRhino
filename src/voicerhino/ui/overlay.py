"""On-screen caption windows.

OverlayWindow: frameless, translucent, always on top, never steals focus from
the game. Unlocked it shows a dashed frame, drags to move and resizes from its
edges; locked it has no frame and (if the view says so) lets clicks through.

CaptionWindow: big roll-up captions (last N lines) with outlined text.
HistoryWindow: the last N seconds with timestamps and speaker names.
"""

from __future__ import annotations

import html
import time

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication, QPainter,
                           QPainterPath, QPen, QTextDocument)
from PySide6.QtWidgets import QWidget

from ..config import ViewConfig
from ..transcript import TranscriptStore, Utterance
from . import winutil
from .theme import speaker_color

EDGE = 8  # px from the border that resizes instead of moving
ACCENT = QColor(88, 101, 242)
LABEL_FALLBACK = "#9aa5ff"


def _color(hex_color: str, alpha: float = 1.0) -> QColor:
    c = QColor(hex_color)
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


def _font(view: ViewConfig, bold: bool | None = None) -> QFont:
    font = QFont(view.font_family)
    font.setPixelSize(max(6, view.font_size))
    font.setBold(view.bold if bold is None else bold)
    return font


class OverlayWindow(QWidget):
    geometry_saved = Signal(object)  # the ViewConfig, after the user moved/resized it
    menu_requested = Signal(object, QPoint)  # (window, global position)

    def __init__(self, view: ViewConfig, store: TranscriptStore):
        flags = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                 | Qt.WindowDoesNotAcceptFocus)
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.store = store
        self.view = view
        self.locked = False
        self.multi_source = False  # label lines with the source name when several run
        self._status = ""
        self._status_until = 0.0
        self.warning: str | None = None  # small note kept at the top, e.g. "Discord is very quiet"
        self._save_timer = QTimer(self, singleShot=True, interval=400, timeout=self._save_geometry)
        self._top_timer = QTimer(self, interval=3000, timeout=self._keep_on_top)
        self._top_timer.start()
        self.apply_view(view)

    # --- configuration -------------------------------------------------
    def apply_view(self, view: ViewConfig) -> None:
        self.view = view
        # Only (-1, -1) means "not placed yet": monitors left of or above the
        # primary one have legitimately negative coordinates.
        if view.x == -1 and view.y == -1:
            self._auto_place()
        self.setGeometry(view.x, view.y, max(80, view.w), max(40, view.h))
        self._update_input_mode()
        self.update()

    def _auto_place(self) -> None:
        screen = QGuiApplication.primaryScreen().availableGeometry()
        v = self.view
        if v.kind == "caption":
            v.w = min(v.w, int(screen.width() * 0.7))
            v.x = screen.x() + (screen.width() - v.w) // 2
            v.y = screen.y() + screen.height() - v.h - int(screen.height() * 0.08)
        else:
            v.x = screen.x() + screen.width() - v.w - 24
            v.y = screen.y() + int(screen.height() * 0.15)

    def set_locked(self, locked: bool) -> None:
        self.locked = locked
        self._update_input_mode()
        self.update()

    def _update_input_mode(self) -> None:
        transparent = self.locked and self.view.click_through
        if bool(self.windowFlags() & Qt.WindowTransparentForInput) != transparent:
            visible = self.isVisible()
            self.setWindowFlag(Qt.WindowTransparentForInput, transparent)
            if visible:
                self.show()
        self.unsetCursor()

    def show_status(self, text: str, seconds: float = 6.0) -> None:
        """Small message shown while there are no captions (loading, errors)."""
        self._status = text
        self._status_until = time.monotonic() + seconds
        self.update()

    def set_warning(self, text: str | None) -> None:
        """Show a small persistent note at the top of the window (None clears it).
        Unlike show_status it stays visible over captions (in a caption window, only
        while captions show). Hidden in windows with show_warnings off."""
        if text != self.warning:
            self.warning = text
            self.update()

    def on_utterance(self, utt: Utterance) -> None:
        self.update()

    def shows(self, utt: Utterance) -> bool:
        """Whether this window shows captions from utt's source (Settings › Windows › Captions from)."""
        return utt.source_id not in self.view.hidden_sources

    def _keep_on_top(self) -> None:
        if self.isVisible():
            winutil.raise_topmost(self)
        if self._status and time.monotonic() > self._status_until:
            self._status = ""
            self.update()

    # --- move / resize while unlocked --------------------------------------
    def _edges(self, pos: QPoint) -> Qt.Edges:
        edges = Qt.Edges()
        if pos.x() <= EDGE:
            edges |= Qt.LeftEdge
        if pos.x() >= self.width() - EDGE:
            edges |= Qt.RightEdge
        if pos.y() <= EDGE:
            edges |= Qt.TopEdge
        if pos.y() >= self.height() - EDGE:
            edges |= Qt.BottomEdge
        return edges

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.RightButton:
            self.menu_requested.emit(self, event.globalPosition().toPoint())
        elif event.button() == Qt.LeftButton and not self.locked:
            edges = self._edges(event.position().toPoint())
            if edges:
                self.windowHandle().startSystemResize(edges)
            else:
                self.windowHandle().startSystemMove()

    def mouseMoveEvent(self, event) -> None:
        if self.locked:
            return
        e = self._edges(event.position().toPoint())
        if e in (Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeFDiagCursor)
        elif e in (Qt.RightEdge | Qt.TopEdge, Qt.LeftEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeBDiagCursor)
        elif e & (Qt.LeftEdge | Qt.RightEdge):
            self.setCursor(Qt.SizeHorCursor)
        elif e & (Qt.TopEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.setCursor(Qt.SizeAllCursor)

    def moveEvent(self, event) -> None:
        self._save_timer.start()

    def resizeEvent(self, event) -> None:
        self._save_timer.start()
        super().resizeEvent(event)

    def _save_geometry(self) -> None:
        g, v = self.geometry(), self.view
        if (v.x, v.y, v.w, v.h) != (g.x(), g.y(), g.width(), g.height()):
            v.x, v.y, v.w, v.h = g.x(), g.y(), g.width(), g.height()
            self.geometry_saved.emit(v)

    # --- painting ---------------------------------------------------------
    def _paint_frame(self, p: QPainter, has_content: bool) -> None:
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if has_content or not self.locked:
            alpha = self.view.background_opacity if has_content else max(0.35, self.view.background_opacity)
            p.setPen(Qt.NoPen)
            p.setBrush(_color(self.view.background, alpha))
            p.drawRoundedRect(r, 10, 10)
        if not self.locked:
            p.setPen(QPen(ACCENT, 2, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r, 10, 10)
            hint = QFont("Segoe UI")
            hint.setPixelSize(12)
            p.setFont(hint)
            p.setPen(QColor(205, 208, 220))
            title = "Captions" if self.view.kind == "caption" else f"History (last {self.view.history_seconds}s)"
            p.drawText(r.adjusted(10, 4, -10, -4), Qt.AlignTop | Qt.AlignLeft, title)
            p.drawText(r.adjusted(10, 4, -10, -4), Qt.AlignTop | Qt.AlignRight,
                       "drag to move · edges resize · right-click for options")

    def _shown_warning(self) -> str | None:
        return self.warning if self.view.show_warnings else None

    def _paint_warning(self, p: QPainter) -> None:
        warning = self._shown_warning()
        if not warning:
            return
        f = QFont("Segoe UI")
        f.setPixelSize(max(13, round(self.view.font_size * 0.4)))
        f.setBold(True)
        p.setFont(f)
        top = 4 if self.locked else 20  # below the "drag to move" hints while unlocked
        r = QRectF(self.rect()).adjusted(10, top, -10, -4)
        flags = Qt.AlignTop | Qt.AlignHCenter
        p.setPen(QColor(0, 0, 0, 220))  # shadow, so it reads over a bright game too
        p.drawText(r.translated(1, 1), flags, warning)
        p.setPen(QColor(255, 183, 77))
        p.drawText(r, flags, warning)

    def _paint_status(self, p: QPainter) -> None:
        if not self._status:
            return
        f = QFont("Segoe UI")
        f.setPixelSize(max(13, self.view.font_size // 2))
        p.setFont(f)
        p.setPen(QColor(230, 230, 235))
        p.drawText(QRectF(self.rect()).adjusted(12, 20, -12, -8), Qt.AlignCenter | Qt.TextWordWrap, self._status)

    def _label(self, u: Utterance) -> tuple[str | None, str | None]:
        if self.view.show_speakers and u.speaker:
            return u.speaker, speaker_color(u.speaker_key)
        if self.multi_source:
            return u.source, None
        return None, None


class CaptionWindow(OverlayWindow):
    """Roll-up captions: newest text at the bottom, older lines scroll off the top."""

    MAX_UTTERANCES = 12

    def __init__(self, view: ViewConfig, store: TranscriptStore):
        self._last_activity = 0.0
        self._cleared_at = 0.0
        self._showing = False
        super().__init__(view, store)
        self._tick = QTimer(self, interval=500, timeout=self._expire)
        self._tick.start()

    def on_utterance(self, utt: Utterance) -> None:
        if utt.text and self.shows(utt):  # a hidden source's speech mustn't keep old captions up
            self._last_activity = time.monotonic()
        self.update()

    def _expire(self) -> None:
        v = self.view
        if self._showing and v.clear_after > 0 and time.monotonic() - self._last_activity > v.clear_after:
            self._cleared_at = time.monotonic()
            self.update()

    def _items(self) -> list[tuple[str | None, str | None, str, bool]]:
        """(label, label color, text, is_partial) in display order."""
        now = time.monotonic()
        if self.view.clear_after > 0 and now - self._last_activity > self.view.clear_after:
            return []
        finals = [u for u in self.store.finals(since=now - 120) if u.end > self._cleared_at and self.shows(u)]
        items = []
        for u in finals[-self.MAX_UTTERANCES:] + [u for u in self.store.partials() if self.shows(u)]:
            label, color = self._label(u)
            items.append((label, color, u.translation or u.text, not u.final))
        return items

    def _layout(self, items, font: QFont, label_font: QFont, width: float):
        """Word-wrap colored runs into lines: list of [(text, color, is_label)]."""
        fm, lfm = QFontMetricsF(font), QFontMetricsF(label_font)
        lines: list[list[tuple[str, str, bool]]] = []
        cur: list[tuple[str, str, bool]] = []
        cur_w = 0.0
        prev_label: object = object()
        for label, label_color, text, partial in items:
            color = self.view.partial_color if partial else self.view.text_color
            if label != prev_label:  # new speaker starts a new line, TV-caption style
                if cur:
                    lines.append(cur)
                cur, cur_w = [], 0.0
                if label:
                    run = f"{label}: "
                    cur.append((run, label_color or LABEL_FALLBACK, True))
                    cur_w += lfm.horizontalAdvance(run)
                prev_label = label
            for word in text.split():
                word_w = fm.horizontalAdvance(word + " ")
                if cur and cur_w + word_w > width:
                    lines.append(cur)
                    cur, cur_w = [], 0.0
                if cur and cur[-1][1] == color and not cur[-1][2]:
                    cur[-1] = (cur[-1][0] + word + " ", color, False)
                else:
                    cur.append((word + " ", color, False))
                cur_w += word_w
        if cur:
            lines.append(cur)
        return lines

    def paintEvent(self, event) -> None:
        v = self.view
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        font, label_font = _font(v), _font(v, bold=True)
        pad = max(10, v.font_size // 3)
        lines = self._layout(self._items(), font, label_font, self.width() - 2 * pad)[-max(1, v.lines):]
        self._showing = bool(lines)
        self._paint_frame(p, self._showing)
        if not lines:
            self._paint_status(p)
            return
        self._paint_warning(p)  # with the captions only: an empty bar over a game stays empty
        fm, lfm = QFontMetricsF(font), QFontMetricsF(label_font)
        line_h = fm.lineSpacing()
        y = self.height() - pad - line_h * len(lines) + fm.ascent()
        outline = QPen(QColor(0, 0, 0, 230), v.outline * 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        for line in lines:
            widths = [(lfm if is_label else fm).horizontalAdvance(t) for t, _, is_label in line]
            x = pad + max(0.0, (self.width() - 2 * pad - sum(widths)) / 2)
            for (text, color, is_label), w in zip(line, widths):
                path = QPainterPath()
                path.addText(QPointF(x, y), label_font if is_label else font, text.rstrip())
                if v.outline > 0:
                    p.strokePath(path, outline)
                p.fillPath(path, QColor(color))
                x += w
            y += line_h


class HistoryWindow(OverlayWindow):
    """The last N seconds as a log, newest at the bottom; mouse wheel scrolls back."""

    def __init__(self, view: ViewConfig, store: TranscriptStore):
        self._scroll = 0.0  # px scrolled up from the bottom
        self._scrolled_at = 0.0
        self._oldest_shown: float | None = None  # end time of the oldest line on screen
        self._doc = QTextDocument()
        self._doc.setDocumentMargin(0)
        super().__init__(view, store)
        self._tick = QTimer(self, interval=1000, timeout=self._expire)
        self._tick.start()

    def _expire(self) -> None:
        now = time.monotonic()
        if self._scroll and now - self._scrolled_at > 20:
            self._scroll = 0.0  # snap back to live after a while
            self.update()
        if self._oldest_shown is not None and now - self.view.history_seconds > self._oldest_shown:
            self.update()  # a line just aged out

    def _html(self) -> str:
        v = self.view
        rows = []
        finals = [u for u in self.store.finals(since=time.monotonic() - v.history_seconds) if self.shows(u)]
        self._oldest_shown = finals[0].end if finals else None
        utterances = finals + [u for u in self.store.partials() if self.shows(u)]
        for u in utterances:
            parts = []
            if v.show_timestamps:
                stamp = time.strftime("%H:%M:%S", time.localtime(u.wall_start))
                parts.append(f'<span style="color:#8a8d93">{stamp}</span> ')
            label, color = self._label(u)
            if label:
                parts.append(f'<b style="color:{color or LABEL_FALLBACK}">{html.escape(label)}:</b> ')
            style = f"color:{v.text_color}" if u.final else f"color:{v.partial_color};font-style:italic"
            parts.append(f'<span style="{style}">{html.escape(u.text)}</span>')
            if u.translation:
                parts.append(f'<br><span style="color:#a0c4ff;font-style:italic">{html.escape(u.translation)}</span>')
            rows.append(f'<p style="margin:0 0 6px 0">{"".join(parts)}</p>')
        return "".join(rows)

    def wheelEvent(self, event) -> None:
        self._scroll = max(0.0, self._scroll + event.angleDelta().y() / 120 * 3 * self.view.font_size)
        self._scrolled_at = time.monotonic()
        self.update()

    def paintEvent(self, event) -> None:
        v = self.view
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        markup = self._html()
        self._paint_frame(p, bool(markup))
        self._paint_warning(p)
        if not markup:
            self._paint_status(p)
            return
        pad = 12
        top = pad + (18 if not self.locked else 0) + (18 if self._shown_warning() else 0)
        width, avail = self.width() - 2 * pad, self.height() - top - pad
        self._doc.setDefaultFont(_font(v))
        self._doc.setTextWidth(width)
        self._doc.setHtml(markup)
        doc_h = self._doc.size().height()
        self._scroll = min(self._scroll, max(0.0, doc_h - avail))
        p.save()
        p.setClipRect(QRectF(pad, top, width, avail))
        p.translate(pad, top + min(0.0, avail - doc_h) + self._scroll)
        self._doc.drawContents(p)
        p.restore()
        if self._scroll:
            p.setPen(QColor(205, 208, 220))
            p.drawText(QRectF(self.rect()).adjusted(0, 0, -pad, -4), Qt.AlignBottom | Qt.AlignRight,
                       "▼ scrolled back")


def make_window(view: ViewConfig, store: TranscriptStore) -> OverlayWindow:
    return HistoryWindow(view, store) if view.kind == "history" else CaptionWindow(view, store)
