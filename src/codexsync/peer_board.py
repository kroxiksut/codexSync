"""One self-verifying file per machine in a shared folder.

The pattern the project lists use (`project_sync`), kept in one place for the
boards that came after it: chat names (D-025) and project files (D-026). Each
machine writes only ``<machine>.json``, replaced in a single step; a reader
believes a file only when its ``entry_digest`` covers its contents and it
claims the machine its name says. A cloud client delivers files in any order
and sometimes half-written, so a bad file is reported, never fatal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any
import uuid

from .fs_replace import replace_with_retry

LOG = logging.getLogger(__name__)


def digest(body: Any) -> str:
    text = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BoardRead:
    #: Machine -> the file's body, without its digest.
    bodies: dict[str, dict[str, Any]]
    #: File name -> why it was not believed.
    unreadable: dict[str, str] = field(default_factory=dict)


def read_bodies(root: Path, file_format: str) -> BoardRead:
    bodies: dict[str, dict[str, Any]] = {}
    unreadable: dict[str, str] = {}
    try:
        entries = sorted(root.iterdir())
    except FileNotFoundError:
        return BoardRead({})
    except OSError as exc:
        return BoardRead({}, {str(root): str(exc)})
    for entry in entries:
        if entry.suffix != ".json" or entry.name.startswith(".") or not entry.is_file():
            continue
        try:
            raw = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            unreadable[entry.name] = str(exc)
            continue
        if not isinstance(raw, dict) or raw.get("format") != file_format:
            unreadable[entry.name] = f"not a {file_format} file"
            continue
        body = {key: value for key, value in raw.items() if key != "entry_digest"}
        if raw.get("entry_digest") != digest(body):
            # Half-delivered by the cloud client, or edited by hand.
            unreadable[entry.name] = "digest does not match the contents"
            continue
        if body.get("machine") != entry.stem:
            unreadable[entry.name] = f"file of {entry.stem!r} claims machine {body.get('machine')!r}"
            continue
        bodies[entry.stem] = body
    return BoardRead(bodies, unreadable)


def write_body(root: Path, machine: str, body: dict[str, Any]) -> Path:
    """Replace this machine's file in one step, staged beside it."""
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{machine}.json"
    staging = root / f".{machine}.{uuid.uuid4().hex}.codexsync.tmp"
    payload = json.dumps({**body, "entry_digest": digest(body)}, ensure_ascii=False, sort_keys=True, indent=2)
    try:
        with staging.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(staging, target)
    finally:
        if staging.exists():
            try:
                staging.unlink()
            except OSError:
                LOG.warning("could not remove staging file %s", staging)
    return target
