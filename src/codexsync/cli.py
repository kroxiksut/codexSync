from __future__ import annotations

import argparse
import logging
import json
import sys
import time
from pathlib import Path

from .app import (
    CONFLICT_POLICIES,
    HandoffStatus,
    ProjectSyncResult,
    sync_projects,
    refresh_thread_catalogue,
    check_codex,
    repair_codex,
    sync_chat_names,
    check_project_files,
    handoff_status,
    run_handoff,
    watch_handoff,
    AutomationRun,
    AutomationView,
    apply_automation,
    automation_status,
    build_context,
    create_config,
    create_codex_backup,
    list_codex_backups,
    apply_project_move_plan,
    apply_repair_projects,
    apply_session_transfer,
    audit_session_index,
    build_guardian_runner,
    collect_process_snapshot,
    inspect_recovery,
    list_history,
    list_journals,
    project_move_notes,
    move_chats,
    print_preflight_report,
    record_branch_resolution,
    record_format_migrations,
    save_transfer_plan,
    scan_chats,
    scan_session_transfer,
    build_working_set,
    load_session_scope,
    write_working_set,
    print_plan,
    remove_automation,
    restore_from_backup,
    restore_global_state,
    accept_guardian_baseline,
    run_automation_job,
    run_preflight,
    run_sync,
    save_project_move_plan,
    scan_project_move,
    scan_repair_projects,
    validate_config_only,
    apply_config_migration,
    check_config_migration,
    preview_config_migration,
    change_config_value,
    change_path_mappings,
    preview_full_sync,
    close_codex_for_sync,
    SYNC_DIRECTIONS,
    list_backup_snapshots,
    list_config_history,
    list_sync_candidates,
    parse_config_value,
    read_home_summary,
    read_working_set,
    recount_state,
    remove_config_value,
    suggest_path_mappings,
)
from .chat_directory import Association, ChatDirectory, ChatEntry, search_chats
from .chat_move import ChatMovePlan
from .config import SYNC_SCOPES, load_config
from .config_locations import (
    ConfigChoice,
    choose_config_path,
    frozen_executable_dir,
    read_config_pointer,
)
from .exceptions import ConfigError, ConflictError, FailSafeError, SafetyPreconditionError
from .exit_codes import ExitCode
from .guardian_inventory import read_guardian_inventory
from .logging_setup import configure_logging
from .models import AppConfig, LoggingConfig
from .recovery import resume_operation, rollback_operation
from .app import _UNRESOLVED_TRANSFER_BLOCKS
from .repair_plan import save_repair_plan
from .safety_gate import OperationKind
from .scheduler import render_scheduler_templates, write_scheduler_templates
from .semantic_transfer import CWD_ABSENT_HERE, FORMAT_MIGRATION, OLDER_FORMAT_HAS_LATER_RECORDS, transfer_direction
from .system_scheduler import BROKEN_TASK_CODES, FOREIGN_TASK, LEGACY_TASK
from .version import __version__

LOG = logging.getLogger(__name__)


def safe_print(line: str) -> None:
    """Print a line that may contain anything a person typed into a chat.

    Chat titles are arbitrary text and the console this ships to is often a
    legacy Windows code page, so a box-drawing character in somebody's message
    would otherwise end the command with a UnicodeEncodeError. Degrading those
    characters is right here: the row is a pointer to a chat, not its content.
    """
    stream = sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode(encoding, errors="replace").decode(encoding, errors="replace"))


#: What each association means for the person reading the list. The point of
#: showing it at all is that the three behave differently when a project moves,
#: and one of them is not visible to Codex at all.
_ASSOCIATION_MARK = {
    Association.BOUND: "pinned",
    Association.DERIVED: "by path",
    Association.DERIVED_VIA_MAPPING: "by rule!",
    Association.NONE: "-",
}


def _chat_row(chat: ChatEntry) -> str:
    when = (chat.timestamp or "")[:10] or "??????????"
    mark = _ASSOCIATION_MARK[chat.association]
    title = chat.title or "(no opening message)"
    return f"{when}  {chat.short_id:<8}  {chat.record_count:>5}  {mark:<9}  {title}"


def _chat_payload(directory: ChatDirectory, chat: ChatEntry) -> dict:
    project = directory.projects.get(chat.project_id or "")
    return {
        "session_id": chat.session_id,
        "relative_path": chat.relative_path,
        "timestamp": chat.timestamp,
        "records": chat.record_count,
        "kind": chat.kind.value,
        "association": chat.association.value,
        "project_id": chat.project_id,
        "project_name": project.name if project else None,
        "cwd": chat.cwd,
        "parent_id": chat.parent_id,
        "title": chat.title,
        "codes": list(chat.codes),
    }


def _print_legend(directory: ChatDirectory) -> None:
    if directory.volatile:
        print("  state: VOLATILE (Codex is running; a chat open right now is still being written)")
    stranded = [chat for chat in directory.chats if chat.association is Association.DERIVED_VIA_MAPPING]
    if stranded:
        print(
            f"  {len(stranded)} chat(s) marked 'by rule!' reach their project only through "
            "[[path_mappings]]. Codex does not read those rules, so it shows them under no "
            "project until they are pinned."
        )


def print_chat_move(plan: ChatMovePlan, written: int, *, applied: bool) -> None:
    """Say what the move would do, or did, and how to confirm it."""
    moving = plan.writing_actions
    print(f"Chat move plan {plan.plan_id}")
    print(f"  target project: {plan.to_project_id}")
    print(f"  chats named: {len(plan.actions)}   bindings to write: {len(moving)}")
    for action in plan.actions:
        origin = action.from_project_id or "no project"
        print(
            f"    {action.session_id.split('-', 1)[0]}  {action.kind.value:<13} "
            f"from {origin} ({action.from_association.value})"
        )
    for code in plan.codes:
        print(f"  code: {code}")
    if plan.codes:
        return
    if applied:
        print(f"Chat move finished. bindings_written={written}")
        return
    if not moving:
        print("  Nothing to write: every chat named is already under that project.")
        return
    print()
    print("  Nothing was written. Close Codex and repeat the command with:")
    print(f"    --confirm {plan.plan_id}")
    print("  The id covers the current state, so it stops matching if anything changes.")


def print_chat_list(
    directory: ChatDirectory, chats: tuple[ChatEntry, ...], *, as_json: bool = False
) -> None:
    if as_json:
        print(json.dumps(
            {
                "volatile": directory.volatile,
                "schema_id": directory.schema_id,
                "matched": len(chats),
                "chats": [_chat_payload(directory, chat) for chat in chats],
            },
            ensure_ascii=False, indent=2,
        ))
        return
    safe_print(f"Chats: {len(chats)}")
    _print_legend(directory)
    if not chats:
        return
    print()
    for chat in chats:
        project = directory.projects.get(chat.project_id or "")
        label = project.name if project and project.name else (chat.project_id or "no project")
        safe_print(f"  {_chat_row(chat)}")
        safe_print(f"      {label}   {chat.cwd or ''}")


def print_chat_tree(
    directory: ChatDirectory,
    *,
    project: str | None = None,
    include_sub_threads: bool = False,
    include_invalid: bool = False,
    as_json: bool = False,
) -> None:
    """Projects with their chats underneath, and the strays at the end.

    A chat with no project is not an error to hide: after a move between
    machines it is the normal state of every chat that came along, so the
    unassigned group is part of the answer rather than a footnote.
    """
    groups: list[tuple[str, str | None]] = []
    for view in sorted(
        directory.projects.values(), key=lambda item: (item.name or item.project_id).casefold()
    ):
        groups.append((view.name or view.project_id, view.project_id))
    groups.append(("(no project)", None))

    if project is not None:
        wanted = {item.project_id for item in directory.project_named(project)}
        if project.casefold() in {"none", "-"}:
            wanted = {None}
        groups = [entry for entry in groups if entry[1] in wanted]

    rendered = []
    for label, project_id in groups:
        chats = [
            chat for chat in search_chats(
                directory, include_sub_threads=include_sub_threads, include_invalid=include_invalid,
            )
            if chat.project_id == project_id
        ]
        view = directory.projects.get(project_id or "")
        rendered.append((label, project_id, view, chats))

    if as_json:
        print(json.dumps(
            {
                "volatile": directory.volatile,
                "schema_id": directory.schema_id,
                "projects": [
                    {
                        "project_id": project_id,
                        "name": view.name if view else None,
                        "roots": list(view.roots) if view else [],
                        "chats": [_chat_payload(directory, chat) for chat in chats],
                    }
                    for _, project_id, view, chats in rendered
                ],
            },
            ensure_ascii=False, indent=2,
        ))
        return

    total = sum(len(chats) for _, _, _, chats in rendered)
    # The `(no project)` bucket is a group but not a project: counting it makes
    # the state look like it holds one project more than it does.
    project_count = sum(1 for _, project_id, _, _ in rendered if project_id is not None)
    print(f"Projects: {project_count}   chats: {total}")
    _print_legend(directory)
    for label, _, view, chats in rendered:
        roots = "  ".join(view.roots) if view and view.roots else ""
        print()
        safe_print(f"{label}   {roots}".rstrip())
        if not chats:
            print("    (no chats)")
            continue
        for chat in chats:
            safe_print(f"    {_chat_row(chat)}")
            for child in directory.children_of(chat.session_id):
                if include_sub_threads:
                    safe_print(f"        |- {_chat_row(child)}")
            count = len(directory.children_of(chat.session_id))
            if count and not include_sub_threads:
                safe_print(f"        |- {count} sub-thread(s)")


def _yes_no(value: bool | None, unknown: str = "unknown") -> str:
    if value is None:
        return unknown
    return "yes" if value else "no"


def print_automation_status(view: AutomationView) -> None:
    """`[scheduler]` as saved, then what the operating system actually has.

    The two halves are printed separately on purpose: the config is the only
    source of truth, and the OS task is merely its applied form, so a reader
    must be able to see at a glance when the second no longer matches the first.
    """
    print("Automation ([scheduler] in config.toml)")
    print(f"  enabled: {_yes_no(view.enabled)}")
    print(f"  mode: {view.mode}")
    print(f"  interval_seconds: {view.interval_seconds}")
    print(f"  run_at_login: {_yes_no(view.run_at_login)}")
    print(f"  startup_delay_seconds: {view.startup_delay_seconds}")
    print(f"  jitter_seconds: {view.jitter_seconds}")
    # A JSON list is the one rendering that is exact on every platform: no
    # shell's quoting rules are needed to read back where an argument ends.
    safe_print(f"  argv: {json.dumps(list(view.argv), ensure_ascii=False)}")
    print(f"  ignored_settings: {', '.join(view.ignored) if view.ignored else '(none)'}")
    print(f"  sync_at_login: {_yes_no(view.sync_at_login)}")
    safe_print(f"  login_argv: {json.dumps(list(view.login_argv), ensure_ascii=False)}")
    login = view.login_status
    if login is None:
        safe_print(f"  login_task: {view.login_status_error or 'the scheduler could not be asked'}")
    elif not login.installed:
        print("  login_task: not installed")
    else:
        safe_print(
            f"  login_task: {login.task_name}; last run {login.last_run_utc or 'never'}; "
            f"last result {login.last_result if login.last_result is not None else 'none'}"
        )
    print("Copies of the Codex state ([state_backup] in config.toml)")
    safe_print(f"  root_dir: {view.backup_root if view.backup_root is not None else '(not chosen)'}")
    print(f"  at_login: {_yes_no(view.backup_at_login)}")
    print(f"  interval_hours: {view.backup_interval_hours}")
    print(f"  keep: {view.backup_keep}")
    safe_print(f"  backup_argv: {json.dumps(list(view.backup_argv), ensure_ascii=False)}")
    backup = view.backup_status
    if backup is None:
        safe_print(f"  backup_task: {view.backup_status_error or 'the scheduler could not be asked'}")
    elif not backup.installed:
        print("  backup_task: not installed")
    else:
        safe_print(
            f"  backup_task: {backup.task_name}; last run {backup.last_run_utc or 'never'}; "
            f"last result {backup.last_result if backup.last_result is not None else 'none'}"
            + ("" if backup.definition_matches is not False else "; differs from config.toml")
        )
    print("Handoff between machines ([handoff] in config.toml)")
    safe_print(f"  root_dir: {view.handoff_root if view.handoff_root is not None else '(not chosen)'}")
    print(f"  enabled: {_yes_no(view.handoff_enabled)}")
    print(f"  delivery_wait_minutes: {view.handoff_wait_minutes}")
    print(f"  notify: {_yes_no(view.handoff_notify)}")
    safe_print(f"  handoff_argv: {json.dumps(list(view.handoff_argv), ensure_ascii=False)}")
    watcher = view.handoff_status
    if watcher is None:
        safe_print(f"  handoff_task: {view.handoff_status_error or 'the scheduler could not be asked'}")
    elif not watcher.installed:
        print("  handoff_task: not installed")
    else:
        safe_print(
            f"  handoff_task: {watcher.task_name}; starts at sign-in"
            + ("" if watcher.definition_matches is not False else "; differs from config.toml")
        )
    for task in view.legacy_tasks:
        safe_print(
            f"Task left by codexSync 0.1: {task.name} -- it still runs `codexsync sync` on its own timer, "
            "which in 0.2 carries settings only, never chats."
        )
        safe_print(f"  remove it: {task.remove_command}")
        print("  then use [handoff] enabled = true and `codexsync automation apply` for the full sync.")
    print("Operating system task")
    status = view.status
    if status is None:
        safe_print(f"  status_error: {view.status_error or 'the scheduler could not be asked'}")
        return
    print(f"  installed: {_yes_no(status.installed)}")
    if status.installed:
        print(f"  enabled: {_yes_no(status.enabled)}")
        print(f"  definition_matches: {_yes_no(status.definition_matches)}")
        if view.reports_run_times:
            print(f"  last_run_utc: {status.last_run_utc or '(never)'}")
            print(f"  next_run_utc: {status.next_run_utc or '(none scheduled)'}")
        else:
            print("  last_run_utc: (not reported by this platform)")
            print("  next_run_utc: (not reported by this platform)")
        print(f"  last_result: {'(none)' if status.last_result is None else status.last_result}")
    if status.installed and status.task_name:
        safe_print(f"  task_name: {status.task_name}")
    if status.owner:
        safe_print(f"  owner: {status.owner}{'' if status.owned_by_me else ' (not this account)'}")
    if status.installed_command:
        safe_print(f"  runs: {json.dumps(list(status.installed_command), ensure_ascii=False)}")
    if status.codes:
        print(f"  codes: {', '.join(status.codes)}")
    if status.detail:
        safe_print(f"  detail: {status.detail}")
    print_task_advice(view)


def print_task_advice(view: AutomationView) -> None:
    """One line saying what to do, when the installed task is not what it should be.

    A task whose executable moved -- which is what an upgraded frozen install
    leaves behind -- fails every run while looking installed, so the advice
    names that case separately from a task that is merely out of date.
    """
    status = view.status
    if status is None:
        return
    if FOREIGN_TASK in status.codes:
        print(
            "  This task belongs to another account. codexSync will not change or remove it; "
            "its own task is installed under a separate name."
        )
        return
    broken = [code for code in status.codes if code in BROKEN_TASK_CODES]
    if broken:
        print(
            f"  The installed task cannot run as registered ({', '.join(broken)}); "
            "run `codexsync automation apply` to point it at this installation."
        )
        return
    if LEGACY_TASK in status.codes:
        print(
            "  This task was installed under the shared name used before per-account tasks; "
            "run `codexsync automation apply` to re-register it under this account's own name."
        )
        return
    if view.enabled != status.installed or (status.installed and status.definition_matches is False):
        print("  The task does not match [scheduler]; run `codexsync automation apply` to fix that.")


#: `automation run` statuses that are a success. BUSY means another Guardian
#: already holds the lock, which `guardian snapshot` also treats as success.
_AUTOMATION_OK = frozenset({"COMMITTED", "UNCHANGED", "PASSED", "DRY_RUN_FINISHED", "BUSY"})


def print_automation_run(run: AutomationRun) -> int:
    label = "SKIPPED_ACTIVE_GUARDIAN" if run.status == "BUSY" else run.status
    print(f"Automation run: {label}")
    print(f"  mode: {run.mode}")
    if run.detail:
        safe_print(f"  detail: {run.detail}")
    if run.actions is not None:
        print(f"  actions: {run.actions}")
    if run.status in _AUTOMATION_OK:
        return int(ExitCode.OK)
    if run.status == "QUARANTINED":
        return int(ExitCode.CONFLICT_DETECTED)
    # FAILED, and any status this shell does not know: never report success.
    return int(ExitCode.FAIL_SAFE)


class _ArgumentParser(argparse.ArgumentParser):
    """argparse, except that a malformed command line exits 4, not 2.

    argparse reports every usage error with exit status 2, which `AI_RULES`
    reserves for a conflict: a scheduled task or script reading the code would
    take `sync --bogus` for "both sides changed" (CS-315). Subparsers inherit
    the class, so every subcommand's errors take the same door.
    """

    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(int(ExitCode.BAD_INPUT), f"{self.prog}: error: {message}\n")


def _conflict_policy_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--conflict-policy", choices=CONFLICT_POLICIES, default=None,
        help="For this run, decide conflicts of settings files and chats by this rule instead of "
        "[conflict] policy: prefer_newer_mtime keeps the newer copy (a chat: its later last "
        "record), prefer_local this machine's, prefer_cloud the cloud's, manual_abort stops. "
        "The copy not kept is backed up first (a chat: whole, in the conflict bundle)",
    )
    parser.add_argument(
        "--direction", choices=SYNC_DIRECTIONS, default=None,
        help="For this run, instead of [sync] direction: bidirectional, to_cloud (this machine "
        "only sends; a chat changed on both keeps this machine's copy) or to_local (only receives)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="codexsync", description="codexSync CLI")
    parser.add_argument(
        "-c", "--config", default=None,
        help=(
            "Path to TOML config. Without it: the config the window last opened, "
            "then config.toml here, then beside the executable; never a location "
            "nobody created"
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose logging (includes redacted process snapshot metadata)",
    )
    # `-V`, because `-v` has meant `--verbose` since 0.1. A build that cannot
    # be asked what it is, is a build whose version nobody checks: both exes
    # reported `0.0.0+unknown` for a full release cycle because the only place
    # the version appeared was the window's About screen (CS-265).
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"codexsync {__version__}",
        help="Print the version and exit",
    )
    terminate_mode = parser.add_mutually_exclusive_group()
    terminate_mode.add_argument(
        "--manual-terminate-confirmation",
        dest="manual_terminate_confirmation_override",
        action="store_const",
        const=True,
        help="Removed in 0.2: Codex is never terminated by codexSync",
    )
    terminate_mode.add_argument(
        "--auto-terminate-without-confirmation",
        dest="manual_terminate_confirmation_override",
        action="store_const",
        const=False,
        help="Removed in 0.2: Codex is never terminated by codexSync",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "validate",
        help="Validate config: it loads, its folders are apart, and mutating commands accept it",
    )
    for diagnostic_name in ("doctor", "preflight"):
        diagnostic = sub.add_parser(diagnostic_name, help="Run read-only environment diagnostics")
        diagnostic.add_argument(
            "--for",
            dest="diagnostic_for",
            choices=["guardian", "sync", "restore", "repair"],
            default="guardian" if diagnostic_name == "doctor" else "sync",
            help="Operation profile to assess; no write probes are performed",
        )
    sub.add_parser("plan", help="Build and print sync plan")

    config_cmd = sub.add_parser(
        "config",
        help="Inspect, edit and upgrade config.toml itself",
        description=(
            "Edit config.toml the way the window's Settings do, and report what this version "
            "would write differently in it. Every write keeps comments and every value it does "
            "not name, passes the same checks as the loader, is refused if the file changed "
            "meanwhile, and copies the replaced file into config-history/."
        ),
    )
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)
    config_check = config_sub.add_parser(
        "check", help="Report what this version would change (read-only)"
    )
    config_upgrade = config_sub.add_parser(
        "upgrade", help="Apply a confirmed plan to config.toml"
    )
    for sub_parser in (config_check, config_upgrade):
        sub_parser.add_argument(
            "--include-defaults",
            action="store_true",
            help="Also report sections a newer version introduced whose absence changes nothing",
        )
        sub_parser.add_argument(
            "--skip",
            action="append",
            default=[],
            metavar="CODE",
            help="Leave this finding alone (repeatable); a blocker cannot be skipped",
        )
    config_upgrade.add_argument(
        "--confirm-plan",
        required=True,
        help="Plan id printed by `config check`; refused if the file changed since",
    )
    config_set = config_sub.add_parser(
        "set",
        help="Set one value, as the Settings page does (comments kept, old file in config-history/)",
    )
    config_set.add_argument("name", metavar="SECTION.KEY", help="For example conflict.policy")
    config_set.add_argument(
        "value", metavar="VALUE",
        help='A TOML value (true, 600, ["sessions", "skills"]); text needs no quotes',
    )
    config_unset = config_sub.add_parser(
        "unset", help="Remove one value, so the built-in default applies again",
    )
    config_unset.add_argument("name", metavar="SECTION.KEY")
    for sub_parser in (config_set, config_unset):
        sub_parser.add_argument(
            "--dry-run", action="store_true", help="Print the change and check it; write nothing",
        )
    config_mapping = config_sub.add_parser("mapping", help="Path mapping rules ([[path_mappings]])")
    mapping_sub = config_mapping.add_subparsers(dest="mapping_command", required=True)
    mapping_list = mapping_sub.add_parser("list", help="List the rules; writes nothing")
    mapping_suggest = mapping_sub.add_parser(
        "suggest", help="Folders chats name that are missing here, and project roots; reads only",
    )
    mapping_add = mapping_sub.add_parser("add", help="Add one rule")
    mapping_add.add_argument("--id", required=True, dest="rule_id", help="A unique name for the rule")
    mapping_add.add_argument("--source-machine", required=True, help="The machine the path comes from")
    mapping_add.add_argument("--target-machine", required=True, help="The machine it is read on")
    mapping_add.add_argument("--from", required=True, dest="from_prefix", help="Folder on the source machine")
    mapping_add.add_argument("--to", required=True, dest="to_prefix", help="The same folder on the target machine")
    mapping_add.add_argument(
        "--case-sensitive", choices=("true", "false"), default=None,
        help="Compare the folder with case; left out, it follows the path's platform",
    )
    mapping_remove = mapping_sub.add_parser("remove", help="Remove one rule by its id")
    mapping_remove.add_argument("--id", required=True, dest="rule_id")
    for sub_parser in (mapping_add, mapping_remove):
        sub_parser.add_argument(
            "--dry-run", action="store_true", help="Print the change and check it; write nothing",
        )
    for sub_parser in (mapping_list, mapping_suggest):
        sub_parser.add_argument("--json", action="store_true", dest="as_json", help="Machine-readable output")
    config_roots = config_sub.add_parser(
        "roots",
        help="One level of what .codex and the cloud copy hold, to choose targets.include_roots; reads only",
    )
    config_roots.add_argument("path", nargs="?", default="", metavar="PATH", help="Folder to list, relative")
    config_history = config_sub.add_parser("history", help="Saved versions of config.toml; reads only")
    for sub_parser in (config_roots, config_history):
        sub_parser.add_argument("--json", action="store_true", dest="as_json", help="Machine-readable output")
    init_cfg = sub.add_parser(
        "init-config",
        help="Write config.toml from the packaged template",
        description=(
            "Write config.toml from the packaged template. With --machine-id, "
            "--local-state-dir and --workspace-root the values are filled in and "
            "validated before anything is written; every template comment is kept."
        ),
    )
    init_cfg.add_argument(
        "--output",
        default="config.toml",
        help="Output path for generated config file",
    )
    init_cfg.add_argument(
        "--force",
        action="store_true",
        help="Overwrite output file if it already exists (plain template only)",
    )
    init_cfg.add_argument("--machine-id", default=None, help="[identity] machine_id for this machine")
    init_cfg.add_argument("--local-state-dir", default=None, help="[paths] local_state_dir: the Codex state directory")
    init_cfg.add_argument("--workspace-root", default=None, help="[paths] workspace_root_dir")
    init_cfg.add_argument("--cloud-root", default=None, help="[paths] cloud_root_dir (optional)")

    sync = sub.add_parser("sync", help="Run synchronization")
    mode_group = sync.add_mutually_exclusive_group()
    mode_group.add_argument("--dry-run", action="store_true", help="Force dry-run mode")
    mode_group.add_argument("--apply", action="store_true", help="Apply changes (overrides dry-run)")
    sync.add_argument(
        "--unattended",
        action="store_true",
        help="Nobody is watching (the sign-in task); recorded as the run's origin",
    )
    sync.add_argument(
        "--scope", choices=SYNC_SCOPES, default=None,
        help=(
            "What to carry for this run instead of [sync] scope: full -- settings, chats and "
            "projects, as the window's Synchronise -- or settings, the files under "
            "targets.include_roots only"
        ),
    )
    _conflict_policy_argument(sync)

    restore = sub.add_parser("restore", help="Restore files from backup snapshot")
    restore.add_argument("--from", dest="snapshot", default=None, help="Snapshot directory name in backup_dir")
    restore.add_argument(
        "--allow-legacy-snapshot",
        action="store_true",
        help="Allow one explicitly named legacy directory/zip after read-only inventory",
    )
    restore.add_argument(
        "--target",
        choices=["local", "cloud"],
        default="local",
        help="Restore destination root",
    )
    restore_mode = restore.add_mutually_exclusive_group()
    restore_mode.add_argument("--dry-run", action="store_true", help="Force dry-run mode")
    restore_mode.add_argument("--apply", action="store_true", help="Apply restore (overrides dry-run)")

    guardian = sub.add_parser("guardian", help="Maintain immutable snapshots of the global Codex JSON state")
    guardian_sub = guardian.add_subparsers(dest="guardian_command", required=True)
    guardian_sub.add_parser("watch", help="Continuously observe .codex-global-state.json")
    guardian_snapshot = guardian_sub.add_parser("snapshot", help="Take one stable Guardian snapshot")
    guardian_snapshot.add_argument("--once", action="store_true", required=True, help="Run one bounded snapshot pipeline")
    guardian_sub.add_parser("list", help="List this machine's snapshots and quarantine; writes nothing")
    guardian_restore = guardian_sub.add_parser(
        "restore", help="Put a verified snapshot back into .codex-global-state.json (preview without --confirm)"
    )
    guardian_restore.add_argument("--snapshot", required=True, help="Snapshot id from `guardian list`")
    guardian_restore.add_argument(
        "--confirm", default=None, dest="confirm_plan",
        help="Plan id from the preview; without it nothing is written",
    )
    guardian_restore.add_argument(
        "--dry-run", action="store_true",
        help=(
            "With --confirm: run every check, including the process gate, and write nothing. "
            "Without --confirm the command is already a preview and checks no gate"
        ),
    )
    guardian_accept = guardian_sub.add_parser(
        "accept",
        help="Accept the current state as the new baseline after a suspicious shrink (preview without --confirm)",
        description=(
            "Guardian quarantines a state that lost many projects or bindings, and keeps "
            "comparing later states with the baseline from before the drop. When the drop is "
            "real -- Codex re-created its projects, say -- nothing ever becomes latest-good "
            "again. This shows why the counts fell and, with --confirm, commits the current "
            "state as latest-good. Only a shrink or a change of schema can be accepted, never a damaged state. "
            "Allowed while Codex is open; writes only into the Guardian root."
        ),
    )
    guardian_accept.add_argument(
        "--confirm", default=None, dest="confirm_plan",
        help="Plan id from the preview; without it nothing is written",
    )
    guardian_scheduler = guardian_sub.add_parser(
        "scheduler",
        help="DEPRECATED: render fallback scheduler templates; use `automation apply`",
        description=(
            "Deprecated. Renders task templates only and installs nothing. The "
            "scheduled task is the applied form of [scheduler] in config.toml: "
            "set it there and run `codexsync automation apply`."
        ),
    )
    guardian_scheduler.add_argument("--platform", choices=["windows", "macos", "linux"], required=True)
    guardian_scheduler.add_argument("--output-dir", required=True)
    guardian_scheduler.add_argument("--log-dir", required=True)
    guardian_scheduler.add_argument("--interval", type=int, default=60)

    automation = sub.add_parser(
        "automation",
        help="Show, apply or run the scheduled task defined by [scheduler] in config.toml",
    )
    automation_sub = automation.add_subparsers(dest="automation_command", required=True)
    automation_sub.add_parser(
        "status", help="Print [scheduler] and the operating system task; changes nothing"
    )
    automation_sub.add_parser(
        "apply", help="Install, update or remove the task so it matches [scheduler] as saved"
    )
    automation_sub.add_parser(
        "remove", help="Remove the task; config.toml is left unchanged"
    )
    automation_sub.add_parser(
        "run", help="Run the configured safe job once, now, in this process"
    )

    project_move = sub.add_parser(
        "project-move", help="Copy a project to a new folder and point Codex at it; the old folder is kept"
    )
    project_move_sub = project_move.add_subparsers(dest="project_move_command", required=True)
    project_move_scan = project_move_sub.add_parser(
        "scan", help="Hash the project and plan the move; writes only the plan file"
    )
    project_move_scan.add_argument("--project", required=True, help="Project id, or a name that is unique")
    project_move_scan.add_argument("--to", required=True, dest="new_root", help="New folder; must not exist yet")
    project_move_scan.add_argument("--save-plan", default=None, help="Save the plan for a later apply")
    project_move_apply = project_move_sub.add_parser(
        "apply", help="Copy, verify, then remap the project root (Codex must be closed)"
    )
    project_move_apply.add_argument("--plan", required=True)
    project_move_apply.add_argument("--confirm-plan", required=True)
    project_move_apply.add_argument(
        "--dry-run", action="store_true", help="Run every check and copy nothing"
    )

    repair = sub.add_parser("repair-projects", help="Analyze or apply JSON-only project repairs")
    repair_sub = repair.add_subparsers(dest="repair_command", required=True)
    repair_scan = repair_sub.add_parser("scan", help="Build a read-only immutable repair plan")
    repair_scan.add_argument("--source-machine", required=True)
    repair_scan.add_argument("--target-machine", required=True)
    repair_scan.add_argument("--output", default=None, help="Optional redacted JSON report path")
    repair_scan.add_argument("--save-plan", default=None, help="Save the complete protected plan for a later apply")
    repair_apply = repair_sub.add_parser("apply", help="Apply one exact cold JSON-only repair plan")
    repair_apply.add_argument("--plan", required=True)
    repair_apply.add_argument("--confirm-plan", required=True)
    repair_apply.add_argument(
        "--dry-run", action="store_true",
        help="Run every check and report the approved action count without writing",
    )

    sessions = sub.add_parser("sessions", help="Analyze session branches across two machines")
    sessions_sub = sessions.add_subparsers(dest="sessions_command", required=True)
    sessions_scan = sessions_sub.add_parser(
        "scan", help="Build a read-only branch transfer plan; writes nothing"
    )
    sessions_scan.add_argument("--source-machine", required=True)
    sessions_scan.add_argument("--target-machine", required=True)
    sessions_scan.add_argument("--resolutions", default=None, help="Recorded conflict decisions to apply")
    sessions_scan.add_argument("--output", default=None, help="Optional redacted JSON report path")
    sessions_scan.add_argument("--save-plan", default=None, help="Save the frozen plan for a later apply")
    sessions_scan.add_argument(
        "--project", action="append", default=[],
        help="Working set: bring this project's chats into .codex (repeatable, id or name)",
    )
    sessions_scan.add_argument(
        "--chat", action="append", default=[],
        help="Working set: bring this chat too (repeatable, session id or its start)",
    )
    sessions_scan.add_argument(
        "--scope-file", default=None,
        help="Working set stored earlier; combined with any --project/--chat given here",
    )
    sessions_scan.add_argument(
        "--save-scope", action="store_true",
        help="Remember this working set for this pair of machines",
    )
    _conflict_policy_argument(sessions_scan)
    sessions_sub.add_parser(
        "index",
        help="Report what each side's session_index.jsonl contains; writes nothing",
    )
    sessions_scope = sessions_sub.add_parser(
        "scope", help="Show the working set stored for a pair of machines; writes nothing",
    )
    sessions_scope.add_argument("--source-machine", required=True)
    sessions_scope.add_argument("--target-machine", required=True)
    sessions_scope.add_argument(
        "--expand", action="store_true", help="Also count the chats it covers now (reads every chat)",
    )
    sessions_scope.add_argument("--json", action="store_true", dest="as_json", help="Machine-readable output")
    sessions_resolve = sessions_sub.add_parser(
        "resolve", help="Record one versioned choice between two divergent branches"
    )
    sessions_resolve.add_argument("--plan", required=True)
    sessions_resolve.add_argument("--conflict", default=None)
    sessions_resolve.add_argument(
        "--choice", default=None, choices=["KEEP_LOCAL", "KEEP_REMOTE", "DEFER"]
    )
    sessions_resolve.add_argument(
        "--format-migrations", action="store_true",
        help="Keep the newer record format for every conflict that is only a format rewrite",
    )
    sessions_resolve.add_argument("--output", required=True, help="Resolutions file to create or extend")
    sessions_catalogue = sessions_sub.add_parser(
        "catalogue",
        help="Chat files Codex does not list; with --confirm-plan, ask Codex to rebuild its chat list",
    )
    sessions_catalogue.add_argument(
        "--confirm-plan", default=None, help="Plan id from the preview; Codex must be closed"
    )
    sessions_catalogue.add_argument(
        "--dry-run", action="store_true", help="Run every check without writing"
    )
    sessions_catalogue.add_argument("--json", dest="as_json", action="store_true")
    sessions_names = sessions_sub.add_parser(
        "names",
        help="Chat names other machines show; with --confirm-plan, set them on chats unnamed here",
    )
    sessions_names.add_argument(
        "--confirm-plan", default=None, help="Plan id from the preview; Codex must be closed"
    )
    sessions_names.add_argument("--dry-run", action="store_true", help="Run every check without writing")
    sessions_names.add_argument("--json", dest="as_json", action="store_true")
    sessions_apply = sessions_sub.add_parser(
        "apply", help="Apply one exact cold session transfer plan"
    )
    sessions_apply.add_argument("--plan", required=True)
    sessions_apply.add_argument("--confirm-plan", required=True)
    sessions_apply.add_argument("--resolutions", default=None, help="Decisions the plan was built with")
    sessions_apply.add_argument(
        "--dry-run", action="store_true",
        help="Run every check and report the write count without writing",
    )

    chats = sub.add_parser("chats", help="Find chats and see which project each one is in")
    chats_sub = chats.add_subparsers(dest="chats_command", required=True)
    for name, help_text in (
        ("list", "Search chats and print them one per line"),
        ("tree", "Print projects with their chats underneath"),
    ):
        command = chats_sub.add_parser(name, help=help_text)
        command.add_argument("--project", default=None, help="Project name, id or prefix; 'none' for unassigned")
        command.add_argument("--sub-threads", action="store_true", help="Include threads agents spawned")
        command.add_argument("--invalid", action="store_true", help="Include sessions the catalog rejected")
        command.add_argument("--source-machine", default=None, help="Machine the chats were recorded on")
        command.add_argument("--target-machine", default=None, help="This machine, for [[path_mappings]]")
        command.add_argument("--json", action="store_true", dest="as_json", help="Machine-readable output")
        if name == "list":
            command.add_argument("--text", default=None, help="Substring of the opening message or the directory")
            command.add_argument("--association", default=None, choices=[item.value for item in Association])
            command.add_argument("--since", default=None, help="ISO timestamp lower bound")
            command.add_argument("--until", default=None, help="ISO timestamp upper bound")
            command.add_argument("--limit", type=_non_negative_int, default=50, help="Maximum rows (0 for no limit)")

    chats_move = chats_sub.add_parser(
        "move", help="Put chosen chats under one project (needs Codex closed to write)"
    )
    chats_move.add_argument(
        "--chat", action="append", required=True, dest="chat_refs",
        help="Chat id or a unique prefix of one; repeat for several",
    )
    chats_move.add_argument("--to", required=True, dest="to_project", help="Project name or id")
    chats_move.add_argument(
        "--confirm", default=None, dest="confirm_plan",
        help="Plan id from the preview; without it nothing is written",
    )
    chats_move.add_argument(
        "--dry-run", action="store_true",
        help=(
            "With --confirm: run every check, including the process gate, and report the write "
            "count without writing. Without --confirm the command is already a preview"
        ),
    )
    chats_move.add_argument("--sub-threads", action="store_true", help="Allow naming a spawned thread")
    chats_move.add_argument("--source-machine", default=None)
    chats_move.add_argument("--target-machine", default=None)

    projects_cmd = sub.add_parser(
        "projects", help="Carry the project list between machines (also part of `handoff sync`)"
    )
    projects_sub = projects_cmd.add_subparsers(dest="projects_command", required=True)
    projects_sync = projects_sub.add_parser(
        "sync",
        help=(
            "Merge other machines' projects into this one and publish this machine's list; "
            "without --confirm-plan it only previews"
        ),
    )
    projects_sync.add_argument(
        "--confirm-plan", default=None, help="Plan id from the preview; without it nothing is written",
    )
    projects_sync.add_argument(
        "--dry-run", action="store_true",
        help="With --confirm-plan: run every check, including the process gate, and write nothing",
    )
    projects_sync.add_argument("--json", action="store_true", dest="as_json", help="Machine-readable output")
    projects_files = projects_sub.add_parser(
        "files",
        help="Whether this machine's project folders hold what other machines last had; writes nothing",
    )
    projects_files.add_argument("--all", action="store_true", help="List every project, not only warnings")
    projects_files.add_argument("--json", action="store_true", dest="as_json", help="Machine-readable output")

    state_backup = sub.add_parser(
        "state-backup",
        help="Copies of the valuable part of the Codex state directory ([state_backup] in config.toml)",
    )
    state_backup_sub = state_backup.add_subparsers(dest="state_backup_command", required=True)
    state_backup_create = state_backup_sub.add_parser(
        "create", help="Take one verified copy now; refused while Codex is open unless --wait is given",
    )
    state_backup_create.add_argument(
        "--wait", action="store_true",
        help="If Codex is open, wait until it closes (up to 23 hours) instead of refusing",
    )
    state_backup_list = state_backup_sub.add_parser("list", help="List the copies in the folder; writes nothing")
    state_backup_list.add_argument("--json", action="store_true", dest="as_json", help="Machine-readable output")

    backups = sub.add_parser(
        "backups", help="Backups a sync or restore takes before overwriting (paths.backup_dir)",
    )
    backups_sub = backups.add_subparsers(dest="backups_command", required=True)
    backups_list = backups_sub.add_parser(
        "list", help="List the snapshots, newest first; the name is what `restore --from` takes",
    )
    backups_list.add_argument("--json", action="store_true", dest="as_json", help="Machine-readable output")

    summary = sub.add_parser(
        "summary", help="What is kept and how it stands -- the window's Home page; reads only",
    )
    summary.add_argument(
        "--recount", action="store_true",
        help="Count chats and projects now (reads every chat) instead of the last count kept",
    )
    summary.add_argument("--json", action="store_true", dest="as_json", help="Machine-readable output")

    handoff = sub.add_parser(
        "handoff",
        help="Hand work from one machine to the next ([handoff] in config.toml)",
    )
    handoff_sub = handoff.add_subparsers(dest="handoff_command", required=True)
    handoff_status_cmd = handoff_sub.add_parser(
        "status", help="Say which machine is working, what it handed off and what arrived; writes nothing",
    )
    handoff_status_cmd.add_argument(
        "--check-delivery", action="store_true",
        help="Also hash the cloud copy to say how much of each pending handoff has arrived",
    )
    handoff_status_cmd.add_argument("--json", action="store_true", dest="as_json", help="Machine-readable output")
    handoff_sync = handoff_sub.add_parser(
        "sync",
        help="Load what other machines handed off, then hand off this one: settings and chats, "
        "conflicts decided by [conflict] policy. Codex must be closed",
    )
    _conflict_policy_argument(handoff_sync)
    handoff_sync.add_argument(
        "--wait-minutes", type=int, default=None,
        help="Minutes to wait for another machine's handoff to arrive (default: handoff.delivery_wait_minutes)",
    )
    handoff_sync.add_argument(
        "--accept-undelivered", action="store_true",
        help="Load even if another machine's handoff has not fully arrived (only when you know it never will)",
    )
    handoff_sub.add_parser(
        "watch",
        help="Run until signed out: load at start, hand off whenever Codex closes (what the task runs)",
    )

    history = sub.add_parser(
        "history", help="List past mutating runs (sync, sessions, ...) from their journals; writes nothing"
    )
    history.add_argument(
        "--family", default="all",
        help=(
            "Operation family to list (sync, sessions, project-sync, chats, restore, repair, ...) or "
            "'all' (default: all -- a full sync writes one run each for settings, chats and projects)"
        ),
    )
    history.add_argument("--limit", type=int, default=20, help="Newest runs to show (default: 20; 0 = all)")
    history.add_argument("--json", action="store_true", dest="as_json", help="Machine-readable output")

    codex = sub.add_parser(
        "codex", help="Check why Codex may not start or show its work, and repair what can be repaired",
    )
    codex_sub = codex.add_subparsers(dest="codex_command", required=True)
    codex_check = codex_sub.add_parser("check", help="List what is wrong with Codex's state; reads only")
    codex_check.add_argument("--json", action="store_true", dest="as_json", help="Machine-readable output")
    codex_repair = codex_sub.add_parser(
        "repair",
        help="Preview, or with --confirm-plan apply, the repair `codex check` offers; Codex must be closed",
    )
    codex_repair.add_argument("--confirm-plan", default=None, help="Plan id from the preview")
    codex_repair.add_argument("--dry-run", action="store_true", help="Run every check without writing")
    codex_repair.add_argument("--json", action="store_true", dest="as_json", help="Machine-readable output")

    recover = sub.add_parser("recover", help="Inspect and clear interrupted mutation evidence")
    recover_sub = recover.add_subparsers(dest="recover_command", required=True)
    recover_list = recover_sub.add_parser(
        "list", help="List mutation journals still open (they block every sync), without side effects"
    )
    recover_list.add_argument("--all", action="store_true", help="Include finished journals too")
    recover_list.add_argument("--json", action="store_true", dest="as_json", help="Print JSON")
    recover_inspect = recover_sub.add_parser("inspect", help="Read one mutation journal without side effects")
    recover_inspect.add_argument("operation_id")
    recover_resume = recover_sub.add_parser(
        "resume", help="Close an interrupted journal so its command can be run again"
    )
    recover_resume.add_argument("operation_id")
    recover_resume_mode = recover_resume.add_mutually_exclusive_group()
    recover_resume_mode.add_argument("--dry-run", action="store_true", help="Report only (default)")
    recover_resume_mode.add_argument("--apply", action="store_true", help="Close the journal")
    recover_rollback = recover_sub.add_parser(
        "rollback", help="Restore the snapshot an interrupted operation created, then close it"
    )
    recover_rollback.add_argument("operation_id")
    recover_rollback.add_argument(
        "--target", choices=["local", "cloud"], default=None,
        help=(
            "Each file goes back to the side its backup recorded. Given, it must be the only side "
            "the snapshot holds; needed only for a restore snapshot that does not record its side"
        ),
    )
    recover_rollback_mode = recover_rollback.add_mutually_exclusive_group()
    recover_rollback_mode.add_argument("--dry-run", action="store_true", help="Report only (default)")
    recover_rollback_mode.add_argument("--apply", action="store_true", help="Perform the rollback")

    return parser



def _run_config_command(args: argparse.Namespace, config_path: Path) -> int:
    """`config check` and `config upgrade`: the config's own migration.

    `check` never writes and always exits 0 -- it is a report, like `plan`.
    `upgrade` needs the id `check` printed, and that id covers the file's
    bytes, so a config edited in between stops the write instead of being
    overwritten by a plan built for a different file.
    """
    if args.config_command not in {"check", "upgrade"}:
        return _run_config_edit(args, config_path)
    plan, diff = preview_config_migration(
        config_path, include_defaults=args.include_defaults, skip=tuple(args.skip)
    )
    if args.config_command == "check":
        _print_config_plan(plan, diff, config_path)
        return int(ExitCode.OK)

    outcome = apply_config_migration(
        config_path,
        confirm_plan_id=args.confirm_plan,
        skip=tuple(args.skip),
        include_defaults=args.include_defaults,
    )
    print(f"Config upgraded: {outcome.saved.path}")
    print(f"Applied: {', '.join(outcome.applied) or 'nothing'}")
    if outcome.skipped:
        print(f"Skipped: {', '.join(outcome.skipped)}")
    if outcome.saved.history_entry is not None:
        print(f"Previous version kept: {outcome.saved.history_entry}")
    return int(ExitCode.OK)


def _run_config_edit(args: argparse.Namespace, config_path: Path) -> int:
    """`config set|unset|mapping|roots|history`: the Settings page, from the console."""
    command = args.config_command
    if command == "set":
        value = parse_config_value(args.name, args.value)
        return _print_config_change(change_config_value(config_path, args.name, value, dry_run=args.dry_run))
    if command == "unset":
        return _print_config_change(remove_config_value(config_path, args.name, dry_run=args.dry_run))
    if command == "history":
        entries = list_config_history(load_config(config_path), config_path)
        if args.as_json:
            print(json.dumps([
                {"name": item.name, "path": str(item.path), "size": item.size,
                 "created_utc": item.created_utc.strftime("%Y-%m-%dT%H:%M:%SZ")}
                for item in entries
            ], sort_keys=True, indent=2))
        elif not entries:
            print("No saved versions of config.toml yet.")
        else:
            for item in entries:
                print(f"{item.created_utc:%Y-%m-%d %H:%M:%S}Z  {item.size:>8} B  {item.path}")
        return int(ExitCode.OK)
    if command == "roots":
        return _print_sync_roots(load_config(config_path), args.path, as_json=args.as_json)
    if args.mapping_command == "list":
        return _print_mappings(load_config(config_path), as_json=args.as_json)
    if args.mapping_command == "suggest":
        return _print_mapping_hints(suggest_path_mappings(config_path), as_json=args.as_json)
    if args.mapping_command == "add":
        rule = {
            "rule_id": args.rule_id, "source_machine": args.source_machine, "target_machine": args.target_machine,
            "from": args.from_prefix, "to": args.to_prefix,
            "case_sensitive": None if args.case_sensitive is None else args.case_sensitive == "true",
        }
        return _print_config_change(change_path_mappings(config_path, add=rule, dry_run=args.dry_run))
    return _print_config_change(change_path_mappings(config_path, remove=args.rule_id, dry_run=args.dry_run))


def _run_full_sync(args: argparse.Namespace, config_path: Path) -> int:
    """`sync` at `[sync] scope = "full"` (D-028): the window's Synchronise.

    Settings, chats and projects, as `handoff sync` carries them; a dry run
    builds every plan and writes nothing.
    """
    dry_run = load_config(config_path).sync.dry_run_default
    if args.dry_run:
        dry_run = True
    if args.apply:
        dry_run = False
    origin = "unattended" if args.unattended else "cli"
    if not dry_run:
        _print_handoff_result(run_handoff(
            config_path, origin=origin, conflict_policy=args.conflict_policy, direction=args.direction,
            on_wait=lambda late: print(
                "Waiting for the cloud: " + ", ".join(f"{item.machine} {item.arrived}/{item.total}" for item in late)
            ),
        ))
        return int(ExitCode.OK)
    preview = preview_full_sync(config_path, conflict_policy=args.conflict_policy, direction=args.direction)
    run_sync(preview.files, dry_run=True, origin=origin)
    plan = preview.chats
    to_cloud = sum(1 for item in plan.items if transfer_direction(item) == "mirror")
    to_codex = sum(1 for item in plan.items if transfer_direction(item) == "local")
    decide = [item for item in plan.blocked_items if item.action in _UNRESOLVED_TRANSFER_BLOCKS]
    print(f"Full sync dry run on {preview.machine} (chats paired with {preview.source}); nothing was written.")
    print(f"  files: {preview.files.plan.action_count} action(s)")
    print(f"  chats: {to_cloud} to the cloud copy, {to_codex} into .codex")
    if decide:
        print(
            f"  chats that need a decision first: {len(decide)} "
            "(the run would stop before writing; `sessions scan` lists them)"
        )
    if preview.projects is None:
        print("  projects: not carried from this state (see the log)")
    else:
        print(
            f"  projects: {len(preview.projects.plan.added)} to add, "
            f"{preview.projects.plan.action_count} change(s) to the project list"
        )
    if preview.waiting_for:
        print(
            "  waiting for the cloud to deliver: " + ", ".join(preview.waiting_for)
            + " (a real run waits up to [handoff] delivery_wait_minutes)"
        )
    print("Apply with: codexsync -c " + str(config_path) + " sync --apply")
    return int(ExitCode.OK)


def _print_handoff_result(result) -> None:
    """What a full sync did: `handoff sync`, and `sync` at `[sync] scope = "full"`."""
    if getattr(result, "codex_closed", False):
        print("Codex was open and quit when asked ([sync] close_codex).")
    print(f"Handoff finished on {result.machine}.")
    print(f"  loaded from: {', '.join(result.taken) or 'nothing new'}")
    print(f"  files written: {result.sync_actions}, chats written: {result.session_actions}")
    if result.chats_decided_by_rule:
        print(
            f"  chats changed on both machines, decided by [conflict] policy: "
            f"{result.chats_decided_by_rule} (the copy not kept is in the conflict bundle)"
        )
    if result.new_chats_written:
        print(
            f"  chats new to this machine written into Codex: {result.new_chats_written} "
            "(start Codex, then `doctor` says whether it lists them: session_visibility)"
        )
    if result.new_chats_kept_in_cloud:
        print(
            f"  chats new to this machine kept in the cloud copy: {result.new_chats_kept_in_cloud} "
            '(set [semantic] new_chats = "same_path" to copy them into Codex)'
        )
    others = result.chats_not_loaded - result.new_chats_kept_in_cloud
    if others > 0:
        print(
            f"  other chats left in the cloud copy only: {others} "
            "(`sessions scan` says why for each)"
        )
    print(
        f"  projects added: {result.projects_added}, project list changes: {result.project_changes}"
    )
    if result.projects_missing_folders:
        print(
            f"  added projects whose folder does not exist here: {result.projects_missing_folders} "
            "(create the folder, or add a [[path_mappings]] rule)"
        )
    if result.chat_names_set or result.chat_names_kept or result.chat_names_waiting:
        print(
            f"  chat names taken from other machines: {result.chat_names_set}"
            f" (named differently here, kept: {result.chat_names_kept};"
            f" waiting for Codex to list the chat: {result.chat_names_waiting})"
        )
    if result.project_files_behind:
        print("  project folders that did not come along (`projects files` lists every file):")
        _print_project_files(result.project_files_behind, limit=20, indent="    ")
    if result.chats_codex_will_list:
        print(
            f"  chats Codex lists after its next start: {result.chats_codex_will_list} "
            "(it rebuilds its chat list from the files; that start takes longer -- leave Codex open "
            "until the chats appear)"
        )
    if result.chats_codex_does_not_list:
        print(
            f"  chat files Codex does not list: {result.chats_codex_does_not_list} "
            "(the files are in place; `sessions catalogue` asks Codex to rebuild its chat list)"
        )
    if result.chats_codex_ignores:
        print(
            f"  chat files Codex still does not list after rebuilding: {result.chats_codex_ignores} "
            "(the files are in place; `sessions catalogue` lists them)"
        )
    for name in result.steps_not_done:
        print(f"  not done this time, the next sync tries again (the log says why): {name}")
    print(
        "  handed off: " + (result.record.handoff_id if result.handed_off else "nothing new")
    )


def _print_backups(snapshots, config_path: Path, *, as_json: bool) -> int:
    """`backups list`: what `restore --from` can take, newest first."""
    if as_json:
        print(json.dumps([
            {"name": item.name, "committed": item.committed, "legacy": item.legacy,
             "compressed": item.compressed, "entries": item.entries, "total_bytes": item.total_bytes,
             "machine": item.machine, "created_utc": item.created_utc, "modified_utc": item.modified_utc,
             "problem": item.problem}
            for item in snapshots
        ], sort_keys=True, indent=2))
        return int(ExitCode.OK)
    if not snapshots:
        print("No backups yet.")
        return int(ExitCode.OK)
    for item in snapshots:
        state = "ok" if item.committed else ("legacy" if item.legacy else f"not usable: {item.problem}")
        size = "?" if item.total_bytes is None else f"{item.total_bytes} B"
        files = "?" if item.entries is None else str(item.entries)
        print(f"{item.created_utc or item.modified_utc}  {item.name}  ({item.machine or 'unknown machine'}, "
              f"{files} file(s), {size}, {state})")
    print(f"Restore one: codexsync -c {config_path} restore --from NAME --dry-run")
    return int(ExitCode.OK)


def _print_scope(scope, args: argparse.Namespace, *, as_json: bool) -> int:
    """`sessions scope`: the working set stored for a pair of machines."""
    expanded = args.expand and not scope.is_empty
    if as_json:
        payload = {"projects": list(scope.projects), "chats": list(scope.chats), "empty": scope.is_empty}
        if expanded:
            payload.update(
                chat_count=scope.chat_count, total_bytes=scope.total_bytes,
                not_in_catalog=len(scope.not_in_catalog),
            )
        print(json.dumps(payload, sort_keys=True, indent=2))
        return int(ExitCode.OK)
    pair = f"{args.source_machine} -> {args.target_machine}"
    if scope.is_empty:
        print(f"No working set stored for {pair}: every chat is brought into .codex.")
        return int(ExitCode.OK)
    print(f"Working set for {pair}:")
    for project in scope.projects:
        print(f"  project: {project}")
    for chat in scope.chats:
        print(f"  chat: {chat}")
    if expanded:
        print(f"  covers now: {scope.chat_count} chat(s), {scope.total_bytes} B")
        if scope.not_in_catalog:
            print(f"  not in this machine's thread catalogue: {len(scope.not_in_catalog)}")
    return int(ExitCode.OK)


def _print_summary(summary, *, as_json: bool) -> int:
    """`summary`: the Home page's tiles, one line each."""
    if as_json:
        def plain(value):
            if hasattr(value, "__dataclass_fields__"):
                return {name: plain(getattr(value, name)) for name in value.__dataclass_fields__}
            if isinstance(value, (list, tuple)):
                return [plain(item) for item in value]
            if isinstance(value, dict):
                return {str(key): plain(item) for key, item in value.items()}
            if value is None or isinstance(value, (bool, int, float, str)):
                return value
            return str(value)

        print(json.dumps(plain(summary), sort_keys=True, indent=2))
        return int(ExitCode.OK)
    print(f"Codex: {summary.codex} (as seen now; a write checks again at that moment)")
    sync = summary.sync
    if sync is not None:
        last = sync.last
        print(
            f"Sync: last {last.created_at_utc if last else 'never'}"
            + (f" ({last.state})" if last else "")
            + f"; last 7 days {sync.runs} run(s), {sync.failed} not finished; files to cloud {sync.to_cloud}, "
            f"to .codex {sync.to_local}; chats to cloud {sync.chats_to_cloud}, to .codex {sync.chats_to_local}"
        )
    if summary.open_journals:
        print(f"Open journals: {summary.open_journals} (`recover list` says what closes each)")
    for title, copies, on in (
        ("Backups", summary.backups, True),
        ("Copies of .codex", summary.copies, summary.copies_configured),
    ):
        if not on:
            print(f"{title}: off ([state_backup] root_dir is not chosen)")
        elif copies is not None:
            print(f"{title}: {copies.count}, {copies.bytes} B, newest {copies.newest_utc or '-'}")
    guardian = summary.guardian
    if guardian is not None:
        print(
            f"Guardian: latest good {guardian.latest_good_utc or 'none'}, {guardian.snapshots} snapshot(s), "
            f"{guardian.quarantined} quarantined, {guardian.problems} problem(s)"
        )
    view = summary.automation
    if view is not None:
        installed = [
            name for name, status in (
                ("periodic", view.status), ("sign-in sync", view.login_status),
                ("copy of .codex", view.backup_status), ("handoff watcher", view.handoff_status),
            ) if status is not None and status.installed
        ]
        print("Automation: " + (", ".join(installed) + " installed" if installed else "no task installed"))
    state = summary.state
    if state is None:
        print("Chats: not counted yet (`summary --recount`)")
    else:
        print(
            f"Chats: {state.chats} ({state.archived} archived, {state.sub_threads} sub-threads), "
            f"{state.projects} project(s), {state.chats_without_project} without a project, "
            f"{state.chats_via_mapping} reachable only through a path mapping; {state.session_bytes} B; "
            f"counted {state.computed_at_utc}"
        )
    for part, reason in sorted(summary.errors.items()):
        print(f"Not read: {part}: {reason}")
    return int(ExitCode.OK)


def _print_config_change(change) -> int:
    if not change.changed:
        print(f"{change.path} already says so; nothing to change.")
        return int(ExitCode.OK)
    print(change.diff, end="")
    if change.saved is None:
        print("Dry run: nothing written. The edit passes the checks a save makes.")
        return int(ExitCode.OK)
    print(f"Saved: {change.saved.path}")
    if change.saved.history_entry is not None:
        print(f"Previous version kept: {change.saved.history_entry}")
    return int(ExitCode.OK)


def _print_mappings(cfg: AppConfig, *, as_json: bool) -> int:
    rules = cfg.path_mappings
    if as_json:
        print(json.dumps([
            {"rule_id": rule.rule_id, "source_machine": rule.source_machine,
             "target_machine": rule.target_machine, "from": rule.source_prefix, "to": rule.target_prefix,
             "case_sensitive": rule.case_sensitive}
            for rule in rules
        ], sort_keys=True, indent=2))
    elif not rules:
        print("No path mapping rules.")
    else:
        for rule in rules:
            case = "" if rule.case_sensitive is None else f"  (case_sensitive = {str(rule.case_sensitive).lower()})"
            print(f"{rule.rule_id}: {rule.source_machine} {rule.source_prefix} -> "
                  f"{rule.target_machine} {rule.target_prefix}{case}")
    return int(ExitCode.OK)


def _print_mapping_hints(hints, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps({
            "machines": list(hints.machines),
            "chat_roots": [{"folder": folder, "chats": count} for folder, count in hints.chat_roots],
            "unmapped_roots": list(hints.unmapped_roots),
            "local_project_roots": list(hints.local_project_roots),
            "remote_project_roots": list(hints.remote_project_roots),
        }, sort_keys=True, indent=2))
        return int(ExitCode.OK)
    print("Machines: " + (", ".join(hints.machines) or "(none named yet)"))
    print("Folders chats name that do not exist here (what a rule is for):")
    for folder in hints.unmapped_roots:
        print(f"  {folder}  ({hints.chats_under(folder)} chat(s))")
    if not hints.unmapped_roots:
        print("  (none)")
    for title, folders in (
        ("Project folders that exist here:", hints.local_project_roots),
        ("Project folders that do not exist here:", hints.remote_project_roots),
    ):
        print(title)
        for folder in folders or ("(none)",):
            print(f"  {folder}")
    print(
        "Add a rule: codexsync config mapping add --id NAME --source-machine A --target-machine B "
        "--from FOLDER --to FOLDER"
    )
    return int(ExitCode.OK)


def _print_sync_roots(cfg: AppConfig, path: str, *, as_json: bool) -> int:
    """One level of both sides, with why an entry cannot be chosen."""
    chosen = {root.strip("/") for root in cfg.targets.include_roots}
    items = list_sync_candidates(cfg, path)

    def included(relative: str) -> bool:
        return any(relative == root or relative.startswith(root + "/") for root in chosen)

    if as_json:
        print(json.dumps([
            {"path": item.relative, "dir": item.is_dir, "local": item.local, "cloud": item.cloud,
             "included": included(item.relative), "semantic_owned": item.semantic_owned,
             "excluded_by_glob": item.excluded_by_glob}
            for item in items
        ], sort_keys=True, indent=2))
        return int(ExitCode.OK)
    if not items:
        print("Nothing here on either side.")
        return int(ExitCode.OK)
    print("  L C  path   (L = in .codex, C = in the cloud copy, * = in targets.include_roots)")
    for item in items:
        notes = []
        if item.semantic_owned:
            notes.append("carried by the chat sync, never copied as a file")
        if item.excluded_by_glob:
            notes.append("excluded by filters.exclude_globs")
        mark = "*" if included(item.relative) else " "
        name = item.relative + ("/" if item.is_dir else "")
        print(f"{mark} {'L' if item.local else '-'} {'C' if item.cloud else '-'}  {name}"
              + (f"   ({'; '.join(notes)})" if notes else ""))
    return int(ExitCode.OK)


def _print_config_plan(plan, diff: str, config_path: Path) -> None:
    if plan.is_current:
        print(f"Config matches this version: {config_path}")
        return
    print(f"Config written for an earlier version: {config_path}")
    print(f"Plan id: {plan.plan_id}")
    for finding in plan.findings:
        mark = "optional" if finding.optional else "required"
        print(f"  [{finding.level}/{mark}] {finding.code}")
        print(f"      {finding.detail}")
        for edit in finding.edits:
            print(f"      -> {edit.describe()}")
        if not finding.edits:
            print("      -> reported only; nothing is changed automatically")
    if diff:
        print()
        print(diff)
    if plan.fixable:
        print()
        print(f"Apply with: codexsync -c {config_path} config upgrade --confirm-plan {plan.plan_id}")


def _warn_about_outdated_config(config_path: Path, command: str) -> None:
    """One line, before the command runs, when the config is from an older version.

    A mutating command refuses such a config anyway; a read-only one works but
    may be reading fewer Codex processes than this version knows about. Either
    way the user is told once, here, and nothing is changed for them.
    """
    if command in {"config", "init-config", "validate"}:
        return
    try:
        plan = check_config_migration(config_path)
    except Exception:  # the command itself reports an unreadable config
        return
    if plan.is_current:
        return
    if plan.blockers:
        LOG.warning(
            "Config is from an earlier version and every mutating command will refuse it (%s). "
            "Run `config check` to see the fix.",
            ", ".join(finding.code for finding in plan.blockers),
        )
        return
    LOG.warning(
        "Config is from an earlier version (%s). Run `config check` to see what would change.",
        ", ".join(plan.codes()),
    )



def _non_negative_int(text: str) -> int:
    """An argparse type for a count where a negative number means nothing.

    `chats list --limit -3` used to slice ``[:-3]`` and drop the last three
    rows instead of refusing (CS-325).
    """
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {value}")
    return value


def _read_scope_file(path: Path):
    """A working set the user named with ``--scope-file``, or a refusal.

    `load_session_scope` reads a missing file as an empty set, which is right
    for the per-pair file nobody has saved yet and wrong here: a typo in the
    name silently dropped the working set, and the apply then wrote every
    session into `.codex` (CS-317).
    """
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ConfigError(f"--scope-file names no file: {resolved}")
    try:
        return load_session_scope(resolved)
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError) as exc:
        raise ConfigError(f"--scope-file is not a stored working set: {resolved} ({exc})") from exc


def _resolve_config_path(explicit: str | None) -> ConfigChoice:
    """Which config this run works on, and where it was found.

    Without `-c` the search is the window's: the config the window last opened
    (its pointer file), this directory, then the folder of a frozen executable.
    Before that the default was the literal `config.toml`, so a machine set up
    through the window answered every command run from another directory with
    "Config file not found", while the window on the same machine opened it
    happily.

    The choice is returned rather than announced here: this runs before logging
    is configured, and a notice written to an unconfigured logger is a notice
    nobody reads.
    """
    return choose_config_path(
        explicit, read_config_pointer(), executable_dir=frozen_executable_dir()
    )


def _require_config_path(choice: ConfigChoice) -> Path:
    """The chosen path, or a refusal that says how to name one.

    Nothing found is not a reason to invent a location: where the config lives
    is the user's decision.
    """
    if choice.path is None:
        raise ConfigError(
            "No config.toml found: pass -c <path>, run from the folder that holds "
            "config.toml, or open or create one in the window (the command line "
            "then finds it too)."
        )
    return choice.path


def _announce_config_choice(choice: ConfigChoice) -> None:
    """Name a config that was found somewhere other than the current folder.

    A command that quietly works on a file the user is not looking at is worse
    than one that says it cannot find anything.
    """
    if choice.source not in ("explicit", "cwd") and choice.exists:
        LOG.info("Using config %s (found in the %s location)", choice.path, choice.source)


def _history_record(run) -> dict:
    return {
        "operation_id": run.operation_id,
        "family": run.family,
        "state": run.state,
        "readable": run.readable,
        "created_at_utc": run.created_at_utc,
        "finished_at_utc": run.finished_at_utc,
        "origin": run.origin,
        "action_count": run.action_count,
        "counts": dict(run.counts) if run.counts is not None else None,
        "failure": run.failure,
        "backup_snapshot": run.backup_snapshot,
    }


def _print_history(runs) -> None:
    """One line per run, newest first. Dry runs write no journal and are absent."""
    if not runs:
        print("No runs recorded.")
        return
    for run in runs:
        state = run.state if run.readable and run.state else "UNREADABLE"
        if run.failure:
            state = f"{state} ({run.failure})"
        if run.counts:
            changes = " ".join(f"{key}={value}" for key, value in sorted(run.counts.items()))
        else:
            changes = f"actions={run.action_count if run.action_count is not None else '?'}"
        print(
            f"{run.created_at_utc or '?'}  {run.family or '?'}  {state}  "
            f"origin={run.origin or '-'}  {changes}  backup={run.backup_snapshot or '-'}"
        )


#: Said wherever Codex is asked to rebuild its chat list (D-032).
_REBUILD_ADVICE = (
    "  On its next start Codex walks every chat file before it opens; leave it open until the chats "
    "appear. If it shows \"could not load your organization's settings\", run `codexsync codex check`."
)

#: One sentence per `codex check` finding; counts fill the braces.
_FINDING_TEXT = {
    "CATALOGUE_REBUILD_STUCK": (
        "Codex will not start: a rebuild of its chat list ({chats} chat files, {megabytes} MB) was ended "
        "part-way and nobody is doing it. `codex repair` sets it back to complete."
    ),
    "CATALOGUE_REBUILD_PENDING": (
        "Codex rebuilds its chat list on its next start ({chats} chat files, {megabytes} MB): leave it open "
        "until the chats appear. `codex repair` skips the rebuild instead."
    ),
    "CATALOGUE_REBUILDING": "Codex is rebuilding its chat list: leave it open until the chats appear.",
    "CATALOGUE_REBUILD_UNDETERMINED": (
        "A rebuild of Codex's chat list is marked, and whether Codex is running could not be determined; "
        "close Codex and check again."
    ),
    "CATALOGUE_UNREADABLE": "Codex's chat list could not be read; nothing about it was judged.",
    "CATALOGUE_MISSES_CHATS": (
        "Codex does not list {chats} chat file(s) that are in place. `sessions catalogue` asks it to "
        "rebuild its chat list."
    ),
    "GLOBAL_STATE_MISSING": (
        "Codex's global state (projects, pins, which chat is in which project) is missing. "
        "`guardian restore` puts a saved snapshot back."
    ),
    "GLOBAL_STATE_INVALID": (
        "Codex's global state does not validate, so projects or chats may be torn apart. "
        "`guardian restore` puts a saved snapshot back."
    ),
    "OPEN_JOURNAL": "{journals} codexSync operation(s) stopped part-way and block every write; see `recover list`.",
}


def _print_codex_health(health) -> None:
    state = health.process_state.value.lower()
    if not health.findings:
        print(f"Codex check: nothing wrong found (Codex {state}).")
        return
    print(f"Codex check (Codex {state}):")
    for item in health.findings:
        text = _FINDING_TEXT.get(item.code, item.code).format_map({"chats": 0, "megabytes": 0, "journals": 0, **item.counts})
        print(f"  [{item.severity.value}] {item.code}: {text}")
    if health.plan.writes:
        print(f"  repair plan id: {health.plan.plan_id}")
        print("  Close Codex and run `codex repair --confirm-plan <plan id>`.")


def _print_journals(journals, *, as_json: bool, include_finished: bool) -> None:
    """Open journals first, each with what it takes to close it."""
    if as_json:
        print(json.dumps([
            {
                **_history_record(item),
                "terminal": item.terminal,
                "machine_id": item.machine_id,
                "own": item.own,
                "closes_itself": item.closes_itself,
                "can_resume": item.can_resume,
                "can_rollback": item.can_rollback,
                "rollback_refusal": item.rollback_refusal,
            }
            for item in journals
        ], sort_keys=True, indent=2))
        return
    if not journals:
        print("No journals recorded." if include_finished else "No open journals: nothing blocks a sync.")
        return
    for item in journals:
        state = item.state if item.readable and item.state else "UNREADABLE"
        if item.failure:
            state = f"{state} ({item.failure})"
        machine = item.machine_id or ("this machine" if item.own else "not recorded")
        print(
            f"{item.operation_id}  {item.created_at_utc or '?'}  {item.family or '?'}  {state}  "
            f"machine={machine}  backup={item.backup_snapshot or '-'}"
        )
        if item.terminal:
            continue
        if item.closes_itself:
            print("    stopped before replacing anything; the next sync on this machine closes it by itself")
        elif not item.readable:
            print("    cannot be read; inspect the file in the journals folder before anything else")
        elif not item.own:
            print("    another machine's run; close it there, or here with `recover resume` once that run is over")
        else:
            exits = ["`recover resume`"] + (["`recover rollback`"] if item.can_rollback else [])
            print(f"    entered the commit phase; close it with {' or '.join(exits)}")
            if item.rollback_refusal:
                print(f"    rollback: {item.rollback_refusal}")


def _handoff_record_json(record) -> dict | None:
    if record is None:
        return None
    return {
        "machine": record.machine,
        "state": record.state,
        "state_since_utc": record.state_since_utc,
        "handoff_id": record.handoff_id,
        "generation": record.generation,
        "handed_off_at_utc": record.handed_off_at_utc,
        "files": len(record.files),
    }


def _handoff_status_json(status: HandoffStatus) -> dict:
    return {
        "machine": status.machine,
        "root": str(status.root),
        "enabled": status.enabled,
        "own": _handoff_record_json(status.own),
        "others": [_handoff_record_json(record) for record in status.others],
        "pending": list(status.pending),
        "working_elsewhere": list(status.working_elsewhere),
        "deliveries": [
            {"machine": item.machine, "handoff_id": item.handoff_id, "total": item.total,
             "arrived": item.arrived, "delivered": item.delivered}
            for item in status.deliveries
        ],
        "unreadable": list(status.unreadable),
    }


def _project_sync_json(result: ProjectSyncResult) -> dict:
    plan = result.plan
    return {
        "machine": result.machine,
        "folder": str(result.root),
        "plan_id": plan.plan_id,
        "from_machines": list(result.pending),
        "unreadable": list(result.unreadable),
        "volatile": result.volatile,
        "projects": [
            {
                "from": item.peer_machine,
                "action": item.kind.value,
                "name": item.name,
                "project_id": item.local_project_id,
                "roots": list(item.roots),
                "codes": list(item.codes),
            }
            for item in plan.items
        ],
        "pins_changed": plan.pins_changed,
        "order_changed": plan.order_changed,
        "bindings_written": plan.bindings_written,
        "written": result.written,
        "published": result.published,
    }


def _print_project_sync(result: ProjectSyncResult, *, applied: bool) -> None:
    plan = result.plan
    print(f"Projects on {result.machine}, from: {', '.join(result.pending) or 'no new list from another machine'}")
    for name in result.unreadable:
        print(f"  not believed: {name}")
    for item in plan.items:
        if item.kind.value == "MATCHED" and not item.codes:
            continue
        codes = f"  [{', '.join(item.codes)}]" if item.codes else ""
        print(f"  {item.kind.value:<9} {item.name or item.peer_project_id}  {'; '.join(item.roots)}{codes}")
    print(
        f"  added: {len(plan.added)}, already here: "
        f"{sum(1 for item in plan.items if item.kind.value == 'MATCHED')}, left alone: {len(plan.ambiguous)}; "
        f"pins {'change' if plan.pins_changed else 'kept'}, order {'changes' if plan.order_changed else 'kept'}, "
        f"chat bindings: {plan.bindings_written}"
    )
    if applied:
        print(f"  written: {result.written}; this machine's list published: {'yes' if result.published else 'unchanged'}")
    else:
        if not plan.writes:
            print("  Nothing to change here.")
        if result.volatile:
            print("  Codex is running: this is a preview. Close Codex, preview again, then apply.")
        print(f"  Plan id: {plan.plan_id}")
        print(
            f"  Apply with: codexsync projects sync --confirm-plan {plan.plan_id} "
            "(also publishes this machine's list for the others)"
        )


def _print_handoff_status(status: HandoffStatus) -> None:
    print(f"Handoff folder: {status.root}")
    print(f"This machine: {status.machine}  (watcher {'on' if status.enabled else 'off'})")
    records = ([status.own] if status.own is not None else []) + list(status.others)
    if not records:
        print("No machine has handed off yet.")
    for record in records:
        mark = "  (this machine)" if record.machine == status.machine else ""
        state = "working since" if record.state == "working" else "handed off, idle since"
        print(f"  {record.machine}{mark}: {state} {record.state_since_utc or '?'}")
        if record.handoff_id:
            print(f"    last handoff: {record.handed_off_at_utc or '?'}  ({len(record.files)} files)")
    for machine in status.working_elsewhere:
        print(f"WARNING: {machine} is working and has not handed off since.")
    delivered = {item.machine: item for item in status.deliveries}
    for machine in status.pending:
        item = delivered.get(machine)
        arrived = f" ({item.arrived} of {item.total} files arrived)" if item is not None else ""
        print(f"Not loaded here yet: the handoff from {machine}{arrived}.")
    for name in status.unreadable:
        print(f"WARNING: {name} cannot be read and is ignored.")


#: Commands a scheduled task runs besides `sync`, which also log to
#: `logging.file` (`system_scheduler._JOB_SUBCOMMANDS`).
_FILE_LOGGED_ONLY = frozenset({"guardian", "preflight", "state-backup", "handoff"})


_FILE_LABELS = (
    ("newer_there", "changed later there"),
    ("missing_here", "only there"),
    ("removed_there", "deleted there, still here"),
)


def _print_project_files(items, *, limit: int | None, indent: str = "  ") -> None:
    """One line per project and one per file that did not come along (D-026)."""
    for item in items:
        print(f"{indent}{item.verdict.value:<20} {item.name}  ({item.root}) against {item.peer}, "
              f"as of {item.peer_published_at_utc or '?'}")
        if item.chats_there_at:
            print(f"{indent}    chats of this project went on there until "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(item.chats_there_at))}")
        for field, label in _FILE_LABELS:
            paths = getattr(item, field)
            for path in paths if limit is None else paths[:limit]:
                print(f"{indent}    {label}: {path}")
            if limit is not None and len(paths) > limit:
                print(f"{indent}    ... and {len(paths) - limit} more {label}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config_choice = _resolve_config_path(args.config)
    config_path = config_choice.path
    try:
        if args.manual_terminate_confirmation_override is not None:
            raise ConfigError(
                "Process termination flags were removed in 0.2; close Codex manually and retry."
            )
        # Logging is configured with defaults first, then with file settings from config when context is built.
        configure_logging(LoggingConfig(level="INFO", file=None), verbose=args.verbose)
        _announce_config_choice(config_choice)
        if args.command != "init-config":
            config_path = _require_config_path(config_choice)
        _warn_about_outdated_config(config_path, args.command)
        cfg_for_verbose = None
        if args.command in _FILE_LOGGED_ONLY:
            # What a scheduled task runs, with nobody watching its console:
            # its log file is the only record it leaves (CS-318).
            try:
                configure_logging(load_config(config_path).logging, verbose=args.verbose)
            except ConfigError:
                pass  # Reported with its exit code by the command itself.
            except Exception as exc:
                LOG.warning("File logging setup failed, continue with default logger: %s", exc)
        if args.command in {"plan", "sync", "restore"}:
            try:
                cfg_for_verbose = load_config(config_path)
                configure_logging(cfg_for_verbose.logging, verbose=args.verbose)
                _emit_verbose_process_snapshot(args.verbose, cfg_for_verbose)
            except ConfigError:
                # Preserve existing behavior: detailed config errors are handled below.
                cfg_for_verbose = None
            except Exception as exc:
                LOG.warning("Verbose process snapshot setup failed, continue with default logger: %s", exc)
                if cfg_for_verbose is not None:
                    _emit_verbose_process_snapshot(args.verbose, cfg_for_verbose)

        if args.command == "config":
            return _run_config_command(args, config_path)

        if args.command == "validate":
            for note in validate_config_only(config_path):
                print(f"Note: {note}")
            print("Config is valid.")
            return int(ExitCode.OK)

        if args.command == "init-config":
            output_path = Path(args.output).expanduser()
            if _init_config_has_values(args):
                created = init_config_with_values(output_path, args)
                print(f"Config written: {created}")
                return int(ExitCode.OK)
            created = init_config_template(output_path=output_path, force=args.force)
            print(f"Config template written: {created}")
            return int(ExitCode.OK)

        if args.command == "automation":
            if args.automation_command == "status":
                print_automation_status(automation_status(config_path))
                return int(ExitCode.OK)
            if args.automation_command == "apply":
                view = apply_automation(config_path)
                print("Scheduled task installed or updated." if view.enabled else
                      "Scheduled task removed: [scheduler] enabled = false.")
                print_automation_status(view)
                return int(ExitCode.OK)
            if args.automation_command == "remove":
                existed = remove_automation(config_path)
                print("Scheduled task removed." if existed else "No scheduled task was installed.")
                print(
                    "  config.toml was not changed: while [scheduler] says enabled = true, "
                    "the next `codexsync automation apply` installs the task again."
                )
                return int(ExitCode.OK)
            if args.automation_command == "run":
                return print_automation_run(run_automation_job(config_path))

        if args.command in {"doctor", "preflight"}:
            operation = {
                "guardian": OperationKind.GUARDIAN_WATCH,
                "sync": OperationKind.SYNC,
                "restore": OperationKind.RESTORE,
                "repair": OperationKind.REPAIR_APPLY,
            }[args.diagnostic_for]
            report = run_preflight(config_path, operation=operation)
            print_preflight_report(report)
            if report.is_ok:
                print("Preflight checks passed.")
                return int(ExitCode.OK)
            print("Preflight checks failed.")
            return int(ExitCode.FAIL_SAFE)

        if args.command == "plan":
            ctx = build_context(
                config_path,
                manual_terminate_confirmation_override=args.manual_terminate_confirmation_override,
                enforce_safety=False,
            )
            print_plan(ctx.plan, volatile=ctx.volatile, direction=ctx.config.sync.direction)
            return int(ExitCode.OK)

        if args.command == "guardian" and args.guardian_command == "watch":
            outcome = build_guardian_runner(config_path).watch()
            if outcome.status == "BUSY":
                LOG.warning("Guardian watcher skipped: %s", outcome.detail)
                return int(ExitCode.OK)
            if outcome.status == "FAILED":
                LOG.error("Guardian watcher stopped: %s", outcome.detail)
                return int(ExitCode.FAIL_SAFE)
            return int(ExitCode.OK)

        if args.command == "guardian" and args.guardian_command == "snapshot":
            outcome = build_guardian_runner(config_path).once()
            if outcome.status in {"COMMITTED", "UNCHANGED", "BUSY"}:
                label = "SKIPPED_ACTIVE_GUARDIAN" if outcome.status == "BUSY" else outcome.status.value
                print(f"Guardian snapshot: {label}")
                return int(ExitCode.OK)
            if outcome.status == "QUARANTINED":
                print("Guardian snapshot: QUARANTINED")
                return int(ExitCode.CONFLICT_DETECTED)
            LOG.error("Guardian snapshot failed: %s", outcome.detail)
            return int(ExitCode.FAIL_SAFE)

        if args.command == "guardian" and args.guardian_command == "list":
            inventory = read_guardian_inventory(config_path)
            print(f"Guardian root: {inventory.root_dir}")
            print(f"  machine: {inventory.machine_id}")
            print(f"  latest-good: {inventory.latest_good_id or '(none)'}")
            for snapshot in inventory.snapshots:
                flags = "latest-good" if snapshot.latest_good else (
                    "verified" if snapshot.committed and snapshot.verified else "UNVERIFIED"
                )
                print(
                    f"  {snapshot.snapshot_id}  gen={snapshot.generation}  {snapshot.created_at_utc}  "
                    f"projects={snapshot.project_count} bindings={snapshot.binding_count}  "
                    f"{snapshot.validation_status}  {flags}"
                )
            for item in inventory.quarantine:
                print(f"  quarantine {item.event_id}  {item.created_at_utc or '?'}  {','.join(item.reason_codes)}")
            for problem in inventory.problems:
                print(f"  problem: {problem}")
            return int(ExitCode.OK)

        if args.command == "guardian" and args.guardian_command == "restore":
            plan, written = restore_global_state(
                config_path,
                snapshot_id=args.snapshot,
                confirm_plan=args.confirm_plan,
                dry_run=args.dry_run,
            )
            print(f"Guardian restore plan {plan.plan_id}")
            print(f"  snapshot: {plan.snapshot_id} (generation {plan.generation}, {plan.snapshot_created_at_utc})")
            print(f"  projects: now {plan.projects_now} -> snapshot {plan.projects_in_snapshot}")
            print(f"  bindings: now {plan.bindings_now} -> snapshot {plan.bindings_in_snapshot}")
            for code in plan.codes:
                print(f"  code: {code}")
            if plan.identical:
                print("  The state already equals this snapshot; nothing to restore.")
                return int(ExitCode.OK)
            if args.confirm_plan is None:
                if plan.codes:
                    return int(ExitCode.CONFLICT_DETECTED)
                print("  Nothing was written. Close Codex and repeat with:")
                print(f"    --confirm {plan.plan_id}")
                return int(ExitCode.OK)
            label = "Guardian restore dry-run finished" if args.dry_run else "Guardian restore finished"
            print(f"{label}. files={written}")
            return int(ExitCode.OK)

        if args.command == "guardian" and args.guardian_command == "accept":
            plan, accepted = accept_guardian_baseline(config_path, confirm_plan=args.confirm_plan)
            print(f"Guardian accept plan {plan.plan_id}")
            if plan.baseline_snapshot_id:
                print(
                    f"  baseline: {plan.baseline_snapshot_id} "
                    f"(generation {plan.baseline_generation}, {plan.baseline_created_at_utc})"
                )
            print(f"  projects: baseline {plan.projects_before} -> now {plan.projects_now}")
            print(f"  bindings: baseline {plan.bindings_before} -> now {plan.bindings_now}")
            if plan.state_codes:
                print(f"  validation: {', '.join(plan.state_codes)}")
            why = plan.explanation
            if why is not None:
                print(
                    f"  projects: {why.projects_replaced} re-created under a new id, "
                    f"{why.projects_removed} removed, {why.projects_added} new"
                )
                print(
                    f"  lost bindings: {why.bindings_to_replaced_projects} to re-created projects, "
                    f"{why.bindings_to_removed_projects} to removed projects, "
                    f"{why.bindings_dropped} dropped from projects that still exist"
                )
            for code in plan.codes:
                print(f"  code: {code}")
            if accepted is not None:
                print(f"Guardian baseline accepted: {accepted} is now latest-good")
                return int(ExitCode.OK)
            if plan.codes:
                informational = set(plan.codes) <= {"NO_BASELINE", "NOTHING_TO_ACCEPT"}
                if informational:
                    print("  Nothing to accept: the next snapshot commits this state by itself.")
                return int(ExitCode.OK if informational else ExitCode.CONFLICT_DETECTED)
            print("  Nothing was written. To make this state the new baseline, repeat with:")
            print(f"    --confirm {plan.plan_id}")
            return int(ExitCode.OK)

        if args.command == "project-move" and args.project_move_command == "scan":
            plan = scan_project_move(config_path, project_id=args.project, new_root=Path(args.new_root).expanduser())
            print(f"Project move plan {plan.plan_id}")
            print(f"  project: {plan.project_name or plan.project_id} ({plan.project_id})")
            print(f"  from: {plan.old_root}   (kept, never modified)")
            print(f"  to:   {plan.new_root}")
            print(f"  files: {plan.file_count}   bytes: {plan.total_bytes}   chats to pin: {len(plan.bindings)}")
            if plan.copy_complete:
                print("  A verified copy is already in place; apply will only update Codex.")
            if plan.volatile:
                print("  VOLATILE: Codex is running; close it and scan again before applying.")
            for code in plan.codes:
                print(f"  code: {code}")
            for code, path in plan.blocked_paths:
                print(f"    {code}: {path}")
            notes = project_move_notes(plan)
            if notes.over_path_limit:
                print(
                    f"  WARNING: the longest path in the copy will be {notes.longest_path} characters; "
                    "long paths are off on this machine, so a program that is not long-path aware "
                    "may fail to open such files there. The move itself is not affected."
                )
            for path in notes.abandoned_stagings:
                print(f"  left by an earlier attempt, removed by apply: {path}")
            if args.save_plan:
                save_project_move_plan(plan, Path(args.save_plan).expanduser().resolve())
            return int(ExitCode.CONFLICT_DETECTED if plan.codes else ExitCode.OK)

        if args.command == "project-move" and args.project_move_command == "apply":
            result = apply_project_move_plan(
                config_path,
                plan_path=Path(args.plan).expanduser().resolve(),
                confirm_plan=args.confirm_plan,
                dry_run=args.dry_run,
            )
            label = "Project move dry-run finished" if result.dry_run else "Project move finished"
            print(
                f"{label}. files={result.copied_files} bytes={result.copied_bytes} "
                f"bindings={result.bindings_written}"
            )
            print(f"  new root: {result.new_root}")
            print(f"  old folder kept: {result.old_root_kept} (delete it yourself once you have checked the copy)")
            return int(ExitCode.OK)

        if args.command == "guardian" and args.guardian_command == "scheduler":
            print(
                "DEPRECATED: `guardian scheduler` only renders templates. Set [scheduler] in "
                "config.toml and run `codexsync automation apply` instead.",
                file=sys.stderr,
            )
            templates = render_scheduler_templates(
                args.platform,
                executable=Path(sys.executable).resolve(),
                config_path=config_path.resolve(),
                log_dir=Path(args.log_dir).expanduser().resolve(),
                interval_seconds=args.interval,
            )
            written = write_scheduler_templates(templates, Path(args.output_dir).expanduser().resolve())
            print(f"Guardian scheduler templates written: {len(written)}")
            return int(ExitCode.OK)

        if args.command == "repair-projects" and args.repair_command == "scan":
            plan = scan_repair_projects(
                config_path,
                source_machine=args.source_machine,
                target_machine=args.target_machine,
            )
            counts: dict[str, int] = {}
            for action in plan.actions:
                counts[action.kind.value] = counts.get(action.kind.value, 0) + 1
            report = {
                "version": plan.version,
                "plan_id": plan.plan_id,
                "volatile": plan.volatile,
                "global_state_sha256": plan.global_state_sha256,
                "mapping_digest": plan.mapping_digest,
                "counts": counts,
                "codes": list(plan.codes),
            }
            rendered = json.dumps(report, sort_keys=True, indent=2)
            print(rendered)
            if args.output:
                output = Path(args.output).expanduser().resolve()
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(rendered + "\n", encoding="utf-8", newline="\n")
            if args.save_plan:
                save_repair_plan(plan, Path(args.save_plan).expanduser().resolve())
            return int(ExitCode.OK if not plan.codes else ExitCode.CONFLICT_DETECTED)

        if args.command == "repair-projects" and args.repair_command == "apply":
            changed = apply_repair_projects(
                config_path,
                plan_path=Path(args.plan).expanduser().resolve(),
                confirm_plan=args.confirm_plan,
                dry_run=args.dry_run,
            )
            label = "Project repair dry-run finished" if args.dry_run else "Project repair finished"
            print(f"{label}. approved_actions={changed}")
            return int(ExitCode.OK)

        if args.command == "sessions" and args.sessions_command == "scan":
            # The working set is read first: it decides what may be written
            # into `.codex`, and it is part of the plan id.
            stored = _read_scope_file(Path(args.scope_file)) if args.scope_file else None
            projects = tuple(args.project) + (stored.projects if stored else ())
            chats = tuple(args.chat) + (stored.chats if stored else ())
            scope = (
                build_working_set(config_path, projects=projects, chats=chats)
                if projects or chats else None
            )
            if scope is not None and args.save_scope:
                write_working_set(
                    config_path, scope,
                    source_machine=args.source_machine, target_machine=args.target_machine,
                )
            plan = scan_session_transfer(
                config_path,
                source_machine=args.source_machine,
                target_machine=args.target_machine,
                resolutions_path=Path(args.resolutions).expanduser().resolve() if args.resolutions else None,
                scope=scope,
                conflict_policy=args.conflict_policy, direction=args.direction,
            )
            counts: dict[str, int] = {}
            for item in plan.items:
                counts[item.action.value] = counts.get(item.action.value, 0) + 1
            report = {
                "plan_id": plan.plan_id,
                "volatile": plan.volatile,
                "layout_id": plan.layout_id,
                "mirror_layout_id": plan.mirror_layout_id,
                "canonical_version": plan.canonical_version,
                "sessions": len(plan.items),
                "counts": counts,
                # Branches bound for `.codex` whose folder is not here, counted
                # and never named: the folders are paths on someone's disk.
                "cwd_absent_here": sum(
                    1 for item in plan.items if CWD_ABSENT_HERE in item.codes
                ),
                # Conflicts that are only the runtime rewriting a history into a
                # newer record format; `sessions resolve --format-migrations`
                # decides the first number in one step and never the second.
                "format_migrations": sum(
                    1 for item in plan.items
                    if item.action.value == "BLOCKED_CONFLICT" and FORMAT_MIGRATION in item.codes
                ),
                "format_migrations_for_you": sum(
                    1 for item in plan.items
                    if item.action.value == "BLOCKED_CONFLICT" and OLDER_FORMAT_HAS_LATER_RECORDS in item.codes
                ),
                # The set is reported by size, never by id: a working set names
                # projects and chats, and those names stay out of the report.
                "working_set": {
                    "projects": len(scope.projects) if scope else 0,
                    "chats": scope.chat_count if scope else 0,
                } if scope else None,
                "codes": list(plan.codes),
                # Session ids, thread names and record payloads never appear:
                # a conflict is addressed by its id alone.
                "conflicts": sorted(
                    item.conflict_id for item in plan.items if item.conflict_id
                ),
            }
            rendered = json.dumps(report, sort_keys=True, indent=2)
            print(rendered)
            if args.output:
                output = Path(args.output).expanduser().resolve()
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(rendered + "\n", encoding="utf-8", newline="\n")
            if args.save_plan:
                save_transfer_plan(plan, Path(args.save_plan).expanduser().resolve())
            # 2 only for what refuses the whole apply and needs a person's
            # decision; a layout- or catalogue-blocked branch is left alone by
            # `sessions apply` and is no conflict (CS-325).
            unresolved = any(item.action in _UNRESOLVED_TRANSFER_BLOCKS for item in plan.blocked_items)
            return int(ExitCode.CONFLICT_DETECTED if unresolved else ExitCode.OK)

        if args.command == "sessions" and args.sessions_command == "index":
            report = audit_session_index(config_path)
            print(json.dumps(report, sort_keys=True, indent=2))
            # A divergent record for one session is a decision, exactly like a
            # divergent branch, so it leaves by the same door as `sessions scan`.
            return int(
                ExitCode.CONFLICT_DETECTED if report["differing"] else ExitCode.OK
            )

        if args.command == "sessions" and args.sessions_command == "resolve":
            if args.format_migrations:
                if args.conflict or args.choice:
                    raise ConfigError("--format-migrations decides its own conflicts; drop --conflict/--choice")
                decided, held = record_format_migrations(
                    Path(args.plan).expanduser().resolve(),
                    output_path=Path(args.output).expanduser().resolve(),
                )
                print(f"Recorded the newer record format for {len(decided)} conflict(s).")
                if held:
                    print(
                        f"  {len(held)} left for you: the older copy has a record later than the newer one, "
                        "so it may hold work the rewrite never saw. Resolve each with --conflict/--choice:"
                    )
                    for conflict_id in held:
                        print(f"    {conflict_id}")
                print("  Re-run `sessions scan --resolutions <file>` to rebuild the plan with them.")
                return int(ExitCode.OK)
            if not args.conflict or not args.choice:
                raise ConfigError("sessions resolve needs --conflict and --choice, or --format-migrations")
            resolution = record_branch_resolution(
                Path(args.plan).expanduser().resolve(),
                conflict_id=args.conflict,
                choice=args.choice,
                output_path=Path(args.output).expanduser().resolve(),
            )
            print(f"Recorded {resolution.choice.value} for conflict {resolution.conflict_id}")
            print(f"  confirmation: {resolution.confirmation}")
            print("  Re-run `sessions scan --resolutions <file>` to rebuild the plan with it.")
            return int(ExitCode.OK)

        if args.command == "sessions" and args.sessions_command == "catalogue":
            refresh = refresh_thread_catalogue(
                config_path, confirm_plan=args.confirm_plan, dry_run=args.dry_run, origin="cli",
            )
            plan = refresh.plan
            if args.as_json:
                print(json.dumps({
                    "plan_id": plan.plan_id,
                    "status": plan.status.value,
                    "database": plan.database,
                    "backfill": plan.backfill,
                    "unnamed": list(plan.unnamed),
                    "codes": list(plan.codes),
                    "refreshed": refresh.refreshed,
                    "volatile": refresh.volatile,
                }, sort_keys=True, indent=2))
                return int(ExitCode.OK)
            print(f"Codex chat list: {plan.status.value} (catalogue {plan.database or '-'}, "
                  f"backfill {plan.backfill or '-'})")
            if plan.codes:
                print(f"  codes: {', '.join(plan.codes)}")
            print(f"  chat files Codex does not list: {len(plan.unnamed)}")
            for path in plan.unnamed:
                print(f"    {path}")
            if refresh.volatile:
                print("  Codex is running: this reading is only an indication")
            if refresh.refreshed:
                print("Asked Codex to rebuild its chat list; it does so on its next start.")
                print(_REBUILD_ADVICE)
            elif args.confirm_plan is None and plan.writes:
                files = refresh.chat_files
                if files is not None:
                    print(
                        f"  a rebuild walks every chat file: {files.count} file(s), "
                        f"{files.size // (1024 * 1024)} MB"
                    )
                print(f"  plan id: {plan.plan_id}")
                print("  Close Codex and run again with --confirm-plan <plan id> to ask it to rebuild its chat list.")
                print(_REBUILD_ADVICE)
            return int(ExitCode.OK)

        if args.command == "codex" and args.codex_command == "check":
            health = check_codex(config_path)
            if args.as_json:
                print(json.dumps({
                    "broken": health.broken,
                    "process_state": health.process_state.value,
                    "findings": [
                        {
                            "code": item.code, "severity": item.severity.value, "counts": dict(item.counts),
                            "fix": item.fix, "command": item.command,
                        }
                        for item in health.findings
                    ],
                    "repair_plan_id": health.plan.plan_id if health.plan.writes else None,
                }, sort_keys=True, indent=2))
                return int(ExitCode.OK)
            _print_codex_health(health)
            return int(ExitCode.OK)

        if args.command == "codex" and args.codex_command == "repair":
            repair = repair_codex(
                config_path, confirm_plan=args.confirm_plan, dry_run=args.dry_run, origin="cli",
            )
            plan = repair.plan
            if args.as_json:
                print(json.dumps({
                    "plan_id": plan.plan_id,
                    "writes": plan.writes,
                    "current": list(plan.current) if plan.current is not None else None,
                    "target": list(plan.target) if plan.target is not None else None,
                    "source": plan.source,
                    "snapshot": plan.snapshot,
                    "repaired": repair.repaired,
                    "backup": repair.snapshot,
                }, sort_keys=True, indent=2))
                return int(ExitCode.OK)
            if not plan.writes:
                print("Nothing to repair: Codex's chat-list rebuild is not stuck.")
                return int(ExitCode.OK)
            print(f"Codex's chat-list rebuild: {plan.current[0] if plan.current else '-'} -> complete")
            print(
                "  row put back: "
                + (f"the one codexSync's copy {plan.snapshot} holds" if plan.snapshot else "the row as found")
            )
            if repair.repaired:
                print(f"Repaired; the catalogue as it was is in backup {repair.snapshot}. Start Codex now.")
            elif args.confirm_plan is None:
                print(f"  plan id: {plan.plan_id}")
                print("  Close Codex and run again with --confirm-plan <plan id> to repair.")
            return int(ExitCode.OK)

        if args.command == "sessions" and args.sessions_command == "names":
            named = sync_chat_names(
                config_path, confirm_plan=args.confirm_plan, dry_run=args.dry_run, origin="cli",
            )
            plan = named.plan
            if args.as_json:
                # Ids and counts only: the names themselves are the user's.
                print(json.dumps({
                    "plan_id": plan.plan_id,
                    "database": plan.database,
                    "to_set": [item.thread_id for item in plan.changes],
                    "kept": plan.kept,
                    "ambiguous": plan.ambiguous,
                    "waiting": plan.waiting,
                    "codes": list(plan.codes),
                    "written": named.written,
                    "published": named.published,
                    "volatile": named.volatile,
                }, sort_keys=True, indent=2))
                return int(ExitCode.OK)
            print(f"Chat names from other machines: {len(plan.changes)} to set here")
            if plan.codes:
                print(f"  codes: {', '.join(plan.codes)}")
            if plan.kept:
                print(f"  named differently here, kept: {plan.kept}")
            if plan.ambiguous:
                print(f"  named differently by two machines, left alone: {plan.ambiguous}")
            if plan.waiting:
                print(f"  waiting for Codex to list the chat here: {plan.waiting}")
            if named.volatile:
                print("  Codex is running: this reading is only an indication")
            if args.confirm_plan is None:
                if plan.writes:
                    print(f"  plan id: {plan.plan_id}")
                    print("  Close Codex and run again with --confirm-plan <plan id> to set them.")
            elif not args.dry_run:
                print(f"Set {named.written} name(s); this machine's names "
                      + ("published." if named.published else "unchanged."))
            return int(ExitCode.OK)

        if args.command == "sessions" and args.sessions_command == "apply":
            written = apply_session_transfer(
                config_path,
                plan_path=Path(args.plan).expanduser().resolve(),
                confirm_plan=args.confirm_plan,
                resolutions_path=Path(args.resolutions).expanduser().resolve() if args.resolutions else None,
                dry_run=args.dry_run,
                origin="cli",
            )
            label = "Session transfer dry-run finished" if args.dry_run else "Session transfer finished"
            print(f"{label}. branches={written}")
            return int(ExitCode.OK)

        if args.command == "projects" and args.projects_command == "files":
            # Every check reads the folders afresh and publishes this machine's
            # side, so the other machines compare against today (D-026).
            report = check_project_files(config_path, publish=True)
            shown = report.items if args.all else report.warnings
            if args.as_json:
                print(json.dumps([
                    {
                        "project": item.name, "root": item.root, "peer": item.peer,
                        "verdict": item.verdict.value, "peer_published_at_utc": item.peer_published_at_utc,
                        "newer_there": list(item.newer_there), "missing_here": list(item.missing_here),
                        "removed_there": list(item.removed_there),
                        "chats_there_at": item.chats_there_at or None,
                    }
                    for item in shown
                ], sort_keys=True, indent=2, ensure_ascii=False))
                return int(ExitCode.OK)
            if report.published:
                print("This machine's project folders were published for the other machines.")
            if not report.items:
                print("No other machine has published its project folders yet (it does on its next check).")
                return int(ExitCode.OK)
            print(f"Project folders compared with other machines: {len(report.items)}, "
                  f"needing attention: {len(report.warnings)}")
            _print_project_files(shown, limit=None)
            return int(ExitCode.OK)

        if args.command == "projects":
            result = sync_projects(config_path, confirm_plan=args.confirm_plan, dry_run=args.dry_run, origin="cli")
            if args.as_json:
                print(json.dumps(_project_sync_json(result), sort_keys=True, indent=2, ensure_ascii=False))
            else:
                _print_project_sync(result, applied=bool(args.confirm_plan) and not args.dry_run)
            return int(ExitCode.OK)

        if args.command == "chats" and args.chats_command == "move":
            plan, written = move_chats(
                config_path,
                chat_refs=args.chat_refs,
                to_project=args.to_project,
                confirm_plan=args.confirm_plan,
                dry_run=args.dry_run,
                include_sub_threads=args.sub_threads,
                source_machine=args.source_machine,
                target_machine=args.target_machine,
            )
            print_chat_move(plan, written, applied=bool(args.confirm_plan) and not args.dry_run)
            return int(ExitCode.CONFLICT_DETECTED if plan.codes else ExitCode.OK)

        if args.command == "chats":
            directory = scan_chats(
                config_path,
                source_machine=args.source_machine,
                target_machine=args.target_machine,
            )
            if args.chats_command == "list":
                selected = search_chats(
                    directory,
                    text=args.text,
                    project=args.project,
                    association=Association(args.association) if args.association else None,
                    since=args.since,
                    until=args.until,
                    include_sub_threads=args.sub_threads,
                    include_invalid=args.invalid,
                )
                if args.limit:
                    selected = selected[: args.limit]
                print_chat_list(directory, selected, as_json=args.as_json)
            else:
                print_chat_tree(
                    directory,
                    project=args.project,
                    include_sub_threads=args.sub_threads,
                    include_invalid=args.invalid,
                    as_json=args.as_json,
                )
            return int(ExitCode.OK)

        if args.command == "recover" and args.recover_command == "list":
            journals = list_journals(config_path)
            if not args.all:
                journals = [item for item in journals if not item.terminal]
            _print_journals(journals, as_json=args.as_json, include_finished=args.all)
            return int(ExitCode.OK)

        if args.command == "recover" and args.recover_command == "inspect":
            journal = inspect_recovery(config_path, args.operation_id)
            print(json.dumps({
                "operation_id": journal.operation_id,
                "family": journal.family,
                "state": journal.state.value,
                "plan_hash": journal.plan_hash,
                "action_count": journal.action_count,
                "backup_snapshot": journal.backup_snapshot,
                "counts": dict(journal.counts) if journal.counts is not None else None,
                "origin": journal.origin,
                "finished_at_utc": journal.finished_at_utc,
                "failure": journal.failure,
                "machine_id": journal.machine_id,
            }, sort_keys=True, indent=2))
            return int(ExitCode.OK)

        if args.command == "backups":
            return _print_backups(list_backup_snapshots(config_path), config_path, as_json=args.as_json)

        if args.command == "summary":
            if args.recount:
                recount_state(config_path)
            return _print_summary(read_home_summary(config_path), as_json=args.as_json)

        if args.command == "sessions" and args.sessions_command == "scope":
            scope = read_working_set(
                config_path, source_machine=args.source_machine, target_machine=args.target_machine,
            )
            if args.expand and not scope.is_empty:
                scope = build_working_set(config_path, projects=scope.projects, chats=scope.chats)
            return _print_scope(scope, args, as_json=args.as_json)

        if args.command == "state-backup":
            if args.state_backup_command == "create":
                result = create_codex_backup(config_path, wait=args.wait)
                print(f"Copy of the Codex state written: {result.path}")
                print(f"  files: {result.files}, bytes: {result.bytes}")
                if result.waited_seconds >= 1:
                    print(f"  waited for Codex to close: {int(result.waited_seconds)} s")
                for name in result.pruned:
                    print(f"  removed old copy: {name}")
                return int(ExitCode.OK)
            copies = list_codex_backups(config_path)
            if args.as_json:
                print(json.dumps([
                    {"name": item.name, "path": str(item.path), "machine": item.machine,
                     "created_utc": item.created_utc, "size": item.size, "own": item.own}
                    for item in copies
                ], sort_keys=True, indent=2))
            elif not copies:
                print("No copies of the Codex state in the folder.")
            else:
                for item in copies:
                    mark = "" if item.own else "  (another machine)"
                    print(f"{item.created_utc}  {item.size:>14} B  {item.name}{mark}")
            return int(ExitCode.OK)

        if args.command == "handoff":
            if args.handoff_command == "status":
                status = handoff_status(config_path, check_delivery=args.check_delivery)
                if args.as_json:
                    print(json.dumps(_handoff_status_json(status), sort_keys=True, indent=2))
                else:
                    _print_handoff_status(status)
                return int(ExitCode.OK)
            if args.handoff_command == "sync":
                if args.wait_minutes is not None and args.wait_minutes < 0:
                    raise ConfigError("--wait-minutes must be 0 or more")
                result = run_handoff(
                    config_path,
                    origin="cli",
                    wait_seconds=None if args.wait_minutes is None else args.wait_minutes * 60.0,
                    accept_undelivered=args.accept_undelivered,
                    on_wait=lambda late: print(
                        "Waiting for the cloud: "
                        + ", ".join(f"{item.machine} {item.arrived}/{item.total}" for item in late)
                    ),
                    conflict_policy=args.conflict_policy, direction=args.direction,
                )
                _print_handoff_result(result)
                return int(ExitCode.OK)
            watch_handoff(config_path)
            return int(ExitCode.OK)

        if args.command == "history":
            runs = list_history(
                config_path,
                family=None if args.family == "all" else args.family,
                limit=args.limit if args.limit > 0 else None,
            )
            if args.as_json:
                print(json.dumps([_history_record(run) for run in runs], sort_keys=True, indent=2))
            else:
                _print_history(runs)
            return int(ExitCode.OK)

        if args.command == "recover" and args.recover_command in {"resume", "rollback"}:
            if args.recover_command == "resume":
                outcome = resume_operation(config_path, args.operation_id, dry_run=not args.apply)
            else:
                outcome = rollback_operation(
                    config_path,
                    args.operation_id,
                    target=args.target,
                    dry_run=not args.apply,
                )
            print(f"Recovery {outcome.action.value} for {outcome.family} operation {outcome.operation_id}")
            print(f"  journal state before: {outcome.state}")
            print(f"  backup snapshot: {outcome.snapshot or '(none recorded)'}")
            if outcome.restored_files:
                print(f"  restored files: {outcome.restored_files}")
            print(f"  {outcome.detail}")
            return int(ExitCode.OK)

        if args.command == "sync":
            scope = args.scope or load_config(config_path).sync.scope
            if scope == "full":
                return _run_full_sync(args, config_path)
            if args.apply and close_codex_for_sync(config_path):
                print("Codex was open and quit when asked ([sync] close_codex).")
            ctx = build_context(
                config_path,
                manual_terminate_confirmation_override=args.manual_terminate_confirmation_override,
                enforce_safety=True,
                conflict_policy=args.conflict_policy, direction=args.direction,
            )
            dry_run = ctx.config.sync.dry_run_default
            if args.dry_run:
                dry_run = True
            if args.apply:
                dry_run = False
            run_sync(ctx, dry_run=dry_run, origin="unattended" if args.unattended else "cli")
            print("Sync finished." if not dry_run else "Dry-run finished.")
            return int(ExitCode.OK)

        if args.command == "restore":
            dry_run = True
            if args.dry_run:
                dry_run = True
            if args.apply:
                dry_run = False

            result = restore_from_backup(
                config_path=config_path,
                snapshot_name=args.snapshot,
                target=args.target,
                dry_run=dry_run,
                manual_terminate_confirmation_override=args.manual_terminate_confirmation_override,
                allow_legacy_snapshot=args.allow_legacy_snapshot,
            )
            mode = "Dry-run" if dry_run else "Restore"
            print(
                f"{mode} finished. snapshot={result.snapshot_name} "
                f"target={result.target} files={result.restored_files}"
            )
            return int(ExitCode.OK)

        return int(ExitCode.BAD_INPUT)

    except ConfigError as exc:
        LOG.error("Configuration error: %s", exc)
        return int(ExitCode.BAD_INPUT)
    except SafetyPreconditionError as exc:
        LOG.error("Safety precondition failed: %s", exc)
        return int(ExitCode.CODEX_RUNNING)
    except ConflictError as exc:
        LOG.error("Conflict detected: %s", exc)
        return int(ExitCode.CONFLICT_DETECTED)
    except FailSafeError as exc:
        LOG.error("Fail-safe stop: %s", exc)
        return int(ExitCode.FAIL_SAFE)
    except Exception as exc:  # pragma: no cover
        LOG.exception("Unhandled error: %s", exc)
        return int(ExitCode.INTERNAL_ERROR)


def init_config_template(output_path: Path, force: bool) -> Path:
    if output_path.exists() and not force:
        raise ConfigError(
            f"File already exists: {output_path}. Use --force to overwrite."
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    template_path = Path(__file__).resolve().with_name("config.example.toml")
    template = template_path.read_text(encoding="utf-8")
    output_path.write_text(template, encoding="utf-8")
    return output_path.resolve()


def _init_config_has_values(args: argparse.Namespace) -> bool:
    return any(
        value is not None
        for value in (args.machine_id, args.local_state_dir, args.workspace_root, args.cloud_root)
    )


def init_config_with_values(output_path: Path, args: argparse.Namespace) -> Path:
    """Write a config with real values through the same validating writer the window uses.

    `--force` is refused rather than honoured: regenerating over an existing
    config would silently discard every edit made to it, and an existing config
    is changed by editing it, not by writing a fresh one on top.
    """
    if args.force:
        raise ConfigError(
            "--force cannot be combined with --machine-id/--local-state-dir/--workspace-root/"
            f"--cloud-root: an existing config is edited, not regenerated ({output_path})."
        )
    missing = [
        flag
        for flag, value in (
            ("--machine-id", args.machine_id),
            ("--local-state-dir", args.local_state_dir),
            ("--workspace-root", args.workspace_root),
        )
        if value is None
    ]
    if missing:
        raise ConfigError(
            "init-config with values needs --machine-id, --local-state-dir and --workspace-root "
            f"together; missing: {', '.join(missing)}"
        )
    saved = create_config(
        output_path.resolve(),
        machine_id=args.machine_id,
        local_state_dir=args.local_state_dir,
        workspace_root_dir=args.workspace_root,
        cloud_root_dir=args.cloud_root,
    )
    return Path(saved.path)


def _emit_verbose_process_snapshot(verbose: bool, cfg: AppConfig) -> None:
    if not verbose:
        return
    try:
        snapshot = collect_process_snapshot(cfg)
    except Exception as exc:
        LOG.warning("Unable to collect process snapshot: %s", exc)
        return

    if not snapshot.main_processes:
        LOG.info("Process snapshot: codex.exe is not running.")
        return

    LOG.info("Process snapshot: codex.exe running (count=%d).", len(snapshot.main_processes))
    LOG.info("Process snapshot: codex-windows-sandbox detected: %s.", "yes" if snapshot.sandbox_detected else "no")
    if not snapshot.subprocesses:
        LOG.info("Process snapshot: no subprocesses detected for codex.exe.")
        return

    LOG.info("Process snapshot: %d subprocess(es) under codex.exe.", len(snapshot.subprocesses))
    for proc in snapshot.subprocesses:
        LOG.info("  pid=%s name=%s parent_pid=%s", proc.pid, proc.name, proc.parent_pid)
