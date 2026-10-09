"""Draw the repository's Social Preview card: assets/brand/social-preview.png.

GitHub shows this 1280x640 image wherever a link to the repository is shared.
It is made from the brand mark and the English overview screenshot, which
`scripts/docs_screenshots.py` renders from invented demo data -- never from a
real config -- so the card publishes nothing about anyone's machine.

There is no API for the image: upload it by hand under the repository's
Settings -> General -> Social preview.

    python scripts/social_preview.py
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    # Without it every glyph renders as a box offscreen.
    os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPainterPath, QPen  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codexsync.gui import theme  # noqa: E402

WIDTH, HEIGHT = 1280, 640
OUTPUT = ROOT / "assets" / "brand" / "social-preview.png"
TITLE = "Safe Codex handoff\nbetween your machines"
SUBTITLE = "Chats · Projects · State · Backup · Recovery"


def draw() -> QImage:
    image = QImage(WIDTH, HEIGHT, QImage.Format.Format_ARGB32)
    image.fill(QColor(theme.LIGHT.background))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

    # Left: the mark, the name, the promise.
    mark = QImage(str(ROOT / "assets" / "brand" / "brand-mark.png"))
    painter.drawImage(QRectF(72, 96, 112, 112), mark)
    painter.setPen(QColor(theme.LIGHT.text))
    painter.setFont(QFont("Segoe UI", 44, QFont.Weight.Bold))
    painter.drawText(QRectF(72, 222, 520, 70), Qt.AlignmentFlag.AlignLeft, "codexSync")
    painter.setFont(QFont("Segoe UI", 30, QFont.Weight.DemiBold))
    painter.drawText(QRectF(72, 304, 520, 120), Qt.AlignmentFlag.AlignLeft, TITLE)
    painter.setPen(QColor(theme.ACCENT))
    painter.setFont(QFont("Segoe UI", 17))
    painter.drawText(QRectF(72, 450, 520, 40), Qt.AlignmentFlag.AlignLeft, SUBTITLE)
    painter.setPen(QColor(theme.LIGHT.muted_text))
    painter.setFont(QFont("Segoe UI", 15))
    painter.drawText(
        QRectF(72, 500, 520, 60), Qt.AlignmentFlag.AlignLeft,
        "Local-first GUI and CLI\nWindows · macOS · Linux (experimental)",
    )

    # Right: the window, cropped to its left part and framed like a card.
    shot = QImage(str(ROOT / "docs" / "screenshots" / "en" / "02-overview.png"))
    frame = QRectF(640, 72, 600, 496)
    source = QRectF(0, 0, shot.width() * 0.62, shot.width() * 0.62 * frame.height() / frame.width())
    clip = QPainterPath()
    clip.addRoundedRect(frame, 14, 14)
    painter.save()
    painter.setClipPath(clip)
    painter.drawImage(frame, shot, source)
    painter.restore()
    painter.setPen(QPen(QColor(theme.LIGHT.line), 2))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(frame, 14, 14)
    painter.end()
    return image


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    image = draw()
    if not image.save(str(OUTPUT)):
        print(f"could not write {OUTPUT}", file=sys.stderr)
        return 1
    print(f"written: {OUTPUT} ({image.width()}x{image.height()})")
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
