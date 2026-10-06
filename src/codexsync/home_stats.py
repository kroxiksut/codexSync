"""What the Home page shows: this machine's CodexSync at a glance (CS-275).

Two kinds of numbers, kept apart on purpose.

*Cheap ones*, read every time the page opens: the sync journals (last run,
runs and failures and files moved in the last 30 days, anything still open),
the backup folder's manifests, the copies of `.codex`, Guardian's inventory,
the OS tasks, and one sample of the process list. None of them opens a session
file, so the page can read them on arrival without breaking the rule that a
screen does not scan when it is shown (CS-262). Each part is read on its own
and fails on its own: an unreadable Guardian root is one tile saying so, not a
page that shows nothing.

*Expensive ones* -- how many chats, projects and sessions there are -- need the
chat scan, which hashes every session file (thirteen seconds on the machine
this was built on). They come from a cache, written whenever a chat scan runs
anywhere in the window and on the page's own "recount" button, and are shown
with the time they were taken. The cache lives in the application's own folder
(`config_locations.cache_dir`), never in `.codex`, one file per config, and
holds counts only: no name, path, title or id.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

from .automation import AutomationView, automation_status
from .chat_directory import Association, ChatDirectory
from .config import load_config
from .config_locations import cache_dir
from .guardian_inventory import read_guardian_inventory
from .models import AppConfig
from .process_detector import CodexProcessDetector
from .recovery import JournalInfo, list_journals
from .restore import list_backup_snapshots
from .runtime import sample_process_state
from .safety_gate import ProcessState
from .session_catalog import SessionState
from .state_backup import list_state_backups
from .fs_replace import replace_with_retry

__all__ = [
    "HomeSummary",
    "StateStats",
    "read_home_summary",
    "read_state_stats",
    "state_stats_from_directory",
    "write_state_stats",
]

#: The period the sync counters cover.
RECENT_DAYS = 30
CACHE_FORMAT = "codexsync-home-stats-v1"


@dataclass(frozen=True, slots=True)
class SyncStats:
    last: JournalInfo | None
    runs: int
    failed: int
    to_cloud: int
    to_local: int
    #: Chats the same runs carried: a full sync writes them under its own
    #: journal (`sessions`), so the file counts above never include them.
    chats_to_cloud: int = 0
    chats_to_local: int = 0


@dataclass(frozen=True, slots=True)
class CopiesStats:
    count: int
    bytes: int
    newest_utc: str | None


@dataclass(frozen=True, slots=True)
class GuardianStats:
    latest_good_utc: str | None
    snapshots: int
    quarantined: int
    problems: int


@dataclass(frozen=True, slots=True)
class StateStats:
    """Counts from the last chat scan. Numbers only, by design."""

    computed_at_utc: str
    chats: int
    sub_threads: int
    archived: int
    projects: int
    chats_without_project: int
    #: Chats only a `[[path_mappings]]` rule connects to a project, which
    #: Codex does not read: invisible in the app until bound.
    chats_via_mapping: int
    session_bytes: int


@dataclass(frozen=True, slots=True)
class HomeSummary:
    #: ``running`` | ``stopped`` | ``unknown``. Advisory, like every indicator:
    #: a write takes its own reading at the moment it happens.
    codex: str
    sync: SyncStats | None
    open_journals: int | None
    backups: CopiesStats | None
    copies: CopiesStats | None
    #: ``[state_backup] root_dir`` is set.
    copies_configured: bool
    guardian: GuardianStats | None
    automation: AutomationView | None
    state: StateStats | None
    #: Part name -> why it could not be read. A part that failed is ``None``.
    errors: dict[str, str] = field(default_factory=dict)


def _parse_utc(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _sync_stats(journals: list[JournalInfo], now: datetime) -> SyncStats:
    runs = [item for item in journals if item.family == "sync"]
    runs.sort(key=lambda item: item.created_at_utc or "", reverse=True)
    since = now - timedelta(days=RECENT_DAYS)
    recent = [item for item in runs if (_parse_utc(item.created_at_utc) or since) > since]
    chats = [
        item for item in journals
        if item.family == "sessions" and item.readable and item.state == "COMMITTED"
        and (_parse_utc(item.created_at_utc) or since) > since
    ]
    failed = [item for item in recent if not item.readable or item.state != "COMMITTED"]
    committed = [item for item in recent if item.readable and item.state == "COMMITTED"]
    return SyncStats(
        last=runs[0] if runs else None,
        runs=len(recent),
        failed=len(failed),
        to_cloud=sum(int((item.counts or {}).get("to_cloud", 0)) for item in committed),
        to_local=sum(int((item.counts or {}).get("to_local", 0)) for item in committed),
        chats_to_cloud=sum(int((item.counts or {}).get("to_cloud", 0)) for item in chats),
        chats_to_local=sum(int((item.counts or {}).get("to_local", 0)) for item in chats),
    )


def _codex_state(cfg: AppConfig) -> str:
    state, _ = sample_process_state(cfg, CodexProcessDetector(cfg.process_detection.process_names))
    return {ProcessState.RUNNING: "running", ProcessState.STOPPED: "stopped"}.get(state, "unknown")


def read_home_summary(
    config_path: Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    cache_root: Path | None = None,
    automation: Callable[[Path], AutomationView] = automation_status,
) -> HomeSummary:
    """Everything cheap, each part on its own. Writes nothing."""
    cfg = load_config(config_path)
    errors: dict[str, str] = {}

    def part(name: str, read: Callable[[], Any]) -> Any:
        try:
            return read()
        except Exception as exc:  # noqa: BLE001 - one tile fails, the page does not
            errors[name] = f"{type(exc).__name__}: {exc}" if not str(exc) else str(exc)
            return None

    moment = now()
    codex = part("codex", lambda: _codex_state(cfg)) or "unknown"
    journals = part("journals", lambda: list_journals(config_path))
    sync = _sync_stats(journals, moment) if journals is not None else None
    open_journals = sum(1 for item in journals if not item.terminal) if journals is not None else None

    def backups() -> CopiesStats:
        listed = list_backup_snapshots(config_path)
        return CopiesStats(
            len(listed),
            sum(item.total_bytes or 0 for item in listed),
            max((item.created_utc or item.modified_utc for item in listed), default=None),
        )

    def copies() -> CopiesStats:
        own = [item for item in list_state_backups(cfg) if item.own]
        return CopiesStats(len(own), sum(item.size for item in own), own[0].created_utc if own else None)

    def guardian() -> GuardianStats:
        inventory = read_guardian_inventory(config_path)
        latest = next(
            (item.created_at_utc for item in inventory.snapshots if item.snapshot_id == inventory.latest_good_id),
            None,
        )
        return GuardianStats(latest, len(inventory.snapshots), len(inventory.quarantine), len(inventory.problems))

    return HomeSummary(
        codex=codex,
        sync=sync,
        open_journals=open_journals,
        backups=part("backups", backups),
        copies=part("copies", copies),
        copies_configured=cfg.state_backup.root_dir is not None,
        guardian=part("guardian", guardian),
        automation=part("automation", lambda: automation(config_path)),
        state=read_state_stats(config_path, cache_root=cache_root),
        errors=errors,
    )


# --- the cache of expensive counts ----------------------------------------------


def _cache_file(config_path: Path, cache_root: Path | None) -> Path:
    key = str(Path(config_path).resolve())
    if os.name == "nt":
        key = key.casefold()
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return (cache_root or cache_dir()) / f"home-{digest}.json"


def state_stats_from_directory(directory: ChatDirectory, *, now: datetime | None = None) -> StateStats:
    moment = now or datetime.now(timezone.utc)
    chats = directory.top_level()
    return StateStats(
        computed_at_utc=moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        chats=len(chats),
        sub_threads=len(directory.chats) - len(chats),
        archived=sum(1 for chat in chats if chat.state is SessionState.ARCHIVED),
        projects=len(directory.projects),
        chats_without_project=sum(1 for chat in chats if chat.association is Association.NONE),
        chats_via_mapping=sum(1 for chat in chats if chat.association is Association.DERIVED_VIA_MAPPING),
        session_bytes=sum(chat.byte_count for chat in directory.chats),
    )


def write_state_stats(config_path: Path, stats: StateStats, *, cache_root: Path | None = None) -> bool:
    """Keep the counts for the next time the page opens; ``False`` if it could not."""
    target = _cache_file(config_path, cache_root)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        staged.write_text(json.dumps({"format": CACHE_FORMAT, **asdict(stats)}, indent=2) + "\n", encoding="utf-8")
        replace_with_retry(staged, target)
    except OSError:
        return False
    return True


def read_state_stats(config_path: Path, *, cache_root: Path | None = None) -> StateStats | None:
    """The cached counts, or ``None`` when there are none or they cannot be trusted."""
    try:
        raw = json.loads(_cache_file(config_path, cache_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("format") != CACHE_FORMAT:
        return None
    try:
        values = {name: raw[name] for name in StateStats.__dataclass_fields__}
    except KeyError:
        return None
    if not isinstance(values["computed_at_utc"], str) or not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for name, value in values.items() if name != "computed_at_utc"
    ):
        return None
    return StateStats(**values)
