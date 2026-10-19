"""Pack the Linux build four ways: tar.gz, AppImage, .deb and .rpm (D-037, CS-421).

    python scripts/linux_packages.py --tag v0.2.0-alpha.2 --dist dist/codexsync --out release

`--dist` is the folder `codexsync-linux.spec` builds: both programs and their
shared `_internal`. Every format carries exactly that folder; nothing is built
twice, so the four cannot disagree about what codexSync is.

What a package does, and what it never does:

* `.deb`/`.rpm` install the folder under `/opt/codexsync`, link `codexsync`
  and `codexsync-gui` into `/usr/bin`, and add a menu entry and an icon. They
  have no install or remove scripts at all: nothing in any home directory --
  no config, no workspace, no `.codex`, no scheduler task -- is created or
  removed by a package. Removing codexSync's own tasks is the person's step
  (`codexsync automation remove`), documented before removal.
* The AppImage is one file to keep anywhere; its runtime is the static type 2
  runtime (no `libfuse2` needed), and scheduler tasks name `$APPIMAGE`, the
  file kept, never the mount it runs from (CS-420).
* The `tar.gz` is the folder with the menu entry and icon beside it: no root,
  no FUSE.

The packaging tools are fetched at the versions `packaging/linux/tools.json`
pins, and a file whose SHA-256 differs is refused before it runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGING = REPO_ROOT / "packaging" / "linux"
TOOLS_FILE = PACKAGING / "tools.json"
DESKTOP_FILE = REPO_ROOT / "src" / "codexsync" / "desktop" / "codexsync.desktop"
ICON_FILE = DESKTOP_FILE.with_name("codexsync.png")
APPRUN_FILE = PACKAGING / "AppRun"
LICENSE_FILE = REPO_ROOT / "LICENSE"

#: Where a package puts the build. One folder, so an update replaces it whole
#: and a scheduler task naming /opt/codexsync/codexsync-gui survives updates.
INSTALL_DIR = "/opt/codexsync"
PROGRAMS = ("codexsync", "codexsync-gui")

#: The machine's name for its architecture -> the name a release asset carries
#: (as the Windows zips do) and the one nfpm takes (Go's).
ARCHES = {
    "x86_64": {"label": "amd64", "goarch": "amd64"},
    "aarch64": {"label": "arm64", "goarch": "arm64"},
}

#: What the frozen build does not carry: the C library, and the system half of
#: Qt -- OpenGL/EGL, fonts, input, D-Bus and the X11/Wayland client libraries
#: the platform plugins load. Names differ between the two families; the
#: release workflow proves each list on a clean system with `ldd`.
DEB_DEPENDS = (
    "libc6 (>= 2.35)",
    "libgl1",
    "libegl1",
    "libfontconfig1",
    "libfreetype6",
    "libxkbcommon0",
    "libxkbcommon-x11-0",
    "libdbus-1-3",
    "libx11-6",
    "libx11-xcb1",
    "libxcb1",
    "libxcb-cursor0",
    "libxcb-icccm4",
    "libxcb-image0",
    "libxcb-keysyms1",
    "libxcb-randr0",
    "libxcb-render-util0",
    "libxcb-shape0",
    "libxcb-xfixes0",
    "libxcb-xkb1",
    "libwayland-client0",
    "libwayland-cursor0",
    "libwayland-egl1",
)
RPM_DEPENDS = (
    "glibc >= 2.35",
    "mesa-libGL",
    "mesa-libEGL",
    "fontconfig",
    "freetype",
    "libxkbcommon",
    "libxkbcommon-x11",
    "dbus-libs",
    "libX11",
    "libX11-xcb",
    "libxcb",
    "xcb-util-cursor",
    "xcb-util-wm",
    "xcb-util-image",
    "xcb-util-keysyms",
    "xcb-util-renderutil",
    "libwayland-client",
    "libwayland-cursor",
    "libwayland-egl",
)

_PRE = re.compile(r"^(?P<base>\d+(?:\.\d+)*)(?:(?P<kind>a|b|rc|\.dev)(?P<num>\d+))?$")


def package_version(version: str) -> str:
    """A PEP 440 version as .deb and .rpm order it: `0.2.0a2` -> `0.2.0~a2`.

    `~` sorts before everything, so `0.2.0~a2 < 0.2.0~rc1 < 0.2.0`, as in
    Python. Anything this does not recognise is refused rather than guessed:
    a version that sorts wrongly makes the package manager refuse an update.
    """
    match = _PRE.match(version)
    if match is None:
        raise ValueError(f"cannot turn {version!r} into a package version")
    base, kind, number = match.group("base"), match.group("kind"), match.group("num")
    if kind is None:
        return base
    return f"{base}~{kind.lstrip('.')}{number}"


def asset_name(tag: str, arch: str, extension: str) -> str:
    """`codexsync-v0.2.0-alpha.2-linux-amd64.deb`, like the Windows zips."""
    return f"codexsync-{tag}-linux-{ARCHES[arch]['label']}.{extension}"


#: The contact Debian's policy asks for in `Maintainer`. The owner chose to
#: publish this address in the packages (2026-10-19); it is not in
#: `pyproject.toml`, so PyPI and the Windows programs do not carry it.
MAINTAINER_EMAIL = "fmalkov91@gmail.com"


def project_metadata() -> dict:
    with open(REPO_ROOT / "pyproject.toml", "rb") as handle:
        project = tomllib.load(handle)["project"]
    return {
        "version": project["version"],
        "description": project["description"],
        "author": project["authors"][0]["name"],
        "homepage": project["urls"]["Homepage"],
        "license": project["license"],
    }


def nfpm_config(dist: Path, arch: str, meta: dict) -> dict:
    """The nfpm configuration, as data (JSON is YAML, so nfpm reads it as is)."""
    contents = [
        {"src": str(dist), "dst": INSTALL_DIR, "type": "tree"},
        *({"src": f"{INSTALL_DIR}/{name}", "dst": f"/usr/bin/{name}", "type": "symlink"} for name in PROGRAMS),
        {"src": str(DESKTOP_FILE), "dst": "/usr/share/applications/codexsync.desktop",
         "file_info": {"mode": 0o644}},
        {"src": str(ICON_FILE), "dst": "/usr/share/icons/hicolor/256x256/apps/codexsync.png",
         "file_info": {"mode": 0o644}},
        {"src": str(LICENSE_FILE), "dst": "/usr/share/doc/codexsync/copyright",
         "packager": "deb", "file_info": {"mode": 0o644}},
        {"src": str(LICENSE_FILE), "dst": "/usr/share/licenses/codexsync/LICENSE",
         "packager": "rpm", "type": "license", "file_info": {"mode": 0o644}},
    ]
    return {
        "name": "codexsync",
        "arch": ARCHES[arch]["goarch"],
        "platform": "linux",
        "version": package_version(meta["version"]),
        "version_schema": "none",
        "release": "1",
        "section": "utils",
        "priority": "optional",
        "maintainer": f"{meta['author']} <{MAINTAINER_EMAIL}>",
        "vendor": meta["author"],
        "description": meta["description"],
        "homepage": meta["homepage"],
        "license": meta["license"],
        "contents": contents,
        # No scripts on purpose: a package touches nothing outside its own files.
        "overrides": {
            "deb": {"depends": list(DEB_DEPENDS)},
            "rpm": {"depends": list(RPM_DEPENDS)},
        },
    }


def fetch_tool(name: str, arch: str, folder: Path) -> Path:
    """Download one pinned tool, refuse it unless its SHA-256 is the pinned one."""
    pin = json.loads(TOOLS_FILE.read_text(encoding="utf-8"))[name][arch]
    target = folder / pin["url"].rsplit("/", 1)[1]
    if not target.exists():
        folder.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(pin["url"], timeout=120) as response:  # noqa: S310 - pinned https URL
            target.write_bytes(response.read())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != pin["sha256"]:
        target.unlink()
        raise SystemExit(f"{target.name}: SHA-256 {digest} is not the pinned {pin['sha256']}")
    target.chmod(target.stat().st_mode | stat.S_IXUSR)
    return target


def write_checksum(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    Path(f"{path}.sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")


def build_tarball(dist: Path, out: Path, tag: str, arch: str) -> Path:
    stem = asset_name(tag, arch, "tar.gz")[: -len(".tar.gz")]
    target = out / f"{stem}.tar.gz"

    def owned_by_root(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        return info

    with tarfile.open(target, "w:gz") as archive:
        archive.add(dist, arcname=stem, filter=owned_by_root)
        # The menu entry is not copied beside the programs: `codexsync
        # shortcut install` writes it, with this folder's path in it (CS-399).
        archive.add(LICENSE_FILE, arcname=f"{stem}/{LICENSE_FILE.name}", filter=owned_by_root)
    return target


def build_appimage(dist: Path, out: Path, tag: str, arch: str, tools: Path) -> Path:
    appimagetool = fetch_tool("appimagetool", arch, tools)
    runtime = fetch_tool("appimage-runtime", arch, tools)
    target = out / asset_name(tag, arch, "AppImage")
    with tempfile.TemporaryDirectory() as work:
        appdir = Path(work) / "codexsync.AppDir"
        shutil.copytree(dist, appdir / "usr" / "lib" / "codexsync", symlinks=True)
        shutil.copy2(APPRUN_FILE, appdir / "AppRun")
        (appdir / "AppRun").chmod(0o755)
        shutil.copy2(DESKTOP_FILE, appdir / "codexsync.desktop")
        shutil.copy2(ICON_FILE, appdir / "codexsync.png")
        (appdir / ".DirIcon").symlink_to("codexsync.png")
        applications = appdir / "usr" / "share" / "applications"
        icons = appdir / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps"
        applications.mkdir(parents=True)
        icons.mkdir(parents=True)
        shutil.copy2(DESKTOP_FILE, applications / "codexsync.desktop")
        shutil.copy2(ICON_FILE, icons / "codexsync.png")
        # The tool is itself an AppImage: extracting it needs no FUSE on the
        # build machine. The runtime is passed in so nothing is downloaded at
        # build time that the pins did not name.
        env = {**os.environ, "ARCH": arch, "APPIMAGE_EXTRACT_AND_RUN": "1"}
        subprocess.run(
            [str(appimagetool), "--no-appstream", "--runtime-file", str(runtime), str(appdir), str(target)],
            check=True, env=env,
        )
    target.chmod(0o755)
    return target


def build_packages(
    dist: Path, out: Path, tag: str, arch: str, tools: Path, meta: dict, packagers: tuple[str, ...] = ("deb", "rpm"),
) -> list[Path]:
    archive = fetch_tool("nfpm", arch, tools)
    nfpm_dir = tools / "nfpm"
    if not (nfpm_dir / "nfpm").exists():
        nfpm_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as bundle:
            bundle.extract("nfpm", nfpm_dir)
    nfpm = nfpm_dir / "nfpm"
    built = []
    with tempfile.TemporaryDirectory() as work:
        config = Path(work) / "nfpm.yaml"
        config.write_text(json.dumps(nfpm_config(dist, arch, meta), indent=2), encoding="utf-8")
        for packager in packagers:
            target = out / asset_name(tag, arch, packager)
            subprocess.run(
                [str(nfpm), "package", "--config", str(config), "--packager", packager, "--target", str(target)],
                check=True,
            )
            built.append(target)
    return built


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--tag", required=True, help="the release tag, as in the asset names")
    parser.add_argument("--dist", type=Path, default=REPO_ROOT / "dist" / "codexsync")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "release")
    parser.add_argument("--tools", type=Path, default=REPO_ROOT / "build" / "linux-tools")
    parser.add_argument("--formats", default="tar.gz,AppImage,deb,rpm")
    args = parser.parse_args(argv)

    if not sys.platform.startswith("linux"):
        parser.error("Linux packages are built on Linux")
    arch = platform.machine()
    if arch not in ARCHES:
        parser.error(f"no Codex build exists for {arch}; codexSync is built only where Codex runs")
    dist = args.dist.resolve()
    for name in PROGRAMS:
        if not os.access(dist / name, os.X_OK):
            parser.error(f"{dist / name} is not an executable build (run PyInstaller with codexsync-linux.spec)")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    tools = args.tools.resolve()
    meta = project_metadata()
    formats = set(args.formats.split(","))

    built: list[Path] = []
    if "tar.gz" in formats:
        built.append(build_tarball(dist, out, args.tag, arch))
    if "AppImage" in formats:
        built.append(build_appimage(dist, out, args.tag, arch, tools))
    packagers = tuple(name for name in ("deb", "rpm") if name in formats)
    if packagers:
        built.extend(build_packages(dist, out, args.tag, arch, tools, meta, packagers))
    for path in built:
        write_checksum(path)
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
