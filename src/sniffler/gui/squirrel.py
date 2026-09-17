"""The squirrel in the Run tab: identity while idle, the sniff on each odor onset.

The artwork is shipped as two tinted layers of the same drawing, split at the nose:
the body and the scent lines. The sniff moves and recolors the scent layer only.
Motion never runs on the executor or MFC threads; it is a GUI-side animation that
restarts on every onset, so a fast pulse train retargets the cue instead of
queueing behind the run. Under reduced motion the nose lights up instead.
"""

import os
import subprocess
import sys

from PySide6.QtCore import QEasingCurve, QRectF, QSize, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPaintEvent, QPixmap
from PySide6.QtWidgets import QWidget

from sniffler.executor import Phase
from sniffler.gui import theme

CUE_MS = 900
# The nose, as a fraction of the artwork; measured on the line art.
NOSE = QRectF(0.653, 0.394, 0.063, 0.071)
RISE_FRACTION = 0.08
IDLE_SCENT_OPACITY = 0.28


def reduce_motion() -> bool:
    """Read the platform reduce-motion setting. SNIFFLER_REDUCE_MOTION overrides it."""
    setting = os.environ.get("SNIFFLER_REDUCE_MOTION")
    if setting is not None:
        return setting.strip().lower() not in {"", "0", "false", "no"}
    try:
        if sys.platform == "darwin":
            result = subprocess.run(
                ["defaults", "read", "com.apple.universalaccess", "reduceMotion"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            return result.stdout.strip() == "1"
        if sys.platform == "win32":
            import ctypes

            animation = ctypes.c_int(1)
            spi_getclientareaanimation = 0x1042
            if ctypes.windll.user32.SystemParametersInfoW(
                spi_getclientareaanimation, 0, ctypes.byref(animation), 0
            ):
                return animation.value == 0
    except (OSError, subprocess.TimeoutExpired, AttributeError):
        pass
    return False


def _tinted(pixmap: QPixmap, color: str) -> QPixmap:
    result = QPixmap(pixmap.size())
    result.fill(Qt.transparent)
    painter = QPainter(result)
    painter.drawPixmap(0, 0, pixmap)
    painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
    painter.fillRect(result.rect(), QColor(color))
    painter.end()
    return result


class SniffWidget(QWidget):
    """The squirrel plus the name of the valve that opened last."""

    def __init__(self, parent: QWidget | None = None, *, reduced: bool | None = None) -> None:
        super().__init__(parent)
        self.reduced_motion = reduce_motion() if reduced is None else reduced
        self._body = QPixmap(str(theme.ASSETS / "squirrel-body.png"))
        self._scent = QPixmap(str(theme.ASSETS / "squirrel-scent.png"))
        self._body_pink = _tinted(self._body, theme.PINK)
        self._scent_pink = _tinted(self._scent, theme.PINK)
        self._phase = Phase.IDLE
        self._open: list[str] = []  # the valves that are open now, in opening order
        self._progress = 0.0  # 0 = onset, 1 = cue finished
        self._cue_active = False
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(CUE_MS)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)
        self._animation.valueChanged.connect(self._on_progress)
        self._animation.finished.connect(self._end_cue)
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.setInterval(CUE_MS)
        self._hold.timeout.connect(self._end_cue)
        self.setMinimumSize(QSize(240, 112))
        self.setMaximumHeight(168)
        self.setAccessibleName("Odor onset")

    # State ---------------------------------------------------------------

    @property
    def cue_name(self) -> str:
        """The open valves, shown beside the squirrel; empty when every valve is closed."""
        return " + ".join(self._open)

    @property
    def cue_active(self) -> bool:
        return self._cue_active

    @property
    def animating(self) -> bool:
        return self._animation.state() == QVariantAnimation.Running

    def set_phase(self, phase: Phase) -> None:
        self._phase = phase
        if phase in {Phase.STARTING}:
            self.clear()
        self.update()

    def clear(self) -> None:
        self._animation.stop()
        self._hold.stop()
        self._cue_active = False
        self._progress = 0.0
        self._open = []
        self.update()

    def show_open(self, valve: str) -> None:
        """Name a valve that is open, without a sniff (the shutdown state, for example)."""
        if valve not in self._open:
            self._open.append(valve)
        self.setAccessibleDescription(f"{self.cue_name} open" if self._open else "")
        self.update()

    def close(self, valve: str) -> None:
        """A valve closed: its name leaves the squirrel."""
        if valve in self._open:
            self._open.remove(valve)
            self.setAccessibleDescription(f"{self.cue_name} open" if self._open else "")
            self.update()

    def sniff(self, valves: list[str]) -> None:
        """Show an odor onset now. A new onset restarts the cue; nothing queues."""
        if not valves:
            return
        for valve in valves:
            self.show_open(valve)
        self._cue_active = True
        self._progress = 0.0
        if self.reduced_motion:
            self._hold.start()
        else:
            self._animation.stop()
            self._animation.start()
        self.update()

    def _on_progress(self, value: object) -> None:
        self._progress = float(value)  # type: ignore[arg-type]
        self.update()

    def _end_cue(self) -> None:
        self._cue_active = False
        self._progress = 0.0
        self.update()

    # Paint ---------------------------------------------------------------

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        area = self.rect()
        image_height = area.height()
        image_width = int(image_height * self._body.width() / self._body.height())
        if image_width > area.width() * 0.6:
            image_width = int(area.width() * 0.6)
            image_height = int(image_width * self._body.height() / self._body.width())
        image = QRectF(
            area.left(), area.top() + (area.height() - image_height) / 2, image_width, image_height
        )
        self._paint_scent(painter, image)
        painter.setOpacity(1.0)
        painter.drawPixmap(image, self._body, QRectF(self._body.rect()))
        if self._cue_active and self.reduced_motion:
            # Static equivalent of the sniff: the nose lights up.
            nose = QRectF(
                image.left() + NOSE.x() * image.width(),
                image.top() + NOSE.y() * image.height(),
                NOSE.width() * image.width(),
                NOSE.height() * image.height(),
            )
            painter.save()
            painter.setClipRect(nose)
            painter.drawPixmap(image, self._body_pink, QRectF(self._body_pink.rect()))
            painter.restore()
        self._paint_name(painter, area, image)

    def _paint_scent(self, painter: QPainter, image: QRectF) -> None:
        source = QRectF(self._scent.rect())
        if self._cue_active and not self.reduced_motion:
            # The scent rises from the nose and fades: the whole cue is one motion.
            rise = RISE_FRACTION * image.height() * self._progress
            painter.setOpacity(1.0 - self._progress)
            painter.drawPixmap(image.translated(0, -rise), self._scent_pink, source)
            return
        if self._phase == Phase.DONE:
            # This detail helps the task by making a finished run recognizable from
            # across the room: the scent and sparkles turn pink and stay.
            painter.setOpacity(1.0)
            painter.drawPixmap(image, self._scent_pink, source)
            return
        if self._phase in {Phase.FAILED, Phase.ABORTED}:
            return
        # Idle and running: the scent is faint so the sniff has somewhere to go.
        # This detail helps the task by showing, before the run starts, where the
        # odor cue will appear.
        painter.setOpacity(IDLE_SCENT_OPACITY)
        painter.drawPixmap(image, self._scent, source)

    def _paint_name(self, painter: QPainter, area, image: QRectF) -> None:
        if not self.cue_name:
            return
        text_area = QRectF(
            image.right() + theme.GAP,
            area.top(),
            area.right() - image.right() - theme.GAP,
            area.height(),
        )
        if text_area.width() < 40:
            return
        # Display size when the name fits, title size when it does not; then elide.
        painter.setFont(theme.font(theme.DISPLAY_PX, bold=True))
        if QFontMetrics(painter.font()).horizontalAdvance(self.cue_name) > text_area.width():
            painter.setFont(theme.font(theme.TITLE_PX, bold=True))
        painter.setPen(QColor(theme.PINK_TEXT if self._cue_active else theme.NAVY))
        text = QFontMetrics(painter.font()).elidedText(
            self.cue_name, Qt.ElideRight, int(text_area.width())
        )
        painter.drawText(text_area, Qt.AlignLeft | Qt.AlignVCenter, text)
