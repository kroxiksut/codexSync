"""The window's tokens against the design they were transcribed from.

`assets/design/design-tokens.json` is the design source and does not ship, so
`theme.py` carries a copy. A copy drifts; this compares the two whenever the
design file is present and skips when it is not (an sdist, a trimmed checkout).
"""
from __future__ import annotations

import json
from pathlib import Path
import unittest

from codexsync.gui import theme

ROOT = Path(__file__).resolve().parents[1]
TOKENS = ROOT / "assets" / "design" / "design-tokens.json"
RESOURCES = ROOT / "src" / "codexsync" / "gui" / "resources"


@unittest.skipUnless(TOKENS.is_file(), "the design source is not in this checkout")
class DesignTokenParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tokens = json.loads(TOKENS.read_text(encoding="utf-8"))

    def test_colours_match_the_design(self) -> None:
        colours = self.tokens["colours"]
        expected = {
            "accent": theme.ACCENT,
            "accentSoft": theme.ACCENT_SOFT,
            "ok": theme.OK,
            "okSoft": theme.OK_SOFT,
            "attention": theme.ATTENTION,
            "attentionSoft": theme.ATTENTION_SOFT,
            "danger": theme.DANGER,
            "dangerSoft": theme.DANGER_SOFT,
            "darkOk": theme.DARK_OK,
            "darkAttention": theme.DARK_ATTENTION,
            "darkDanger": theme.DARK_DANGER,
            "darkAccentText": theme.DARK_ACCENT_TEXT,
            "darkSelection": theme.DARK_SELECTION,
        }
        for prefix, palette in (("light", theme.LIGHT), ("dark", theme.DARK)):
            expected[f"{prefix}Background"] = palette.background
            expected[f"{prefix}Surface"] = palette.surface
            expected[f"{prefix}Text"] = palette.text
            expected[f"{prefix}MutedText"] = palette.muted_text
            expected[f"{prefix}Line"] = palette.line
        self.assertEqual(sorted(colours), sorted(expected), "a token was added or removed")
        for name, value in expected.items():
            with self.subTest(token=name):
                self.assertEqual(value.upper(), colours[name].upper())

    def test_layout_matches_the_design(self) -> None:
        layout = self.tokens["layout"]
        self.assertEqual(theme.SIDEBAR_WIDTH, layout["sidebarWidth"])
        self.assertEqual(theme.WINDOW_CORNER_RADIUS, layout["windowCornerRadius"])
        self.assertEqual(theme.SURFACE_CORNER_RADIUS, layout["surfaceCornerRadius"])
        self.assertEqual(theme.CONTROL_CORNER_RADIUS, layout["controlCornerRadius"])
        self.assertEqual(theme.CONTENT_PADDING, layout["contentPadding"])
        self.assertEqual(theme.GRID_GAP, layout["gridGap"])

    def test_typography_matches_the_design(self) -> None:
        self.assertEqual(theme.FONT_FAMILY, self.tokens["typography"]["family"])


def _luminance(colour: str) -> float:
    channels = [int(colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class ContrastTests(unittest.TestCase):
    """A status is read from its colour; a colour that cannot be read hides it."""

    def test_status_and_accent_text_meet_wcag_aa_on_both_palettes(self) -> None:
        for palette in (theme.LIGHT, theme.DARK):
            accent = theme.ACCENT if not palette.dark else theme.DARK_ACCENT_TEXT
            for name, colour in [(tone, theme.tone_colour(tone, palette)) for tone in ("ok", "attention", "danger")] + [("accent", accent), ("text", palette.text), ("muted", palette.muted_text)]:
                for ground in (palette.surface, palette.background):
                    with self.subTest(dark=palette.dark, colour=name, ground=ground):
                        self.assertGreaterEqual(_contrast(colour, ground), 4.5)

    def test_banner_headlines_are_readable_on_their_tint(self) -> None:
        for tone in ("ok", "attention", "danger"):
            foreground, soft = theme.TONES[tone]
            with self.subTest(tone=tone):
                self.assertGreaterEqual(_contrast(foreground, soft), 4.5)
                self.assertGreaterEqual(_contrast(theme.LIGHT.text, soft), 4.5)


class ThemeTests(unittest.TestCase):
    def test_every_core_status_has_a_tone(self) -> None:
        for status in ("PASS", "WARN", "FAIL"):
            with self.subTest(status=status):
                self.assertIn(theme.STATUS_TONES[status], theme.TONES)

    def test_both_palettes_build_a_stylesheet(self) -> None:
        for palette in (theme.LIGHT, theme.DARK):
            sheet = theme.stylesheet(palette)
            self.assertIn(palette.background, sheet)
            self.assertEqual(sheet.count("{"), sheet.count("}"))
            for tone in theme.TONES:
                banner = theme.banner_style(tone, palette)
                self.assertEqual(banner.count("{"), banner.count("}"))

    def test_the_packaged_brand_resources_exist(self) -> None:
        """They ship as package data; a window without them draws no mark."""
        self.assertTrue((RESOURCES / "codexsync.ico").is_file())
        self.assertTrue((RESOURCES / "brand-mark.png").is_file())
        self.assertTrue((RESOURCES / "brand-mark-dark.png").is_file())
        for name in ("light", "dark"):
            self.assertTrue((RESOURCES / f"chevron-down-{name}.svg").is_file())
            self.assertIn(f"chevron-down-{name}.svg", theme.stylesheet(
                theme.DARK if name == "dark" else theme.LIGHT, RESOURCES
            ))


if __name__ == "__main__":
    unittest.main()
