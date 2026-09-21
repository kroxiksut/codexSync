"""The frozen builds must be able to state their own version (CS-265).

`version.py` reads the version from installed package metadata so it can never
drift from `pyproject.toml`. PyInstaller does not carry `dist-info` unless a
spec asks for it, so without `copy_metadata` the exe raises
`PackageNotFoundError`, falls back to `0.0.0+unknown`, and stamps that string
into every Guardian snapshot manifest through `PRODUCER_VERSION` -- a snapshot
that cannot say which build wrote it.

The exe itself is not built here (that needs PyInstaller and minutes); what is
pinned is that both specs still ask for the metadata, and that the fallback is
never what a working install reports.
"""
from __future__ import annotations

from pathlib import Path
import unittest

from codexsync.version import PRODUCER_VERSION, __version__

SPECS = ("codexsync.spec", "codexsync-gui.spec")
FALLBACK = "0.0.0+unknown"


class SpecMetadataTests(unittest.TestCase):
    def test_both_specs_copy_the_package_metadata(self) -> None:
        for name in SPECS:
            text = Path(name).read_text(encoding="utf-8")
            with self.subTest(spec=name):
                self.assertIn("copy_metadata", text)
                self.assertIn('copy_metadata("codexsync")', text)

    def test_both_specs_import_the_hook(self) -> None:
        for name in SPECS:
            text = Path(name).read_text(encoding="utf-8")
            with self.subTest(spec=name):
                self.assertIn("from PyInstaller.utils.hooks import", text)


class VersionTests(unittest.TestCase):
    def test_an_installed_package_never_reports_the_fallback(self) -> None:
        self.assertNotEqual(__version__, FALLBACK)

    def test_the_producer_string_carries_that_version(self) -> None:
        # What lands in a Guardian snapshot manifest.
        self.assertEqual(PRODUCER_VERSION, f"codexsync-{__version__}")

    def test_the_version_matches_pyproject(self) -> None:
        text = Path("pyproject.toml").read_text(encoding="utf-8")
        declared = next(
            line.split("=", 1)[1].strip().strip('"')
            for line in text.splitlines()
            if line.startswith("version")
        )
        self.assertEqual(__version__, declared)



class VersionFlagTests(unittest.TestCase):
    """A build has to be able to say what it is, from a terminal.

    Until 0.2 the version appeared only on the window's About screen, which is
    why both exes reported `0.0.0+unknown` without anyone noticing.
    """

    def _version_output(self, argv: list[str]) -> str:
        import contextlib
        import io

        from codexsync.cli import main

        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            main(argv)
        self.assertEqual(caught.exception.code, 0)
        return out.getvalue().strip()

    def test_long_flag(self) -> None:
        self.assertEqual(self._version_output(["--version"]), f"codexsync {__version__}")

    def test_short_flag_is_capital_v(self) -> None:
        # `-v` has meant `--verbose` since 0.1 and must keep meaning it.
        self.assertEqual(self._version_output(["-V"]), f"codexsync {__version__}")


if __name__ == "__main__":
    unittest.main()
