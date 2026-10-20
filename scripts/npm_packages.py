"""Pack the release builds as npm packages, and publish them (CS-416).

    python scripts/npm_packages.py stage --tag v0.2.0-alpha.2 --assets release --out npm
    python scripts/npm_packages.py publish --tag v0.2.0-alpha.2 --dir npm

Laid out like Codex's own npm package. `codexsync` is a launcher
(`packaging/npm`) whose `optionalDependencies` name one package per system,
each a version of the same name -- `codexsync-linux-x64` is an alias of
`codexsync@0.2.0-alpha.2-linux-x64` -- and npm installs only the one whose
`os` and `cpu` match. A platform package carries the release build exactly as
the release ships it: the two Windows programs from their zips, the Linux
folder from its `tar.gz`. Nothing is rebuilt, and no package has an install
script: nothing runs on the user's machine until they run codexSync.

One difference from the `tar.gz`: npm does not pack symbolic links, and the
Linux folder holds a few (PyInstaller links Qt's libraries into `_internal`).
Each is replaced by a copy of what it points at, so every path PyInstaller
laid out still exists.

`publish` puts the platform packages first and the launcher last, so the
launcher never names a version that is not there, and skips a version npm
already holds, so a run that failed half-way can simply be repeated. Each
platform package gets a tag of its own (`linux-x64`, `alpha-linux-x64`, as
Codex does), never `latest`; the launcher gets `latest` when it is a release
or while no release exists yet, and its pre-release label otherwise.

Standard library only, like the package.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "packaging" / "npm"
LICENSE_FILE = REPO_ROOT / "LICENSE"
NAME = "codexsync"
PROGRAMS = ("codexsync", "codexsync-gui")

#: npm's name for each system -> what the release asset is called and what
#: npm matches. macOS has no frozen build, so it has no package.
PLATFORMS = {
    "win32-x64": {"os": "win32", "cpu": "x64", "asset": "windows-amd64"},
    "win32-arm64": {"os": "win32", "cpu": "arm64", "asset": "windows-arm64"},
    "linux-x64": {"os": "linux", "cpu": "x64", "asset": "linux-amd64"},
    "linux-arm64": {"os": "linux", "cpu": "arm64", "asset": "linux-arm64"},
}

_PEP440 = re.compile(r"^(?P<base>\d+\.\d+\.\d+)(?:(?P<kind>a|b|rc)(?P<num>\d+))?$")
_KINDS = {"a": "alpha", "b": "beta", "rc": "rc"}
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?$")


def _release_meta():
    spec = importlib.util.spec_from_file_location("release_meta", Path(__file__).with_name("release_meta.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def npm_version(version: str) -> str:
    """A PEP 440 version as npm orders it: `0.2.0a2` -> `0.2.0-alpha.2`.

    The same spelling as the tag. Anything else is refused rather than guessed:
    npm needs three numbers, and a version that sorts wrongly in npm makes
    `npm update` keep the older one.
    """
    match = _PEP440.match(version)
    if match is None:
        raise ValueError(f"cannot turn {version!r} into an npm version")
    if match.group("kind") is None:
        return match.group("base")
    return f"{match.group('base')}-{_KINDS[match.group('kind')]}.{match.group('num')}"


def prerelease_label(version: str) -> str | None:
    """`alpha` for `0.2.0-alpha.2`, None for a release."""
    if not _SEMVER.match(version):
        raise ValueError(f"{version!r} is not a version this script publishes")
    return version.split("-", 1)[1].split(".", 1)[0] if "-" in version else None


def platform_tag(version: str, platform: str) -> str:
    label = prerelease_label(version)
    return platform if label is None else f"{label}-{platform}"


def launcher_tag(version: str, latest: str | None) -> str:
    """`latest` for a release, and for a pre-release while no release exists.

    `latest` is what `npm install codexsync` installs. While every version is
    a pre-release it follows the newest one, as `pip install --pre` would;
    once a release exists a pre-release only moves its own label. A `latest`
    naming a platform package (npm gives the very first version published
    `latest` whatever its tag) is no release, so it is replaced.
    """
    label = prerelease_label(version)
    if label is None:
        return "latest"
    if latest is None or "-" in latest:
        return "latest"
    return label


def project_metadata() -> dict:
    with open(REPO_ROOT / "pyproject.toml", "rb") as handle:
        project = tomllib.load(handle)["project"]
    return {
        "version": project["version"],
        "description": project["description"],
        "author": project["authors"][0]["name"],
        "homepage": project["urls"]["Homepage"],
        "repository": project["urls"]["Repository"].rstrip("/"),
        "license": project["license"],
    }


def _common(meta: dict) -> dict:
    return {
        "license": meta["license"],
        "author": meta["author"],
        "homepage": meta["homepage"],
        # Trusted publishing and provenance compare this with the repository
        # the workflow runs in.
        "repository": {"type": "git", "url": f"git+{meta['repository']}.git"},
        "bugs": {"url": f"{meta['repository']}/issues"},
    }


def platform_manifest(platform: str, version: str, meta: dict) -> dict:
    system = PLATFORMS[platform]
    return {
        "name": NAME,
        "version": f"{version}-{platform}",
        "description": f"The {platform} build of codexSync, installed by the codexsync package.",
        **_common(meta),
        "os": [system["os"]],
        "cpu": [system["cpu"]],
        "files": ["dist", "LICENSE"],
    }


def launcher_manifest(version: str, meta: dict) -> dict:
    return {
        "name": NAME,
        "version": version,
        "description": meta["description"],
        **_common(meta),
        "keywords": ["codex", "openai", "sync", "backup", "chats"],
        "bin": {program: f"bin/{program}.js" for program in PROGRAMS},
        "files": ["bin", "lib", "README.md", "LICENSE"],
        "engines": {"node": ">=18"},
        "os": sorted({system["os"] for system in PLATFORMS.values()}),
        "cpu": sorted({system["cpu"] for system in PLATFORMS.values()}),
        "optionalDependencies": {
            f"{NAME}-{platform}": f"npm:{NAME}@{version}-{platform}" for platform in PLATFORMS
        },
    }


def _write_manifest(folder: Path, manifest: dict) -> None:
    (folder / "package.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Every member, refused if it would land outside the folder or is not a plain file, folder or link."""
    members = archive.getmembers()
    for member in members:
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts:
            raise ValueError(f"{member.name}: outside the archive's folder")
        if member.issym():
            target = PurePosixPath(member.linkname)
            if target.is_absolute() or _escapes(name, target):
                raise ValueError(f"{member.name}: a link out of the archive's folder")
        elif member.islnk():
            # A hard link names another member, by its path in the archive.
            target = PurePosixPath(member.linkname)
            if target.is_absolute() or ".." in target.parts:
                raise ValueError(f"{member.name}: a link out of the archive's folder")
        elif not (member.isfile() or member.isdir()):
            raise ValueError(f"{member.name}: not a file, a folder or a link")
    return members


def _escapes(name: PurePosixPath, target: PurePosixPath) -> bool:
    """Does a link at ``name`` pointing at relative ``target`` leave the archive's root?"""
    depth = len(name.parts) - 1
    for part in target.parts:
        depth += -1 if part == ".." else (0 if part == "." else 1)
        if depth < 0:
            return True
    return False


def _stage_windows(assets: Path, tag: str, system: dict, dist: Path) -> None:
    dist.mkdir(parents=True)
    for program in PROGRAMS:
        archive = assets / f"{program}-{tag}-{system['asset']}.zip"
        with zipfile.ZipFile(archive) as bundle:
            dist.joinpath(f"{program}.exe").write_bytes(bundle.read(f"{program}.exe"))


def _stage_linux(assets: Path, tag: str, system: dict, dist: Path) -> None:
    if sys.platform == "win32":
        # The executable bits would be lost on the way, and npm packs a file's
        # mode as it finds it.
        raise SystemExit("a Linux package is staged on Linux or macOS, where file modes survive")
    archive = assets / f"{NAME}-{tag}-{system['asset']}.tar.gz"
    with tempfile.TemporaryDirectory() as work, tarfile.open(archive) as bundle:
        members = _safe_members(bundle)
        if hasattr(tarfile, "data_filter"):
            bundle.extractall(work, members=members, filter="data")
        else:  # pragma: no cover - Python 3.11
            bundle.extractall(work, members=members)
        folders = [item for item in Path(work).iterdir() if item.is_dir()]
        if len(folders) != 1:
            raise SystemExit(f"{archive.name}: expected one folder, found {len(folders)}")
        # symlinks=False: a link becomes a copy of what it points at.
        shutil.copytree(folders[0], dist, symlinks=False)
    for program in PROGRAMS:
        if not (dist / program).is_file() or not (dist / program).stat().st_mode & 0o100:
            raise SystemExit(f"{archive.name}: {program} is missing or not executable")


def stage(assets: Path, tag: str, out: Path, platforms: list[str]) -> list[Path]:
    """Write one folder per package under ``out``; returns them, the launcher last."""
    meta = project_metadata()
    release_meta = _release_meta()
    if release_meta.version_from_tag(tag) != meta["version"]:
        raise SystemExit(f"{tag} does not name version {meta['version']}, which pyproject.toml declares")
    version = npm_version(meta["version"])
    folders = []
    for platform in platforms:
        system = PLATFORMS[platform]
        folder = out / f"{NAME}-{platform}"
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        dist = folder / "dist"
        if system["os"] == "win32":
            _stage_windows(assets, tag, system, dist)
        else:
            _stage_linux(assets, tag, system, dist)
        if any(path.is_symlink() for path in dist.rglob("*")):
            raise SystemExit(f"{folder.name}: a symbolic link survived; npm would leave it out")
        shutil.copy2(LICENSE_FILE, folder / "LICENSE")
        _write_manifest(folder, platform_manifest(platform, version, meta))
        folders.append(folder)

    launcher = out / NAME
    if launcher.exists():
        shutil.rmtree(launcher)
    shutil.copytree(LAUNCHER, launcher)
    shutil.copy2(LICENSE_FILE, launcher / "LICENSE")
    _write_manifest(launcher, launcher_manifest(version, meta))
    folders.append(launcher)
    return folders


def _npm() -> str:
    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit("npm is not on PATH")
    return npm


def pack(folders: list[Path], out: Path) -> list[Path]:
    packed = []
    for folder in folders:
        result = subprocess.run(
            [_npm(), "pack", "--json", "--pack-destination", str(out)],
            cwd=folder, check=True, capture_output=True, text=True,
        )
        packed.append(out / json.loads(result.stdout)[0]["filename"])
    return packed


def _published(spec: str) -> bool:
    result = subprocess.run([_npm(), "view", spec, "version", "--json"], capture_output=True, text=True)
    return result.returncode == 0 and bool(result.stdout.strip())


def _latest() -> str | None:
    result = subprocess.run([_npm(), "view", NAME, "dist-tags", "--json"], capture_output=True, text=True)
    if result.returncode != 0:
        return None  # not on npm yet
    return json.loads(result.stdout or "{}").get("latest")


def publish(folder: Path, tag: str) -> int:
    version = npm_version(_release_meta().version_from_tag(tag))
    order = [(folder / f"{NAME}-{version}-{platform}.tgz", f"{version}-{platform}", platform) for platform in PLATFORMS]
    order.append((folder / f"{NAME}-{version}.tgz", version, None))
    missing = [str(path) for path, _, _ in order if not path.is_file()]
    if missing:
        raise SystemExit("not packed: " + ", ".join(missing))
    for path, spec_version, platform in order:
        if _published(f"{NAME}@{spec_version}"):
            print(f"{NAME}@{spec_version} is already on npm")
            continue
        dist_tag = platform_tag(version, platform) if platform else launcher_tag(version, _latest())
        subprocess.run([_npm(), "publish", str(path), "--tag", dist_tag, "--access", "public"], check=True)
        print(f"{NAME}@{spec_version} published as {dist_tag}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    staging = commands.add_parser("stage", help="write and pack one package per system plus the launcher")
    staging.add_argument("--tag", required=True)
    staging.add_argument("--assets", type=Path, required=True, help="the folder holding the release zips and tar.gz files")
    staging.add_argument("--out", type=Path, required=True)
    staging.add_argument("--platforms", default=",".join(PLATFORMS), help="a subset, for a local check")
    staging.add_argument("--no-pack", action="store_true", help="write the folders only")
    publishing = commands.add_parser("publish", help="publish what stage packed, platforms first")
    publishing.add_argument("--tag", required=True)
    publishing.add_argument("--dir", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "publish":
        return publish(args.dir.resolve(), args.tag)
    platforms = [item for item in args.platforms.split(",") if item]
    unknown = sorted(set(platforms) - set(PLATFORMS))
    if unknown:
        parser.error(f"no build is published for {', '.join(unknown)}")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    folders = stage(args.assets.resolve(), args.tag, out, platforms)
    for path in folders if args.no_pack else pack(folders, out):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
