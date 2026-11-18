"""Read-only descriptions of a saved session plan and its journal."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .config import load_config
from .exceptions import FailSafeError
from .mutation_journal import JournalStore, MutationJournal
from .semantic_transfer import TransferPlan, load_transfer_plan, transfer_direction


@dataclass(frozen=True)
class TaskItem:
    title: str
    session_id: str
    direction: str
    path: str
    state: str


def task_rows(plan: TransferPlan, params: dict | None, *, completed: bool = False) -> tuple[TaskItem, ...]:
    """Describe the plan without changing it; engine order is local then mirror."""
    params = params or {}
    ordered = [item for side in ("local", "mirror") for item in plan.items if transfer_direction(item) == side]
    ranks = {id(item): index for index, item in enumerate(ordered)}
    known = "staged" in params or "applied" in params or completed
    staged = len(ordered) if completed else params.get("staged", 0)
    applied = len(ordered) if completed else params.get("applied", 0)
    staged = staged if isinstance(staged, int) and staged >= 0 else 0
    applied = applied if isinstance(applied, int) and applied >= 0 else 0
    failed = params.get("relative_path")
    failed_hash = params.get("session_hash")
    phase = params.get("phase")
    matching_paths = sum(1 for item in plan.items if item.target_relative_path and item.target_relative_path == failed)
    rows = []
    for item in plan.items:
        direction = transfer_direction(item) or ""
        path = item.target_relative_path or ""
        rank = ranks.get(id(item))
        if item.action.is_blocked:
            state = "blocked"
        elif rank is None:
            state = "skipped"
        elif phase in {"stage_chats", "apply_chats", "verify_chats", "delete_chat_files"} and (
            (failed_hash and item.session_hash == failed_hash)
            or (not failed_hash and path and path == failed and matching_paths == 1)
        ):
            state = "failed"
        elif not known:
            state = "unknown"
        elif rank < applied:
            state = "applied"
        elif rank < staged:
            state = "staged"
        else:
            state = "waiting"
        match = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", Path(path).name)
        rows.append(TaskItem("", match.group(0)[:8] if match else "", direction, path, state))
    return tuple(rows)


def filter_task_rows(rows: tuple[TaskItem, ...], category: str) -> tuple[TaskItem, ...]:
    if category == "all":
        return rows
    if category == "not_applied":
        return tuple(row for row in rows if row.state != "applied")
    return tuple(row for row in rows if row.state == category)


def load_latest_session_task(config_path: Path) -> tuple[TransferPlan, MutationJournal] | None:
    """Read a plan only when its id matches its journal; never guess an old plan."""
    cfg = load_config(config_path)
    store = JournalStore(cfg.paths.temp_dir)
    journals = []
    if store.root.is_dir():
        for path in store.root.glob("*.json"):
            try:
                journal = store.load(path.stem)
            except FailSafeError:
                continue
            if journal.operation_id == path.stem and journal.family == "sessions":
                journals.append(journal)
    root = cfg.paths.workspace_root_dir or cfg.paths.backup_dir.parent
    plans = root / "plans"
    if not journals:
        return None
    journal = max(journals, key=lambda value: value.created_at_utc)
    for path in plans.glob("*.json"):
        try:
            plan = load_transfer_plan(path)
        except (OSError, ValueError, AttributeError):
            continue
        if plan.plan_id == journal.plan_hash:
            return plan, journal
    return None
