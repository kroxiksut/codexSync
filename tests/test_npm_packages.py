"""The npm packages (CS-416), checked without publishing anything.

The release workflow stages, packs and installs them on clean runners. Pinned
here: how a version and its tags are spelt, that the launcher names exactly
the packages that are built and none has an install script, that a Linux
folder reaches npm without the symbolic links npm would drop, that an archive
cannot write outside its folder, and -- where node is installed -- that the
launcher passes arguments and exit codes through.
"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import unittest
from unittest import mock
import uuid
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[1]
SANDBOX = REPO_ROOT / "test-sandbox"
_spec = importlib.util.spec_from_file_location("npm_packages", REPO_ROOT / "scripts" / "npm_packages.py")
assert _spec is not None and _spec.loader is not None
npm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(npm)

LAUNCH_JS = REPO_ROOT / "packaging" / "npm" / "lib" / "launch.js"


def _declared_tag() -> str:
    return "v" + npm.npm_version(npm.project_metadata()["version"])


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"npm-{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)


class VersionTests(unittest.TestCase):
    def test_a_version_is_spelt_as_the_tag_is(self) -> None:
        self.assertEqual(npm.npm_version("0.2.0a2"), "0.2.0-alpha.2")
        self.assertEqual(npm.npm_version("0.2.0b1"), "0.2.0-beta.1")
        self.assertEqual(npm.npm_version("0.2.0rc3"), "0.2.0-rc.3")
        self.assertEqual(npm.npm_version("0.2.0"), "0.2.0")

    def test_a_version_npm_cannot_order_is_refused_not_guessed(self) -> None:
        for odd in ("0.2", "0.2.0.dev1", "0.2.0.post1", "0.2.0+local", "v0.2.0", "0.2.0a"):
            with self.subTest(version=odd), self.assertRaises(ValueError):
                npm.npm_version(odd)

    def test_the_declared_version_converts(self) -> None:
        npm.npm_version(npm.project_metadata()["version"])


class TagTests(unittest.TestCase):
    def test_a_platform_package_never_takes_latest(self) -> None:
        self.assertEqual(npm.platform_tag("0.2.0", "linux-x64"), "linux-x64")
        self.assertEqual(npm.platform_tag("0.2.0-alpha.2", "win32-arm64"), "alpha-win32-arm64")

    def test_the_launcher_is_latest_while_no_release_exists(self) -> None:
        self.assertEqual(npm.launcher_tag("0.2.0-alpha.2", None), "latest")
        self.assertEqual(npm.launcher_tag("0.2.0-alpha.2", "0.2.0-alpha.1"), "latest")
        # npm gives the first version ever published `latest`, whatever its tag.
        self.assertEqual(npm.launcher_tag("0.2.0-alpha.2", "0.2.0-alpha.2-win32-x64"), "latest")

    def test_after_a_release_a_pre_release_moves_only_its_label(self) -> None:
        self.assertEqual(npm.launcher_tag("0.3.0-beta.1", "0.2.0"), "beta")
        self.assertEqual(npm.launcher_tag("0.2.1", "0.2.0"), "latest")


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.meta = npm.project_metadata()
        self.launcher = npm.launcher_manifest("0.2.0-alpha.2", self.meta)

    def test_the_launcher_names_every_built_system_under_its_alias(self) -> None:
        self.assertEqual(
            self.launcher["optionalDependencies"],
            {f"codexsync-{key}": f"npm:codexsync@0.2.0-alpha.2-{key}" for key in npm.PLATFORMS},
        )
        for key in npm.PLATFORMS:
            platform = npm.platform_manifest(key, "0.2.0-alpha.2", self.meta)
            self.assertEqual(platform["name"], self.launcher["name"])
            self.assertEqual(platform["version"], f"0.2.0-alpha.2-{key}")
            self.assertEqual(f"{platform['os'][0]}-{platform['cpu'][0]}", key)

    def test_nothing_runs_at_install_time(self) -> None:
        manifests = [self.launcher] + [npm.platform_manifest(key, "0.2.0", self.meta) for key in npm.PLATFORMS]
        for manifest in manifests:
            self.assertNotIn("scripts", manifest)
            self.assertNotIn("dependencies", manifest)
        for key in npm.PLATFORMS:
            self.assertNotIn("bin", npm.platform_manifest(key, "0.2.0", self.meta))

    def test_both_programs_have_a_launcher_file(self) -> None:
        self.assertEqual(set(self.launcher["bin"]), set(npm.PROGRAMS))
        for target in self.launcher["bin"].values():
            script = REPO_ROOT / "packaging" / "npm" / target
            self.assertTrue(script.is_file(), target)
            self.assertTrue(script.read_bytes().startswith(b"#!/usr/bin/env node\n"), target)

    def test_the_repository_is_the_one_trusted_publishing_checks(self) -> None:
        self.assertEqual(self.launcher["repository"]["url"], f"git+{self.meta['repository']}.git")

    def test_the_launcher_knows_the_same_systems(self) -> None:
        text = LAUNCH_JS.read_text(encoding="utf-8")
        listed = re.search(r"const PLATFORMS = \[(?P<items>[^\]]*)\]", text)
        assert listed is not None
        self.assertEqual(re.findall(r'"([^"]+)"', listed.group("items")), list(npm.PLATFORMS))


class ArchiveSafetyTests(unittest.TestCase):
    @staticmethod
    def _archive(*members: tarfile.TarInfo) -> tarfile.TarFile:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for member in members:
                archive.addfile(member, io.BytesIO(b"") if member.isfile() else None)
        buffer.seek(0)
        return tarfile.open(fileobj=buffer)

    @staticmethod
    def _member(name: str, kind: bytes = tarfile.REGTYPE, link: str = "") -> tarfile.TarInfo:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = link
        return info

    def test_a_link_inside_the_folder_is_accepted(self) -> None:
        archive = self._archive(
            self._member("build/_internal/sub/lib.so"),
            self._member("build/_internal/lib.so", tarfile.SYMTYPE, "sub/lib.so"),
            self._member("build/_internal/up.so", tarfile.SYMTYPE, "../codexsync"),
        )
        self.assertEqual(len(npm._safe_members(archive)), 3)

    def test_a_member_leaving_the_folder_is_refused(self) -> None:
        for member in (
            self._member("../evil"),
            self._member("/etc/evil"),
            self._member("build/link", tarfile.SYMTYPE, "/etc/passwd"),
            self._member("build/link", tarfile.SYMTYPE, "../../outside"),
            self._member("build/hard", tarfile.LNKTYPE, "../outside"),
            self._member("build/device", tarfile.CHRTYPE),
        ):
            with self.subTest(name=member.name, link=member.linkname), self.assertRaises(ValueError):
                npm._safe_members(self._archive(member))


class StageTests(_Sandbox):
    def test_the_windows_programs_come_out_of_their_zips_unchanged(self) -> None:
        tag = _declared_tag()
        assets = self.root / "assets"
        assets.mkdir()
        for program in npm.PROGRAMS:
            with zipfile.ZipFile(assets / f"{program}-{tag}-windows-amd64.zip", "w") as bundle:
                bundle.writestr(f"{program}.exe", f"{program} build".encode())
                bundle.writestr("LICENSE", "licence")
        folders = npm.stage(assets, tag, self.root / "out", ["win32-x64"])

        platform, launcher = folders
        for program in npm.PROGRAMS:
            self.assertEqual((platform / "dist" / f"{program}.exe").read_bytes(), f"{program} build".encode())
        self.assertTrue((platform / "LICENSE").is_file())
        manifest = json.loads((platform / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["os"], ["win32"])
        self.assertEqual(launcher.name, "codexsync")
        self.assertTrue((launcher / "lib" / "launch.js").is_file())
        self.assertTrue((launcher / "README.md").is_file())
        self.assertEqual(
            json.loads((launcher / "package.json").read_text(encoding="utf-8"))["version"],
            npm.npm_version(npm.project_metadata()["version"]),
        )

    def test_a_tag_that_is_not_the_declared_version_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            npm.stage(self.root, "v9.9.9", self.root / "out", ["win32-x64"])

    @unittest.skipIf(sys.platform == "win32", "a Linux package is staged where file modes survive")
    def test_a_linux_folder_reaches_npm_with_copies_for_its_links(self) -> None:
        tag = _declared_tag()
        build = self.root / "build" / f"codexsync-{tag}-linux-amd64"
        (build / "_internal" / "PySide6" / "Qt" / "lib").mkdir(parents=True)
        for program in npm.PROGRAMS:
            (build / program).write_text("#!/bin/sh\n", encoding="utf-8")
            (build / program).chmod(0o755)
        real = build / "_internal" / "PySide6" / "Qt" / "lib" / "libQt6Core.so.6"
        real.write_bytes(b"qt core")
        (build / "_internal" / "libQt6Core.so.6").symlink_to("PySide6/Qt/lib/libQt6Core.so.6")
        assets = self.root / "assets"
        assets.mkdir()
        with tarfile.open(assets / f"codexsync-{tag}-linux-amd64.tar.gz", "w:gz") as archive:
            archive.add(build, arcname=build.name)

        platform = npm.stage(assets, tag, self.root / "out", ["linux-x64"])[0]
        dist = platform / "dist"
        self.assertEqual([path for path in dist.rglob("*") if path.is_symlink()], [])
        self.assertEqual((dist / "_internal" / "libQt6Core.so.6").read_bytes(), b"qt core")
        self.assertEqual((dist / "_internal" / "PySide6" / "Qt" / "lib" / "libQt6Core.so.6").read_bytes(), b"qt core")
        for program in npm.PROGRAMS:
            self.assertTrue((dist / program).stat().st_mode & 0o100, program)


class PublishTests(_Sandbox):
    def test_platforms_go_first_and_a_published_version_is_skipped(self) -> None:
        version = "0.2.0-alpha.2"
        for key in npm.PLATFORMS:
            (self.root / f"codexsync-{version}-{key}.tgz").write_bytes(b"")
        (self.root / f"codexsync-{version}.tgz").write_bytes(b"")
        commands: list[list[str]] = []

        def run(argv, **_kwargs):
            commands.append([Path(item).name if item.endswith(".tgz") else item for item in argv[1:]])
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(npm, "_npm", return_value="npm"), \
                mock.patch.object(npm, "_published", side_effect=lambda spec: spec.endswith("-win32-x64")), \
                mock.patch.object(npm, "_latest", return_value=f"{version}-win32-x64"), \
                mock.patch.object(npm.subprocess, "run", side_effect=run), \
                mock.patch("builtins.print"):
            npm.publish(self.root, "v0.2.0-alpha.2")

        self.assertEqual(commands, [
            ["publish", f"codexsync-{version}-win32-arm64.tgz", "--tag", "alpha-win32-arm64", "--access", "public"],
            ["publish", f"codexsync-{version}-linux-x64.tgz", "--tag", "alpha-linux-x64", "--access", "public"],
            ["publish", f"codexsync-{version}-linux-arm64.tgz", "--tag", "alpha-linux-arm64", "--access", "public"],
            ["publish", f"codexsync-{version}.tgz", "--tag", "latest", "--access", "public"],
        ])

    def test_nothing_is_published_while_a_package_is_missing(self) -> None:
        (self.root / "codexsync-0.2.0-alpha.2.tgz").write_bytes(b"")
        with mock.patch.object(npm.subprocess, "run") as run, self.assertRaises(SystemExit):
            npm.publish(self.root, "v0.2.0-alpha.2")
        run.assert_not_called()


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class LauncherTests(_Sandbox):
    """The launcher, run by node against a stand-in for the platform package."""

    def setUp(self) -> None:
        super().setUp()
        self.modules = self.root / "node_modules"
        shutil.copytree(REPO_ROOT / "packaging" / "npm", self.modules / "codexsync")
        self.key = subprocess.run(
            ["node", "-p", "process.platform + '-' + process.arch"], capture_output=True, text=True, check=True,
        ).stdout.strip()

    def _platform_package(self) -> Path:
        dist = self.modules / f"codexsync-{self.key}" / "dist"
        dist.mkdir(parents=True)
        (dist.parent / "package.json").write_text('{"name": "codexsync"}', encoding="utf-8")
        return dist

    def _launch(self, *args: str) -> subprocess.CompletedProcess:
        script = self.modules / "codexsync" / "bin" / "codexsync.js"
        return subprocess.run(["node", str(script), *args], capture_output=True, text=True, timeout=60)

    def test_without_the_platform_package_it_says_so_and_exits_4(self) -> None:
        result = self._launch("--version")
        self.assertEqual(result.returncode, 4)
        self.assertIn("codexsync:", result.stderr)

    def test_arguments_and_the_exit_code_pass_through(self) -> None:
        if self.key not in npm.PLATFORMS:
            self.skipTest(f"no build is published for {self.key}")
        dist = self._platform_package()
        if sys.platform == "win32":
            # A stand-in program that exits with the code it is given.
            shutil.copy2(Path(shutil.which("cmd") or r"C:\Windows\System32\cmd.exe"), dist / "codexsync.exe")
            result = self._launch("/c", "exit 7")
        else:
            program = dist / "codexsync"
            program.write_text('#!/bin/sh\nexit "$1"\n', encoding="utf-8")
            program.chmod(0o755)
            result = self._launch("7")
        self.assertEqual(result.returncode, 7, result.stderr)


if __name__ == "__main__":
    unittest.main()
