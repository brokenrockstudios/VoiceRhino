"""Dark theme for dialogs, the tray icon, and stable per-speaker colors."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QApplication

# Bright, distinct colors that read well on dark/translucent backgrounds.
SPEAKER_COLORS = [
    "#FFD166", "#4CC9F0", "#06D6A0", "#F78C6B",
    "#C77DFF", "#FF6B9A", "#B5E48C", "#90E0EF",
]
_assigned: dict[str, str] = {}


def speaker_color(key: str | None) -> str | None:
    """Same speaker key -> same color for the whole session."""
    if not key:
        return None
    if key not in _assigned:
        _assigned[key] = SPEAKER_COLORS[len(_assigned) % len(SPEAKER_COLORS)]
    return _assigned[key]


def apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    p = QPalette()
    base, window, text = QColor("#1e1f22"), QColor("#2b2d31"), QColor("#e6e6e6")
    p.setColor(QPalette.Window, window)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, window)
    p.setColor(QPalette.ToolTipBase, base)
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, window)
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.Highlight, QColor("#5865f2"))
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.Link, QColor("#8ab4f8"))
    p.setColor(QPalette.PlaceholderText, QColor("#8a8d93"))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        p.setColor(QPalette.Disabled, role, QColor("#6d6f75"))
    app.setPalette(p)


def app_icon(active: bool = True) -> QIcon:
    """A 'CC' badge; grey when captions are paused."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 256):
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor("#5865f2" if active else "#6d6f75"))
        painter.setPen(Qt.NoPen)
        r = QRectF(0, size * 0.12, size, size * 0.76)
        painter.drawRoundedRect(r, size * 0.18, size * 0.18)
        font = QFont("Segoe UI", 1, QFont.Black)
        font.setPixelSize(max(6, int(size * 0.46)))
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(r, Qt.AlignCenter, "CC")
        painter.end()
        icon.addPixmap(pix)
    return icon
