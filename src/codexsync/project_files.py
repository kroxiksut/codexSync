"""Whether a project's own files came along with its chats (D-026).

codexSync carries chats and the project list, never the project folders: the
code lives in git, in a cloud folder, or nowhere but one disk. A chat that
continues here against an older copy of the code works on the wrong files, and
nothing in Codex says so. So each machine publishes, at every full sync, what
its project folders hold, and the next machine compares that with its own:

- a **git** folder: the commit checked out, the branch, and whether there were
  uncommitted changes (with a digest of them, so two machines whose cloud
  client carried the same edits agree);
- any **other** folder: every file with its size, SHA-256 and change time --
  heavy tool folders left out, files above `HASH_LIMIT` by size and time only.
  A hash is recomputed only for a file whose size or time moved (`HashCache`,
  in this machine's local cache folder), and a file with the same bytes is the
  same file whatever its time, so a copy made later says nothing.

With each project goes the last time one of its chats changed on that machine
(``threads.cwd``/``updated_at``, read-only). The receiving machine says first
which projects saw chats continue elsewhere -- that is where files most likely
moved -- but compares every project, since anything may change a folder.

Only warnings come of it; nothing is copied, pulled or blocked. A commit from
the other machine missing here, a history that went two ways, changes left
uncommitted there, or plain files newer there are what the person is told.
Files newer *here* are this machine's own work and say nothing.

Git runs as an external program when it is installed; codexSync takes no
dependency on it, and a folder git cannot read is compared as plain files.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any, Callable

from .fs_links import is_link
from .peer_board import read_bodies, write_body
from .fs_replace import replace_with_retry

FILES_FORMAT = "codexsync-project-files-v1"

#: Folders tools rebuild; their contents say nothing about the work.
EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", ".next", ".nuxt", "dist", "build", "target",
    ".gradle", ".idea", ".vs",
})
#: A plain folder with more files than this is reported as not compared.
WALK_LIMIT = 50_000
#: Files above this are compared by size and change time, not read.
HASH_LIMIT = 100 * 1024 * 1024

#: (returncode, stdout) of ``git -C <root> <args>``; returncode -1 when git
#: could not be started at all.
GitRunner = Callable[[Path, list[str]], tuple[int, bytes]]


def run_git(root: Path, args: list[str]) -> tuple[int, bytes]:
    """Run git without ever showing a console window (see process_detector)."""
    kwargs: dict[str, Any] = {}
    startupinfo_class = getattr(subprocess, "STARTUPINFO", None)
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", None)
    if startupinfo_class is not None and no_window is not None:
        startupinfo = startupinfo_class()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = no_window
    # A status refreshes and rewrites `.git/index` when it can; this is a
    # reading, so it must not touch the person's repository.
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, check=False, stdin=subprocess.DEVNULL, timeout=60, env=env, **kwargs,
        )
    except (OSError, subprocess.SubprocessError):
        return -1, b""
    return done.returncode, done.stdout


class FilesKind(str, Enum):
    GIT = "git"
    PLAIN = "plain"
    MISSING = "missing"


#: One file of a plain folder: size, SHA-256 (``None`` above `HASH_LIMIT`),
#: and the time it last changed.
FileEntry = tuple[int, "str | None", int]


@dataclass(frozen=True, slots=True)
class FilesState:
    kind: FilesKind
    head: str | None = None
    branch: str | None = None
    dirty: bool = False
    #: Digest of the uncommitted changes (git) -- equal when two machines
    #: hold the same edits on the same commit.
    worktree: str | None = None
    #: Plain folders: relative POSIX path -> (size, sha256, mtime_ns).
    entries: dict[str, FileEntry] = field(default_factory=dict)
    truncated: bool = False
    #: Plain folders: files this machine had at an earlier publication and
    #: no longer has -> when that was first seen (unix seconds). Without it
    #: "added here" and "deleted there" read the same.
    removed: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value, "head": self.head, "branch": self.branch, "dirty": self.dirty,
            "worktree": self.worktree, "truncated": self.truncated,
            "entries": {key: list(value) for key, value in sorted(self.entries.items())},
            "removed": dict(sorted(self.removed.items())),
        }

    @classmethod
    def from_json(cls, raw: Any) -> "FilesState":
        if not isinstance(raw, dict):
            raise ValueError("state must be an object")
        entries: dict[str, FileEntry] = {}
        for key, value in dict(raw.get("entries") or {}).items():
            size, digest, mtime = value
            entries[str(key)] = (int(size), digest if isinstance(digest, str) else None, int(mtime))
        return cls(
            FilesKind(raw["kind"]),
            raw.get("head") if isinstance(raw.get("head"), str) else None,
            raw.get("branch") if isinstance(raw.get("branch"), str) else None,
            bool(raw.get("dirty")),
            raw.get("worktree") if isinstance(raw.get("worktree"), str) else None,
            entries,
            bool(raw.get("truncated")),
            {str(key): int(value) for key, value in dict(raw.get("removed") or {}).items()},
        )


def _text(output: bytes) -> str:
    return output.decode("utf-8", errors="replace").strip()


def _git_state(root: Path, git: GitRunner) -> FilesState | None:
    code, out = git(root, ["rev-parse", "--is-inside-work-tree"])
    if code != 0 or _text(out) != "true":
        return None
    # A folder an enclosing repository ignores is not part of it: its commit
    # says nothing about these files.
    if git(root, ["check-ignore", "-q", "."])[0] == 0:
        return None
    code, out = git(root, ["rev-parse", "--verify", "-q", "HEAD"])
    head = _text(out) if code == 0 and out.strip() else None
    code, out = git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    branch = _text(out) if code == 0 and out.strip() else None
    code, status = git(root, ["status", "--porcelain=v1", "-z"])
    if code != 0:
        return None
    dirty = bool(status.strip(b"\0 \n"))
    worktree = None
    if dirty:
        changes = hashlib.sha256()
        code, diff = git(root, ["diff", "HEAD", "--binary"] if head else ["diff", "--cached", "--binary"])
        changes.update(diff if code == 0 else b"?")
        code, others = git(root, ["ls-files", "--others", "--exclude-standard", "-z"])
        for name in sorted(item for item in others.split(b"\0") if item) if code == 0 else ():
            path = root / name.decode("utf-8", errors="replace")
            try:
                changes.update(name + b"\0" + str(path.stat().st_size).encode() + b"\0")
            except OSError:
                changes.update(name + b"\0?\0")
        worktree = changes.hexdigest()
    return FilesState(FilesKind.GIT, head, branch, dirty, worktree)


class HashCache:
    """This machine's file hashes for one folder, so only changed files are read.

    Kept in the local cache folder, never in the project or the workspace: a
    hash is recomputed whenever the size or the change time differs, and a
    missing or damaged cache is simply rebuilt.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.known: dict[str, list[Any]] = {}
        self.used: dict[str, list[Any]] = {}
        if path is not None:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.known = {str(key): value for key, value in raw.items() if isinstance(value, list)}
            except (OSError, ValueError):
                self.known = {}

    def lookup(self, relative: str, size: int, mtime_ns: int) -> str | None:
        value = self.known.get(relative)
        if value and len(value) == 3 and value[0] == size and value[1] == mtime_ns and isinstance(value[2], str):
            return value[2]
        return None

    def remember(self, relative: str, size: int, mtime_ns: int, digest: str) -> None:
        self.used[relative] = [size, mtime_ns, digest]

    def save(self) -> None:
        if self.path is None or self.used == self.known:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            temp.write_text(json.dumps(self.used, sort_keys=True), encoding="utf-8")
            replace_with_retry(temp, self.path)
        except OSError:
            pass  # a cache that cannot be written is recomputed next time


def cache_file_for(cache_root: Path, folder: Path) -> Path:
    key = hashlib.sha256(str(folder).casefold().encode("utf-8")).hexdigest()[:20]
    return cache_root / f"{key}.json"


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plain_state(root: Path, limit: int, cache: HashCache) -> FilesState:
    entries: dict[str, FileEntry] = {}
    truncated = False
    for directory, subdirs, names in os.walk(root):
        current = Path(directory)
        kept = []
        for name in subdirs:
            child = current / name
            try:
                if name in EXCLUDED_DIRS or is_link(child, child.lstat()):
                    continue
            except OSError:
                continue
            kept.append(name)
        subdirs[:] = sorted(kept)
        for name in sorted(names):
            path = current / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode) or is_link(path, info):
                continue
            relative = path.relative_to(root).as_posix()
            digest = None
            if info.st_size <= HASH_LIMIT:
                digest = cache.lookup(relative, info.st_size, info.st_mtime_ns)
                if digest is None:
                    try:
                        digest = _hash(path)
                    except OSError:
                        digest = None
                if digest is not None:
                    cache.remember(relative, info.st_size, info.st_mtime_ns, digest)
            entries[relative] = (info.st_size, digest, info.st_mtime_ns)
            if len(entries) >= limit:
                truncated = True
                break
        if truncated:
            break
    cache.save()
    return FilesState(FilesKind.PLAIN, entries=entries, truncated=truncated)


def read_files_state(
    root: Path, *, git: GitRunner = run_git, limit: int = WALK_LIMIT, cache_root: Path | None = None,
) -> FilesState:
    """What one project folder holds now. Reads only (the hash cache aside)."""
    if not root.is_dir():
        return FilesState(FilesKind.MISSING)
    state = _git_state(root, git)
    if state is not None:
        return state
    cache = HashCache(cache_file_for(cache_root, root) if cache_root is not None else None)
    return _plain_state(root, limit, cache)


class Verdict(str, Enum):
    #: The same work on both machines.
    IN_STEP = "IN_STEP"
    #: This machine has more than the other; nothing to say.
    AHEAD_HERE = "AHEAD_HERE"
    #: The other machine's commit is not here, or is newer than this one's.
    BEHIND = "BEHIND"
    #: Both machines committed work the other does not have.
    DIVERGED = "DIVERGED"
    #: The other machine left changes uncommitted that are not here.
    UNCOMMITTED_THERE = "UNCOMMITTED_THERE"
    #: A plain folder with files that changed later there, exist only there,
    #: or were deleted there and are still here.
    FILES_CHANGED_THERE = "FILES_CHANGED_THERE"
    #: The folder does not exist on this machine.
    MISSING_HERE = "MISSING_HERE"
    #: The folder exists here and not on the other machine.
    MISSING_THERE = "MISSING_THERE"
    #: One side is git and the other is not, or a folder could not be read.
    NOT_COMPARED = "NOT_COMPARED"


#: Verdicts the person is told about.
WARNINGS = frozenset({
    Verdict.BEHIND, Verdict.DIVERGED, Verdict.UNCOMMITTED_THERE, Verdict.FILES_CHANGED_THERE,
    Verdict.MISSING_HERE, Verdict.MISSING_THERE,
})
#: How long a deleted file is remembered in a publication.
REMOVED_KEPT_SECONDS = 90 * 24 * 3600


@dataclass(frozen=True, slots=True)
class Comparison:
    verdict: Verdict
    #: Plain folders: files that differ and changed later on the other machine.
    newer_there: tuple[str, ...] = ()
    #: Plain folders: files the other machine has and this one does not.
    missing_here: tuple[str, ...] = ()
    #: Plain folders: files deleted on the other machine and still here.
    removed_there: tuple[str, ...] = ()


def _same_file(here: FileEntry, there: FileEntry) -> bool:
    if here[1] is not None and there[1] is not None:
        return here[1] == there[1]
    # Above the hashing limit: size and change time are all there is.
    return here[0] == there[0] and here[2] == there[2]


def _compare_plain(here: FilesState, there: FilesState) -> Comparison:
    if here.truncated or there.truncated:
        return Comparison(Verdict.NOT_COMPARED)
    missing = tuple(sorted(path for path in there.entries if path not in here.entries))
    newer = tuple(sorted(
        path for path, entry in there.entries.items()
        if path in here.entries and not _same_file(here.entries[path], entry)
        and entry[2] > here.entries[path][2]
    ))
    # Deleted there, and not changed here since: this copy is what is stale.
    removed = tuple(sorted(
        path for path, when in there.removed.items()
        if path in here.entries and path not in there.entries
        and here.entries[path][2] <= when * 1_000_000_000
    ))
    if missing or newer or removed:
        return Comparison(Verdict.FILES_CHANGED_THERE, newer, missing, removed)
    differs_here = here.entries.keys() != there.entries.keys() or any(
        not _same_file(here.entries[path], entry) for path, entry in there.entries.items()
    )
    return Comparison(Verdict.AHEAD_HERE if differs_here else Verdict.IN_STEP)


def compare(root: Path, here: FilesState, there: FilesState, *, git: GitRunner = run_git) -> Comparison:
    """How this folder stands against the other machine's published state."""
    if here.kind is FilesKind.MISSING:
        return Comparison(Verdict.MISSING_HERE)
    if there.kind is FilesKind.MISSING:
        return Comparison(Verdict.MISSING_THERE)
    if here.kind is not there.kind:
        return Comparison(Verdict.NOT_COMPARED)
    if here.kind is FilesKind.PLAIN:
        return _compare_plain(here, there)
    verdict = Verdict.IN_STEP
    if there.head and there.head != here.head:
        present = git(root, ["cat-file", "-e", f"{there.head}^{{commit}}"])[0] == 0
        if not present or not here.head:
            return Comparison(Verdict.BEHIND)
        if git(root, ["merge-base", "--is-ancestor", there.head, here.head])[0] == 0:
            verdict = Verdict.AHEAD_HERE
        elif git(root, ["merge-base", "--is-ancestor", here.head, there.head])[0] == 0:
            return Comparison(Verdict.BEHIND)
        else:
            return Comparison(Verdict.DIVERGED)
    if there.dirty and (there.head != here.head or there.worktree != here.worktree):
        return Comparison(Verdict.UNCOMMITTED_THERE)
    return Comparison(verdict)


@dataclass(frozen=True, slots=True)
class FilesPublication:
    machine: str
    #: Project id -> {"root": str, "name": str, "state": FilesState,
    #: "last_chat_at": int} -- the last time (unix seconds, 0 for never) a
    #: chat of this project changed on that machine.
    projects: dict[str, dict[str, Any]]
    published_at_utc: str = ""

    def body(self) -> dict[str, Any]:
        return {
            "format": FILES_FORMAT,
            "machine": self.machine,
            "projects": {
                key: {
                    "root": value["root"], "name": value["name"], "state": value["state"].to_json(),
                    "last_chat_at": int(value.get("last_chat_at") or 0),
                }
                for key, value in sorted(self.projects.items())
            },
            "published_at_utc": self.published_at_utc,
        }


@dataclass(frozen=True, slots=True)
class FilesBoard:
    publications: dict[str, FilesPublication]
    unreadable: dict[str, str] = field(default_factory=dict)

    def others(self, machine: str) -> list[FilesPublication]:
        return [item for name, item in sorted(self.publications.items()) if name != machine]


def read_files_board(root: Path) -> FilesBoard:
    read = read_bodies(root, FILES_FORMAT)
    publications: dict[str, FilesPublication] = {}
    unreadable = dict(read.unreadable)
    for machine, body in read.bodies.items():
        try:
            projects = {
                str(key): {
                    "root": str(value["root"]), "name": str(value.get("name") or ""),
                    "state": FilesState.from_json(value["state"]),
                    "last_chat_at": int(value.get("last_chat_at") or 0),
                }
                for key, value in dict(body["projects"]).items()
            }
        except (KeyError, TypeError, ValueError) as exc:
            unreadable[f"{machine}.json"] = f"malformed project: {exc}"
            continue
        publications[machine] = FilesPublication(machine, projects, str(body.get("published_at_utc", "")))
    return FilesBoard(publications, unreadable)


def write_files_publication(root: Path, publication: FilesPublication) -> Path:
    return write_body(root, publication.machine, publication.body())


def with_removed(current: FilesState, previous: FilesState | None, now: int) -> FilesState:
    """``current`` carrying the files that vanished since ``previous``.

    A file listed before and gone now is remembered from the first time it
    was missed; one that came back is forgotten; anything older than
    `REMOVED_KEPT_SECONDS` is dropped.
    """
    if current.kind is not FilesKind.PLAIN or previous is None or previous.kind is not FilesKind.PLAIN:
        return current
    removed = {
        path: when for path, when in previous.removed.items()
        if path not in current.entries and now - when < REMOVED_KEPT_SECONDS
    }
    for path in previous.entries:
        if path not in current.entries:
            removed.setdefault(path, now)
    return FilesState(
        current.kind, current.head, current.branch, current.dirty, current.worktree,
        current.entries, current.truncated, removed,
    )


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
