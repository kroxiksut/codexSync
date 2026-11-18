"""The Linux packaging (D-037, CS-421), checked without building anything.

The packages themselves are built and installed by the release workflow on
Linux runners. What is pinned here is what must not drift between releases:
how a version sorts in a package manager, what a package installs and that it
has no install or remove scripts, which tools are fetched and against which
hashes, and that the build spec carries what the frozen programs need.
"""
from __future__ import annotations

import configparser
import importlib.util
import json
from pathlib import Path
import shutil
import unittest
import uuid

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("linux_packages", REPO_ROOT / "scripts" / "linux_packages.py")
assert _spec is not None and _spec.loader is not None
packages = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(packages)


class VersionTests(unittest.TestCase):
    def test_a_prerelease_sorts_before_its_release(self) -> None:
        self.assertEqual(packages.package_version("0.2.0a2"), "0.2.0~a2")
        self.assertEqual(packages.package_version("0.2.0b1"), "0.2.0~b1")
        self.assertEqual(packages.package_version("0.2.0rc1"), "0.2.0~rc1")
        self.assertEqual(packages.package_version("0.2.0.dev3"), "0.2.0~dev3")
        self.assertEqual(packages.package_version("0.2.0"), "0.2.0")

    def test_a_version_it_does_not_know_is_refused_not_guessed(self) -> None:
        for odd in ("0.2.0.post1", "0.2.0+local", "v0.2.0", "0.2.0a"):
            with self.subTest(version=odd), self.assertRaises(ValueError):
                packages.package_version(odd)

    def test_the_declared_version_converts(self) -> None:
        packages.package_version(packages.project_metadata()["version"])


class NamingTests(unittest.TestCase):
    def test_assets_are_named_like_the_windows_zips(self) -> None:
        self.assertEqual(
            packages.asset_name("v0.2.0-alpha.2", "x86_64", "deb"), "codexsync-v0.2.0-alpha.2-linux-amd64.deb"
        )
        self.assertEqual(
            packages.asset_name("v0.2.0", "aarch64", "AppImage"), "codexsync-v0.2.0-linux-arm64.AppImage"
        )

    def test_only_the_architectures_codex_ships_for(self) -> None:
        # D-037: a codexSync where no Codex runs has nothing to synchronise.
        self.assertEqual(set(packages.ARCHES), {"x86_64", "aarch64"})


class PackageContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = packages.nfpm_config(Path("/build/dist/codexsync"), "x86_64", packages.project_metadata())

    def test_no_install_or_remove_script_exists(self) -> None:
        # A package touches nothing in any home: no config, no workspace, no
        # `.codex`, no scheduler task. Without scripts it cannot.
        text = json.dumps(self.config)
        for key in ("scripts", "preinstall", "postinstall", "preremove", "postremove"):
            self.assertNotIn(key, text)

    def test_the_build_goes_under_opt_and_the_commands_onto_path(self) -> None:
        contents = self.config["contents"]
        tree = [item for item in contents if item.get("type") == "tree"]
        self.assertEqual([(item["src"], item["dst"]) for item in tree], [(str(Path("/build/dist/codexsync")), "/opt/codexsync")])
        links = {item["dst"]: item["src"] for item in contents if item.get("type") == "symlink"}
        self.assertEqual(links, {
            "/usr/bin/codexsync": "/opt/codexsync/codexsync",
            "/usr/bin/codexsync-gui": "/opt/codexsync/codexsync-gui",
        })
        destinations = {item["dst"] for item in contents}
        self.assertIn("/usr/share/applications/codexsync.desktop", destinations)
        self.assertIn("/usr/share/icons/hicolor/256x256/apps/codexsync.png", destinations)
        for item in contents:
            with self.subTest(dst=item["dst"]):
                self.assertTrue(
                    item["dst"].startswith(("/opt/codexsync", "/usr/bin/", "/usr/share/")),
                    "nothing outside the package's own places",
                )

    def test_metadata_comes_from_pyproject(self) -> None:
        meta = packages.project_metadata()
        self.assertEqual(self.config["version"], packages.package_version(meta["version"]))
        self.assertEqual(self.config["license"], "GPL-3.0-or-later")
        self.assertEqual(self.config["arch"], "amd64")
        self.assertEqual(
            packages.nfpm_config(Path("/d"), "aarch64", meta)["arch"], "arm64"
        )

    def test_the_maintainer_is_a_name_with_an_address(self) -> None:
        """Debian's `Maintainer` field is `Name <address>`; the address is the owner's choice."""
        meta = packages.project_metadata()
        self.assertRegex(self.config["maintainer"], r"^\S.* <[^<>@\s]+@[^<>@\s]+>$")
        self.assertTrue(self.config["maintainer"].startswith(meta["author"] + " <"))
        self.assertNotIn("@", self.config["vendor"])

    def test_nfpm_is_unpacked_into_a_folder_named_by_its_archive(self) -> None:
        """CS-480: a moved pin must never reuse the binary of the old archive."""
        folder = Path(__file__).resolve().parents[1] / "test-sandbox" / f"nfpm-{uuid.uuid4().hex[:8]}"
        folder.mkdir(parents=True)
        try:
            one, two = folder / "a.tar.gz", folder / "b.tar.gz"
            one.write_bytes(b"one")
            two.write_bytes(b"two")
            self.assertNotEqual(packages.nfpm_folder(folder, one), packages.nfpm_folder(folder, two))
            self.assertEqual(packages.nfpm_folder(folder, one).parent, folder / "nfpm")
        finally:
            shutil.rmtree(folder)

    def test_both_families_declare_the_same_kinds_of_library(self) -> None:
        deb, rpm = self.config["overrides"]["deb"]["depends"], self.config["overrides"]["rpm"]["depends"]
        self.assertEqual(deb[0], "libc6 (>= 2.35)")
        self.assertEqual(rpm[0], "glibc >= 2.35")
        # CS-451: ARM64 is built on Ubuntu 24.04, so its floor is that glibc.
        arm = packages.nfpm_config(Path("/d"), "aarch64", packages.project_metadata())["overrides"]
        self.assertEqual(arm["deb"]["depends"][0], "libc6 (>= 2.39)")
        self.assertEqual(arm["rpm"]["depends"][0], "glibc >= 2.39")
        self.assertEqual(arm["deb"]["depends"][1:], deb[1:])
        # The system half of Qt, named the way each family names it.
        for deb_name, rpm_name in (
            ("libgl1", "mesa-libGL"), ("libegl1", "mesa-libEGL"), ("libfontconfig1", "fontconfig"),
            ("libxkbcommon0", "libxkbcommon"), ("libdbus-1-3", "dbus-libs"),
            ("libxcb-cursor0", "xcb-util-cursor"), ("libwayland-client0", "libwayland-client"),
        ):
            with self.subTest(library=deb_name):
                self.assertIn(deb_name, deb)
                self.assertIn(rpm_name, rpm)


class ToolPinTests(unittest.TestCase):
    def test_every_tool_is_pinned_per_architecture_to_an_https_url_and_a_hash(self) -> None:
        pins = json.loads(packages.TOOLS_FILE.read_text(encoding="utf-8"))
        for name in ("nfpm", "appimagetool", "appimage-runtime"):
            for arch in packages.ARCHES:
                with self.subTest(tool=name, arch=arch):
                    pin = pins[name][arch]
                    self.assertTrue(pin["url"].startswith("https://github.com/"))
                    self.assertIn(pins[name]["version"], pin["url"])
                    self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")


    def test_a_dropped_download_is_tried_again(self) -> None:
        # CS-558: one `IncompleteRead` failed a whole build.
        import http.client
        import io

        answers = [http.client.IncompleteRead(b"par"), ConnectionResetError("reset"), io.BytesIO(b"whole")]
        waits = []

        def opener(url, timeout):
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        self.assertEqual(packages._download("https://x/y", sleep=waits.append, opener=opener), b"whole")
        self.assertEqual(len(waits), 2)

    def test_a_download_that_keeps_failing_stops_the_build(self) -> None:
        def opener(url, timeout):
            raise TimeoutError("slow")

        with self.assertRaises(SystemExit):
            packages._download("https://x/y", sleep=lambda _: None, opener=opener)


class CleanSystemCheckTests(unittest.TestCase):
    """`scripts/linux_package_check.sh`, read: only the Release rehearsal runs it."""

    def setUp(self) -> None:
        self.script = (REPO_ROOT / "scripts" / "linux_package_check.sh").read_text(encoding="utf-8")

    def test_ldd_searches_the_bundle_as_the_bootloader_does(self) -> None:
        # CS-489: `_sqlite3` has no RUNPATH; the libsqlite3 PyInstaller brought
        # is found through LD_LIBRARY_PATH=_internal at run time, and `ldd`
        # without it called a clean Ubuntu "libraries missing".
        body = self.script.split("check_libraries() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('bundle="$1/_internal"', body)
        self.assertIn('LD_LIBRARY_PATH="$bundle" xargs', body)

    def test_every_format_imports_what_a_command_needs(self) -> None:
        # `--version` imports neither sqlite3 nor zstd; `doctor --report` does,
        # and must say it reads `.jsonl.zst` (CS-553).
        cases = self.script.split('case "$kind" in', 1)[1]
        for kind in ("deb", "rpm", "appimage", "tarball"):
            with self.subTest(kind=kind):
                branch = cases.split(f"    {kind})", 1)[1].split(";;", 1)[0]
                self.assertIn("check_report ", branch)
        report = self.script.split("check_report() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('"zstd": true', report)
        self.assertIn('HOME="$smoke"', report, "nothing of the check lands in a real home")

class DocumentationNamesTests(unittest.TestCase):
    def test_the_install_steps_name_the_files_a_release_has(self) -> None:
        # CS-557: the guides said `codexsync-<version>-...`; the files carry the tag.
        for lang, token in (("en", "<tag>"), ("ru", "<метка>"), ("zh", "<标签>")):
            text = (REPO_ROOT / "docs" / lang / "README.md").read_text(encoding="utf-8")
            for extension in ("deb", "rpm", "AppImage", "tar.gz"):
                with self.subTest(lang=lang, extension=extension):
                    self.assertIn(packages.asset_name(token, "x86_64", extension), text)
            self.assertIn(packages.asset_name("v0.2.0", "x86_64", "deb"), text)
            self.assertNotRegex(text, r"codexsync-<(version|версия|版本)>-linux")

class LauncherFileTests(unittest.TestCase):
    def test_the_menu_entry_names_the_window_and_its_icon(self) -> None:
        entry = configparser.ConfigParser(interpolation=None)
        entry.optionxform = str
        entry.read(packages.DESKTOP_FILE, encoding="utf-8")
        section = entry["Desktop Entry"]
        self.assertEqual(section["Type"], "Application")
        self.assertEqual(section["Exec"], "codexsync-gui")
        self.assertEqual(section["Icon"], "codexsync")
        self.assertEqual(section["Terminal"], "false")
        self.assertTrue(section["Categories"].endswith(";"))

    def test_the_window_claims_that_entry(self) -> None:
        # GNOME on Wayland ties a window to `<desktop file name>.desktop`.
        text = (REPO_ROOT / "src" / "codexsync" / "gui" / "window.py").read_text(encoding="utf-8")
        self.assertIn(f'setDesktopFileName("{packages.DESKTOP_FILE.stem}")', text)

    def test_the_appimage_runs_the_program_that_is_window_and_cli(self) -> None:
        text = packages.APPRUN_FILE.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn('exec "$HERE/usr/lib/codexsync/codexsync-gui" "$@"', text)
        self.assertNotIn("\r", text, "a CRLF shebang does not run")

    def test_the_icon_is_a_256_pixel_png(self) -> None:
        data = packages.ICON_FILE.read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        self.assertEqual((width, height), (256, 256))


class LinuxSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (REPO_ROOT / "codexsync-linux.spec").read_text(encoding="utf-8")

    def test_one_folder_two_programs(self) -> None:
        self.assertIn('name="codexsync",', self.text)
        self.assertIn('name="codexsync-gui",', self.text)
        self.assertEqual(self.text.count("exclude_binaries=True"), 2, "both share the folder's libraries")
        self.assertIn("COLLECT(", self.text)

    def test_the_build_states_its_version_and_the_console_carries_no_qt(self) -> None:
        self.assertIn('copy_metadata("codexsync")', self.text)
        console = self.text.split("console = Analysis(", 1)[1].split(")\n", 1)[0]
        self.assertIn('"PySide6"', console)


if __name__ == "__main__":
    unittest.main()
