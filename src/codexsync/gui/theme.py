"""Design tokens and the stylesheet built from them, with no Qt in sight.

The values are transcribed from ``assets/design/design-tokens.json``, which is
the design source and does not ship; ``tests/test_gui_theme.py`` compares the
two whenever that file is present, so a token changed in the design and not
here fails instead of drifting.

Only colours and geometry live here. Whether something is green is decided by
the core's report, never by this module: a colour is how a status is drawn, not
what it is.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Shared by both palettes: the accent and the three status colours.
ACCENT = "#1D5FD0"
ACCENT_SOFT = "#E7F0FF"
OK = "#137A4C"
OK_SOFT = "#E5F6ED"
ATTENTION = "#985B00"
ATTENTION_SOFT = "#FFF4DE"
DANGER = "#B3261E"
DANGER_SOFT = "#FBE9E7"
#: The status colours are tuned for a white surface; these are the same tones
#: lifted for the dark one. `tests/test_gui_theme.py` holds every status colour
#: to WCAG AA contrast (4.5:1) on its palette's surface and background.
DARK_OK = "#4CC38A"
DARK_ATTENTION = "#E0A43A"
DARK_DANGER = "#F2837A"
DARK_ACCENT_TEXT = "#8DB4FF"
DARK_SELECTION = "#243B63"

SIDEBAR_WIDTH = 220
WINDOW_CORNER_RADIUS = 14
SURFACE_CORNER_RADIUS = 9
CONTROL_CORNER_RADIUS = 7
CONTENT_PADDING = 34
GRID_GAP = 18

FONT_FAMILY = "Inter, Segoe UI, system-ui, sans-serif"


@dataclass(frozen=True, slots=True)
class Palette:
    background: str
    surface: str
    text: str
    muted_text: str
    line: str
    dark: bool


LIGHT = Palette(
    background="#F4F6FA",
    surface="#FFFFFF",
    text="#1B2430",
    muted_text="#667085",
    line="#DFE4EC",
    dark=False,
)

DARK = Palette(
    background="#171A20",
    surface="#222730",
    text="#EDF1F7",
    muted_text="#AEB8C7",
    line="#3B4350",
    dark=True,
)

#: Foreground and soft background for each kind of banner and status. The soft
#: tints are light-palette colours; on a dark surface they would glare, so the
#: dark palette draws the same tone as a thin border on the surface instead.
TONES = {
    "ok": (OK, OK_SOFT),
    "attention": (ATTENTION, ATTENTION_SOFT),
    "danger": (DANGER, DANGER_SOFT),
    "neutral": (None, None),
}

#: How the core's check status maps onto a tone.
STATUS_TONES = {"PASS": "ok", "WARN": "attention", "FAIL": "danger"}


def tone_colour(tone: str, palette: Palette) -> str:
    foreground, _ = TONES.get(tone, (None, None))
    if foreground is None:
        return palette.text
    if palette.dark:
        return {"ok": DARK_OK, "attention": DARK_ATTENTION, "danger": DARK_DANGER}[tone]
    return foreground


def banner_style(tone: str, palette: Palette) -> str:
    """Stylesheet for a banner frame and the headline inside it."""
    foreground = tone_colour(tone, palette)
    _, soft = TONES.get(tone, (None, None))
    if soft is None:
        background, border = palette.surface, palette.line
    elif palette.dark:
        background, border = palette.surface, foreground
    else:
        background, border = soft, soft
    return (
        f"#banner {{ background: {background}; border: 1px solid {border};"
        f" border-radius: {SURFACE_CORNER_RADIUS}px; }}"
        f" #bannerTitle, #bannerDot {{ color: {foreground}; }}"
    )


def stylesheet(palette: Palette, resources: Path | None = None) -> str:
    """The application-wide stylesheet for one palette.

    ``resources`` is where the drop-down chevron lives. A styled combo box
    loses its native arrow, so without the file it simply draws none.
    """
    p = palette
    selection = ACCENT_SOFT if not p.dark else DARK_SELECTION
    accent_text = ACCENT if not p.dark else DARK_ACCENT_TEXT
    danger = tone_colour("danger", p)
    chevron = ""
    if resources is not None:
        image = (resources / f"chevron-down-{'dark' if p.dark else 'light'}.svg").as_posix()
        chevron = f"QComboBox::down-arrow {{ image: url({image}); width: 12px; height: 12px; }}"
    return f"""
QWidget {{
    color: {p.text};
    font-family: {FONT_FAMILY};
    font-size: 10.5pt;
}}
QMainWindow, #content {{ background: {p.background}; }}
#sidebar {{ background: {p.surface}; border-right: 1px solid {p.line}; }}
#brandName {{ font-size: 14pt; font-weight: 500; }}
#nav {{ background: transparent; border: none; outline: 0; }}
#nav::item {{
    color: {p.muted_text};
    padding: 9px 12px;
    margin: 2px 0;
    border-radius: {CONTROL_CORNER_RADIUS}px;
}}
#nav::item:hover {{ background: {p.background}; }}
#nav::item:selected {{
    color: {accent_text};
    background: {selection};
    font-weight: 500;
}}
#pageTitle {{ font-size: 18pt; font-weight: 500; }}
#muted, #pageSubtitle, #cardSummary {{ color: {p.muted_text}; }}
#card {{
    background: {p.surface};
    border: 1px solid {p.line};
    border-radius: {SURFACE_CORNER_RADIUS}px;
}}
#cardTitle {{ font-size: 12pt; font-weight: 500; }}
#bannerTitle {{ font-size: 11.5pt; font-weight: 500; }}
QTableWidget, QTreeWidget {{
    background: {p.surface};
    alternate-background-color: {p.background};
    border: none;
    gridline-color: {p.line};
    selection-background-color: {selection};
    selection-color: {p.text};
}}
QTableWidget::item {{ border-bottom: 1px solid {p.line}; padding: 5px 4px; }}
QTreeWidget::item {{ padding: 3px 2px; }}
QTableWidget::item:selected, QTreeWidget::item:selected {{ background: {selection}; color: {p.text}; }}
QHeaderView {{ background: {p.surface}; border: none; }}
QHeaderView::section {{
    background: {p.surface};
    color: {p.muted_text};
    border: none;
    border-bottom: 1px solid {p.line};
    padding: 6px 6px;
}}
QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox {{
    background: {p.surface};
    border: 1px solid {p.line};
    border-radius: {CONTROL_CORNER_RADIUS}px;
    padding: 6px 10px;
    selection-background-color: {ACCENT};
    selection-color: #FFFFFF;
}}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled, QPlainTextEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
    color: {p.muted_text};
    background: {p.background};
}}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{
    background: {p.surface};
    color: {p.muted_text};
    border: 1px solid {p.line};
    border-radius: {CONTROL_CORNER_RADIUS}px;
    padding: 7px 12px;
    margin: 2px 6px 6px 0;
}}
QTabBar::tab:selected {{ background: {selection}; color: {accent_text}; border-color: {ACCENT}; font-weight: 500; }}
QTabBar::tab:hover {{ border-color: {ACCENT}; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{ background: {p.line}; border-radius: 3px; min-height: 28px; min-width: 28px; }}
QScrollBar::handle:hover {{ background: {p.muted_text}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QToolTip {{ color: {p.text}; background: {p.surface}; border: 1px solid {p.line}; padding: 4px; }}
QPushButton#danger {{ color: {danger}; border-color: {danger}; }}
QPushButton {{
    background: {p.surface};
    border: 1px solid {p.line};
    border-radius: {CONTROL_CORNER_RADIUS}px;
    padding: 9px 20px;
}}
QPushButton#compact {{ padding: 9px 0; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: {p.muted_text}; }}
QPushButton#primary {{
    background: {ACCENT};
    border-color: {ACCENT};
    color: #FFFFFF;
    font-weight: 500;
}}
QPushButton#primary:disabled {{ background: {p.line}; border-color: {p.line}; color: {p.muted_text}; }}
QComboBox {{
    background: {p.surface};
    border: 1px solid {p.line};
    border-radius: {CONTROL_CORNER_RADIUS}px;
    padding: 7px 12px;
    min-width: 220px;
}}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox QAbstractItemView {{
    background: {p.surface};
    border: 1px solid {p.line};
    selection-background-color: {selection};
    selection-color: {p.text};
}}
#command {{
    font-family: Consolas, "Cascadia Mono", monospace;
    background: {p.background};
    border: 1px solid {p.line};
    border-radius: {CONTROL_CORNER_RADIUS}px;
    padding: 8px 12px;
}}
{chevron}
QStatusBar {{ background: {p.surface}; color: {p.muted_text}; border-top: 1px solid {p.line}; }}
"""


__all__ = [
    "ACCENT",
    "DARK",
    "LIGHT",
    "Palette",
    "SIDEBAR_WIDTH",
    "STATUS_TONES",
    "banner_style",
    "stylesheet",
    "tone_colour",
]
