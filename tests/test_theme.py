"""The look contract: every informative color pair meets WCAG AA; icons render everywhere."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from sniffler.gui import icons, theme

TEXT_MINIMUM = 4.5
LARGE_MINIMUM = 3.0


def contrast_table() -> list[tuple[str, str, str, float, float]]:
    rows = [
        (name, fg, bg, theme.contrast_ratio(fg, bg), TEXT_MINIMUM)
        for name, (fg, bg) in theme.TEXT_PAIRS.items()
    ]
    rows += [
        (name, fg, bg, theme.contrast_ratio(fg, bg), LARGE_MINIMUM)
        for name, (fg, bg) in theme.LARGE_PAIRS.items()
    ]
    return rows


class ContrastTests(unittest.TestCase):
    def test_every_informative_pair_meets_wcag_aa(self) -> None:
        for name, foreground, background, ratio, minimum in contrast_table():
            with self.subTest(pair=name):
                self.assertGreaterEqual(
                    ratio, minimum, f"{name}: {foreground} on {background} is {ratio:.2f}:1"
                )

    def test_pink_is_one_family(self) -> None:
        # Both pink constants are the same hue: live attention in two weights.
        from PySide6.QtGui import QColor

        mark, text = QColor(theme.PINK).hue(), QColor(theme.PINK_TEXT).hue()
        self.assertLess(abs(mark - text), 12)

    def test_reference_ratio(self) -> None:
        self.assertAlmostEqual(theme.contrast_ratio("#000000", "#ffffff"), 21.0, places=2)


class IconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_every_command_icon_ships_and_renders(self) -> None:
        for name in icons.FILES:
            with self.subTest(icon=name):
                self.assertTrue((theme.ASSETS / "icons" / f"{icons.FILES[name]}.svg").exists())
                pixmap = icons.icon(name).pixmap(theme.ICON_PX)
                self.assertFalse(pixmap.isNull())
                image = pixmap.toImage()
                inked = any(
                    image.pixelColor(x, y).alpha() > 0
                    for x in range(image.width())
                    for y in range(image.height())
                )
                self.assertTrue(inked, f"{name} rendered nothing")

    def test_icons_are_navy_when_enabled_and_grey_when_disabled(self) -> None:
        from PySide6.QtGui import QColor, QIcon

        def dominant(mode: QIcon.Mode) -> str:
            image = icons.icon("save").pixmap(24, mode=mode).toImage()
            for x in range(image.width()):
                for y in range(image.height()):
                    color = image.pixelColor(x, y)
                    if color.alpha() == 255:
                        return color.name()
            return ""

        self.assertEqual(dominant(QIcon.Normal), QColor(theme.NAVY).name())
        self.assertEqual(dominant(QIcon.Disabled), QColor(theme.DISABLED).name())

    def test_window_icon_files_load(self) -> None:
        from PySide6.QtGui import QIcon

        for name in ("squirrel.ico", "squirrel.icns", "squirrel.png"):
            with self.subTest(file=name):
                self.assertFalse(QIcon(str(theme.ASSETS / name)).isNull())


if __name__ == "__main__":
    print(f"{'pair':28} {'fg':8} {'bg':8} {'ratio':>6} {'min':>4}")
    for name, foreground, background, ratio, minimum in contrast_table():
        print(f"{name:28} {foreground:8} {background:8} {ratio:6.2f} {minimum:4.1f}")
    unittest.main()
