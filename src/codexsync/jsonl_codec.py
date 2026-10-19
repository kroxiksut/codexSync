"""How a session branch is stored on disk, and how to read one back.

A branch inside the Codex state directory is plain JSONL. The runtime reads
those files, so nothing may transform them. The codexSync cloud mirror is a
different destination — nothing but codexSync reads it — so a branch may be
stored there in a compressed container.

That is worth doing because the mirror is a cloud folder and its cost is
measured in bytes. Measured on the state this was built against: 228 branches,
778 MiB, median 640 KiB; the same content is about a third of that as xz (824
MiB of mirrored sessions came to 299 MiB). The
file count is unchanged, which is deliberate — one archive of everything would
be re-uploaded whole after a single session grew by one line, and would have to
be unpacked before the remote side could be classified at all.

Compression is a property of the *container* and never of the history. Every
value that decides anything — a branch hash, a record count, a byte count, the
comparison between two branches — is computed over the decompressed stream, so
a compressed mirror copy of a branch compares `IDENTICAL` to the plain local
one instead of looking like a divergence. That is the whole reason reading goes
through `open_jsonl` here rather than `path.open("rb")` at each call site.

The codec is recorded in the mirror layout id, which is part of the frozen
transfer plan, so a plan built for one container can never be applied under
another: the plan id simply stops matching.
"""
from __future__ import annotations

from enum import Enum
import gzip
import lzma
import os
from pathlib import Path
import re
import shutil
from typing import BinaryIO
import zlib

try:  # Python 3.14+
    from compression import zstd as _zstd
except ImportError:  # pragma: no cover - depends on the interpreter
    _zstd = None

#: Whether this interpreter reads zstd: the standard library has it from
#: Python 3.14 on (`compression.zstd`), and the core takes no dependency.
ZSTD_READABLE = _zstd is not None


#: Suffix a branch keeps in every container. What precedes it is the logical
#: name, which is the same on both sides regardless of how the file is stored.
JSONL_SUFFIX = ".jsonl"

#: xz preset. 1 is the measured sweet spot on session JSONL: it reaches 9-21%
#: of the original on real branches at roughly 25 MiB/s, and the higher presets
#: buy single-digit percentages for several times the time and memory.
_XZ_PRESET = 1

_COPY_CHUNK = 1024 * 1024

#: What reading a branch can raise. A plain file only ever fails with an
#: ``OSError``, but a container has its own: ``gzip`` raises ``EOFError`` on a
#: truncated member, ``zlib.error`` on a damaged deflate stream, and ``lzma``
#: raises ``LZMAError`` on a corrupt one, and none of them is an ``OSError``.
#: A half-written container is the expected condition in a mirror a cloud
#: client writes on its own schedule, so every reader catches these together
#: rather than dying with a traceback on a file that will be complete a second
#: later.
JSONL_READ_ERRORS: tuple[type[BaseException], ...] = (OSError, EOFError, lzma.LZMAError, zlib.error) + (
    (_zstd.ZstdError,) if _zstd is not None else ()
)


class JsonlCodec(str, Enum):
    """Container a session branch is stored in."""

    NONE = "none"
    GZIP = "gzip"
    XZ = "xz"
    #: Read only, and only where `ZSTD_READABLE` (CS-425): Codex 0.163 keeps
    #: chats it has not touched for a while as `.jsonl.zst`. codexSync never
    #: writes it -- not into `.codex`, which is always plain, and not into the
    #: mirror, which an interpreter without zstd must still read.
    ZSTD = "zstd"

    @property
    def suffix(self) -> str:
        return _SUFFIXES[self]

    @property
    def file_suffix(self) -> str:
        """Full suffix a branch file carries in this container."""
        return JSONL_SUFFIX + self.suffix


_SUFFIXES: dict[JsonlCodec, str] = {
    JsonlCodec.NONE: "",
    JsonlCodec.GZIP: ".gz",
    JsonlCodec.XZ: ".xz",
    JsonlCodec.ZSTD: ".zst",
}

#: The containers codexSync writes, and the only ones `[semantic]
#: mirror_compression` may name.
WRITABLE_CODECS: tuple[JsonlCodec, ...] = (JsonlCodec.NONE, JsonlCodec.GZIP, JsonlCodec.XZ)
#: The containers this interpreter reads. Without zstd a `.jsonl.zst` is not a
#: known container at all, and `unsupported_container` reports it instead.
READABLE_CODECS: tuple[JsonlCodec, ...] = WRITABLE_CODECS + ((JsonlCodec.ZSTD,) if ZSTD_READABLE else ())

#: Longest suffix first, so ``.jsonl.gz`` is never read as ``.jsonl``.
_BY_SUFFIX: tuple[tuple[str, JsonlCodec], ...] = tuple(
    sorted(
        ((codec.file_suffix, codec) for codec in READABLE_CODECS),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
)


def codec_of(name: str | Path) -> JsonlCodec | None:
    """Container the given file name is in, or None when it is not a branch."""
    text = (name.name if isinstance(name, Path) else name).lower()
    for suffix, codec in _BY_SUFFIX:
        if text.endswith(suffix):
            return codec
    return None


#: A branch in a container this version cannot open: one suffix after
#: `.jsonl`. Codex itself stores chats so -- `rollout-...jsonl.zst` (zstd),
#: first seen 2026-10-15 on Codex 0.163 for every chat not touched for a
#: while, while its catalogue still names the plain `.jsonl` (CS-394).
_UNSUPPORTED_CONTAINER = re.compile(r"\.jsonl\.([a-z0-9]{1,10})$", re.IGNORECASE)


def unsupported_container(name: str | Path) -> str | None:
    """The container suffix (``"zst"``) of a branch this version cannot read, or ``None``.

    Such a file is still that chat's history: it is reported and blocks the
    chat, never skipped -- a skipped copy made the chat look absent here, and
    the mirror's older copy was then planned into `.codex` beside it. A
    hidden name (a staging file) is never a branch.
    """
    text = name.name if isinstance(name, Path) else name
    if text.startswith(".") or codec_of(text) is not None:
        return None
    match = _UNSUPPORTED_CONTAINER.search(text)
    return match.group(1).lower() if match else None


def is_branch_file(name: str | Path) -> bool:
    return codec_of(name) is not None


def logical_name(name: str) -> str:
    """The name this branch would have as plain JSONL.

    Two sides may store one branch in different containers, so anything that
    compares or derives a path works on the logical name -- a container this
    version cannot read included.
    """
    codec = codec_of(name)
    if codec is None:
        unsupported = unsupported_container(name)
        return name[: -(len(unsupported) + 1)] if unsupported else name
    if codec is JsonlCodec.NONE:
        return name
    return name[: -len(codec.suffix)]


def held_in_any_container(path: Path) -> bool:
    """Whether the branch ``path`` names exists in some container, readable here or not.

    Codex's catalogue names `x.jsonl` while the file is `x.jsonl.zst`, so a
    plain existence check reports a chat Codex reads as gone. Reads only.
    """
    if os.path.lexists(path):
        return True
    base = logical_name(path.name).lower()
    try:
        names = os.listdir(path.parent)
    except OSError:
        return False
    return any(
        logical_name(name).lower() == base and (codec_of(name) is not None or unsupported_container(name))
        for name in names
    )


def logical_relative_path(relative_path: str) -> str:
    head, _, tail = relative_path.rpartition("/")
    stripped = logical_name(tail)
    return f"{head}/{stripped}" if head else stripped


def with_codec(relative_path: str, codec: JsonlCodec) -> str:
    """The logical path stored in ``codec``. Idempotent by construction."""
    return logical_relative_path(relative_path) + codec.suffix


def open_jsonl(path: Path, codec: JsonlCodec | None = None) -> BinaryIO:
    """Open a branch for reading as a binary stream of JSONL bytes.

    The returned handle yields exactly the bytes the branch has as plain JSONL,
    whatever container it is stored in, and supports ``readline(limit)`` so a
    single oversized record still cannot be read into memory unbounded.

    ``codec`` is inferred from the file name, which is right for a branch in
    place. Pass it explicitly for a staged payload, whose name is a temporary
    one that says nothing about its container.
    """
    if codec is None:
        codec = codec_of(path)
    if codec is JsonlCodec.GZIP:
        return gzip.open(path, "rb")  # type: ignore[return-value]
    if codec is JsonlCodec.XZ:
        return lzma.open(path, "rb")  # type: ignore[return-value]
    if codec is JsonlCodec.ZSTD:
        if _zstd is None:
            raise OSError(f"this Python cannot read zstd: {path.name}")
        return _zstd.open(path, "rb")  # type: ignore[return-value]
    return path.open("rb")


def transcode(
    source: Path, destination: Path, source_codec: JsonlCodec, destination_codec: JsonlCodec
) -> None:
    """Rewrite ``source`` from one container into another, streaming.

    A transfer moves a branch between two directories that need not store it
    the same way: out of the mirror it must be decompressed, into the mirror
    compressed, and between two like containers it is a plain copy. Doing this
    in one place is what keeps a compressed body from ever landing under a
    plain name.
    """
    if destination_codec not in WRITABLE_CODECS:
        raise ValueError(f"codexSync never writes {destination_codec.value}")
    if source_codec is destination_codec:
        shutil.copyfile(source, destination)
        return
    with open_jsonl(source, source_codec) as reader:
        if destination_codec is JsonlCodec.NONE:
            with destination.open("wb") as writer:
                shutil.copyfileobj(reader, writer, _COPY_CHUNK)
        elif destination_codec is JsonlCodec.GZIP:
            with destination.open("wb") as raw:
                with gzip.GzipFile(
                    filename="", fileobj=raw, mode="wb", compresslevel=6, mtime=0
                ) as writer:
                    shutil.copyfileobj(reader, writer, _COPY_CHUNK)
        else:
            with lzma.open(destination, "wb", preset=_XZ_PRESET) as writer:
                shutil.copyfileobj(reader, writer, _COPY_CHUNK)


def compress_file(source: Path, destination: Path, codec: JsonlCodec) -> None:
    """Stream ``source`` into ``destination`` in ``codec``.

    Never loads a branch into memory: the largest observed one is 121 MiB.

    The output is deterministic for the same input, so re-storing an unchanged
    branch produces identical bytes and a cloud client sees no change at all.
    That takes two arguments from gzip, which by default stamps the header with
    the current time *and* the destination's name: since a branch is written
    through a staging file whose name is a position in the plan, the name would
    make every rewrite differ from the last for no reason at all.
    """
    transcode(source, destination, JsonlCodec.NONE, codec)


def parse_codec(value: str) -> JsonlCodec:
    """A container codexSync may write, as `[semantic] mirror_compression` names it."""
    allowed = ", ".join(codec.value for codec in WRITABLE_CODECS)
    try:
        codec = JsonlCodec(value)
    except ValueError as exc:
        raise ValueError(f"unknown compression {value!r}; expected one of {allowed}") from exc
    if codec not in WRITABLE_CODECS:
        raise ValueError(f"compression {value!r} is read, never written; expected one of {allowed}")
    return codec
