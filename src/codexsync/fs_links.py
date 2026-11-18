"""One rule for "does this directory entry point somewhere else".

Three readers walk trees they must not leave — a project being moved, the
`.codex` copy, the session catalogue — and each used to answer this on its
own. ``is_symlink()`` misses a Windows junction, so a walk that trusted it
followed `.codex/skills` into folders outside `.codex`; "any reparse point"
catches the junction but also every cloud placeholder, which is an ordinary
file or folder whose content reads like any other.

What separates a link is the name-surrogate bit Windows sets on every reparse
tag that names another location (symlinks, junctions, WSL links). Placeholders
of Yandex.Disk and OneDrive (``IO_REPARSE_TAG_CLOUD_*``, ``0x9000xxxx``) lack
it. Only when the tag cannot be read is a reparse point assumed to be a link,
because then nothing proves it is not. ``os.path.isjunction`` exists only from
Python 3.12, so the attribute test is what carries 3.11; elsewhere the stat
result has no Windows attributes and only a symlink is a link.
"""
from __future__ import annotations

import os
import stat

FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
#: Bit Windows sets on every reparse tag that *names another location*: symlinks,
#: junctions, WSL links. Tags without it hold the data themselves.
REPARSE_TAG_NAME_SURROGATE = 0x20000000


def is_link(path: str | os.PathLike[str], info: os.stat_result) -> bool:
    """Whether the entry at ``path``, whose ``lstat`` is ``info``, is a link."""
    if stat.S_ISLNK(info.st_mode):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None and isjunction(path):
        return True
    if not getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    tag = getattr(info, "st_reparse_tag", None)
    if not isinstance(tag, int) or tag == 0:
        return True
    return bool(tag & REPARSE_TAG_NAME_SURROGATE)


def extended_path(path: str) -> str:
    """``path`` in Windows' extended-length form, not held to 260 characters.

    Elsewhere, and for a path already in that form, it is returned as given.
    """
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    full = os.path.abspath(path)
    if full.startswith("\\\\"):
        return "\\\\?\\UNC\\" + full[2:]
    return "\\\\?\\" + full
