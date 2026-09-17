"""Shipped SVG icons, rendered here so they look the same on every platform.

The files are Tabler outline icons (MIT, see assets/LICENSES.md). Each SVG uses
``currentColor``; this module substitutes the palette color and rasterizes the
result, so nothing depends on the OS icon theme.
"""

from functools import cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from sniffler.gui import theme

# Command name -> Tabler icon file. One family, one stroke weight.
FILES = {
    "new": "file-plus",
    "open": "folder-open",
    "save": "device-floppy",
    "add": "plus",
    "remove": "trash",
    "duplicate": "copy",
    "move-up": "arrow-up",
    "move-down": "arrow-down",
    "pulse-train": "wave-square",
    "start": "player-play",
    "stop": "player-stop",
    "read-limits": "gauge",
}
SIZES = (16, 20, 24, 32)


def _render(name: str, color: str, size: int, ratio: int) -> QPixmap:
    source = (theme.ASSETS / "icons" / f"{FILES[name]}.svg").read_text(encoding="utf-8")
    renderer = QSvgRenderer(QByteArray(source.replace("currentColor", color).encode()))
    image = QImage(size * ratio, size * ratio, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, size * ratio, size * ratio))
    painter.end()
    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


@cache
def icon(name: str, color: str = theme.NAVY) -> QIcon:
    """Return the icon for a command name: ``color`` when enabled, grey when disabled.

    A control drawn on navy, such as the primary button, asks for a white icon.
    """
    result = QIcon()
    for size in SIZES:
        for ratio in (1, 2):
            result.addPixmap(_render(name, color, size, ratio), QIcon.Normal)
            result.addPixmap(_render(name, theme.DISABLED, size, ratio), QIcon.Disabled)
    return result
