"""The sniffler look, frozen in one place: palette, spacing, and type.

The three measured colors come from the squirrel artwork. Everything else is a
derivative of them or a neutral. Pink has one meaning: live attention (the sniff,
the current step, the progress cursor, a high deviation). Navy carries text and
the primary controls; the cream family carries surfaces.

The window stays light on every platform. Qt gives no portable dark-mode palette
signal that the tests can exercise, and the lab's Windows desktops are light, so
the palette is fixed and the Fusion style draws it the same way on macOS, Windows,
and the offscreen platform.
"""

from pathlib import Path

from PySide6.QtGui import QColor, QFont, QIcon, QPalette
from PySide6.QtWidgets import QApplication

ASSETS = Path(__file__).with_name("assets")

# Measured from the artwork.
NAVY = "#122d58"
CREAM = "#f3d39b"
PINK = "#e83879"

# Derivatives and neutrals.
PINK_TEXT = "#9e204e"  # pink darkened until it reads as text on the light surfaces
PINK_WASH = "#fbe1ea"  # pink at low opacity over white: the current step row
SURFACE = "#fbf5e8"  # cream lightened: the window background
PANEL = "#ffffff"  # tables, fields, the plot
INK_SOFT = "#4f5f7a"  # navy lightened: secondary text
DISABLED = "#76819a"  # navy lightened further: disabled text and icons
LINE = "#d9d0bf"  # borders, from cream
GOLD = "#8a5f10"  # cream darkened: the second plot series

# Trial fills in the timeline: one lightness ramp of navy, so the order reads
# without color vision. The legend names each trial as well.
TRIAL_RAMP = ("#122d58", "#3d5480", "#6c7fa3", "#9aa9c4", "#c4cde0", "#e2e7f0")
# Plot series: one color for each MFC; commanded is dashed, actual is solid.
SERIES = (NAVY, GOLD, "#6c7fa3")

# Spacing: one unit. Layout margins and gaps are multiples of it.
UNIT = 8
GAP = UNIT
SECTION_GAP = 2 * UNIT
MARGIN = 2 * UNIT

# Type: three sizes, two weights (normal and bold).
BODY_PX = 13
TITLE_PX = 18
DISPLAY_PX = 34

ICON_PX = 20
CHECK_PX = 16  # the QCheckBox indicator
SWITCH_W = 28  # the painted switch in valve cells: track width and height
SWITCH_H = 16
CHECK_MARK = (ASSETS / "icons" / "check-white.svg").as_posix()
# Measured platform metric: the Fusion check box is 14 px high plus the text
# line; 28 px keeps a valve cell one click tall without clipping the box.
ROW_PX = 28

# Every (foreground, background) pair that carries information. The contrast
# test reads this table.
TEXT_PAIRS = {
    "body on window": (NAVY, SURFACE),
    "body on panel": (NAVY, PANEL),
    "body on invalid cell": (NAVY, CREAM),
    "body on current step": (NAVY, PINK_WASH),
    "secondary on window": (INK_SOFT, SURFACE),
    "secondary on panel": (INK_SOFT, PANEL),
    "attention text on window": (PINK_TEXT, SURFACE),
    "attention text on panel": (PINK_TEXT, PANEL),
    "primary button text": (PANEL, NAVY),
    "selected row text": (PANEL, NAVY),
}
LARGE_PAIRS = {
    "disabled text on window": (DISABLED, SURFACE),
    "icon on window": (NAVY, SURFACE),
    "icon on panel": (NAVY, PANEL),
    "attention mark on window": (PINK, SURFACE),
    "attention mark on panel": (PINK, PANEL),
    "gold series on panel": (GOLD, PANEL),
    "light series on panel": ("#6c7fa3", PANEL),
}


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG 2 contrast ratio between two sRGB colors."""

    def luminance(color: str) -> float:
        channels = []
        for value in QColor(color).getRgb()[:3]:
            scaled = value / 255
            channels.append(
                scaled / 12.92 if scaled <= 0.04045 else ((scaled + 0.055) / 1.055) ** 2.4
            )
        red, green, blue = channels
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def font(size_px: int = BODY_PX, *, bold: bool = False) -> QFont:
    result = QFont(QApplication.font())
    result.setPixelSize(size_px)
    result.setBold(bold)
    return result


def palette() -> QPalette:
    result = QPalette()
    result.setColor(QPalette.Window, QColor(SURFACE))
    result.setColor(QPalette.WindowText, QColor(NAVY))
    result.setColor(QPalette.Base, QColor(PANEL))
    result.setColor(QPalette.AlternateBase, QColor(SURFACE))
    result.setColor(QPalette.Text, QColor(NAVY))
    result.setColor(QPalette.PlaceholderText, QColor(INK_SOFT))
    result.setColor(QPalette.Button, QColor(SURFACE))
    result.setColor(QPalette.ButtonText, QColor(NAVY))
    result.setColor(QPalette.Highlight, QColor(NAVY))
    result.setColor(QPalette.HighlightedText, QColor(PANEL))
    result.setColor(QPalette.Link, QColor(PINK_TEXT))
    result.setColor(QPalette.ToolTipBase, QColor(NAVY))
    result.setColor(QPalette.ToolTipText, QColor(PANEL))
    result.setColor(QPalette.Light, QColor(PANEL))
    result.setColor(QPalette.Midlight, QColor(SURFACE))
    result.setColor(QPalette.Mid, QColor(LINE))
    result.setColor(QPalette.Dark, QColor(INK_SOFT))
    result.setColor(QPalette.Shadow, QColor(NAVY))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        result.setColor(QPalette.Disabled, role, QColor(DISABLED))
    return result


STYLE_SHEET = f"""
QToolTip {{ color: {PANEL}; background: {NAVY}; border: 1px solid {NAVY}; padding: 4px; }}
QPushButton#primary {{
    background: {NAVY}; color: {PANEL}; border: 1px solid {NAVY};
    border-radius: 4px; padding: 6px 14px; font-weight: bold;
}}
QPushButton#primary:disabled {{ background: {SURFACE}; color: {DISABLED}; border-color: {LINE}; }}
QPushButton#consequential {{ font-weight: bold; padding: 6px 14px; }}
QPushButton#tabCorner {{ font-weight: bold; padding: 2px 14px; }}  /* fits the tab bar row */
QHeaderView::section {{ background: {SURFACE}; color: {INK_SOFT}; border: 0;
    border-bottom: 1px solid {LINE}; border-right: 1px solid {LINE}; padding: 4px 6px; }}
QTableView, QTableWidget, QListWidget {{ gridline-color: {LINE}; border: 1px solid {LINE}; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {NAVY}; border-radius: 3px;
    background: {PANEL}; }}
QCheckBox::indicator:checked {{ background: {NAVY}; image: url({CHECK_MARK}); }}
QCheckBox::indicator:disabled {{ border-color: {DISABLED}; }}
"""


def window_icon() -> QIcon:
    """The color squirrel, in every size the dock, taskbar, and title bar ask for."""
    return QIcon(str(ASSETS / "squirrel.ico"))


def apply(application: QApplication) -> None:
    """Style, palette, font, and window icon. Call once before the window is built."""
    application.setStyle("Fusion")
    application.setPalette(palette())
    body = QFont(application.font())
    body.setPixelSize(BODY_PX)
    application.setFont(body)
    application.setStyleSheet(STYLE_SHEET)
    application.setWindowIcon(window_icon())
