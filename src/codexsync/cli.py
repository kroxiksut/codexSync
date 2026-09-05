from __future__ import annotations

import argparse
import logging
import json
import sys
from pathlib import Path

from .app import (
    build_context,
    apply_repair_projects,
    apply_session_transfer,
    audit_session_index,
    build_guardian_runner,
    collect_process_snapshot,
    inspect_recovery,
    move_chats,
    print_preflight_report,
    record_branch_resolution,
    save_transfer_plan,
    scan_chats,
    scan_session_transfer,
    print_plan,
    restore_from_backup,
    run_preflight,
    run_sync,
    scan_repair_projects,
    validate_config_only,
)
from .chat_directory import Association, ChatDirectory, ChatEntry, search_chats
from .chat_move import ChatMovePlan
from .config import load_config
from .exceptions import ConfigError, ConflictError, FailSafeError, SafetyPreconditionError
from .exit_codes import ExitCode
from .logging_setup import configure_logging
from .models import AppConfig, LoggingConfig
from .recovery import resume_operation, rollback_operation
from .repair_plan import save_repair_plan
from .safety_gate import OperationKind
from .scheduler import render_scheduler_templates, write_scheduler_templates

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codexsync", description="codexSync CLI")
    parser.add_argument("-c", "--config", default="config.toml", help="Path to TOML config")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose logging (includes redacted process snapshot metadata)",
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

    sub.add_parser("validate", help="Validate config only")
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
    init_cfg = sub.add_parser("init-config", help="Write example config.toml file")
    init_cfg.add_argument(
        "--output",
        default="config.toml",
        help="Output path for generated config file",
    )
    init_cfg.add_argument(
        "--force",
        action="store_true",
        help="Overwrite output file if it already exists",
    )

    sync = sub.add_parser("sync", help="Run synchronization")
    mode_group = sync.add_mutually_exclusive_group()
    mode_group.add_argument("--dry-run", action="store_true", help="Force dry-run mode")
    mode_group.add_argument("--apply", action="store_true", help="Apply changes (overrides dry-run)")

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
    guardian_scheduler = guardian_sub.add_parser("scheduler", help="Render user-level fallback scheduler templates")
    guardian_scheduler.add_argument("--platform", choices=["windows", "macos", "linux"], required=True)
    guardian_scheduler.add_argument("--output-dir", required=True)
    guardian_scheduler.add_argument("--log-dir", required=True)
    guardian_scheduler.add_argument("--interval", type=int, default=60)

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
    sessions_sub.add_parser(
        "index",
        help="Report what each side's session_index.jsonl contains; writes nothing",
    )
    sessions_resolve = sessions_sub.add_parser(
        "resolve", help="Record one versioned choice between two divergent branches"
    )
    sessions_resolve.add_argument("--plan", required=True)
    sessions_resolve.add_argument("--conflict", required=True)
    sessions_resolve.add_argument(
        "--choice", required=True, choices=["KEEP_LOCAL", "KEEP_REMOTE", "DEFER"]
    )
    sessions_resolve.add_argument("--output", required=True, help="Resolutions file to create or extend")
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
            command.add_argument("--limit", type=int, default=50, help="Maximum rows (0 for no limit)")

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
        help="Run every check and report the write count without writing",
    )
    chats_move.add_argument("--sub-threads", action="store_true", help="Allow naming a spawned thread")
    chats_move.add_argument("--source-machine", default=None)
    chats_move.add_argument("--target-machine", default=None)

    recover = sub.add_parser("recover", help="Inspect and clear interrupted mutation evidence")
    recover_sub = recover.add_subparsers(dest="recover_command", required=True)
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
        "--target", choices=["local", "cloud"], required=True,
        help="Root the snapshot is restored into; never guessed from the snapshot",
    )
    recover_rollback_mode = recover_rollback.add_mutually_exclusive_group()
    recover_rollback_mode.add_argument("--dry-run", action="store_true", help="Report only (default)")
    recover_rollback_mode.add_argument("--apply", action="store_true", help="Perform the rollback")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config_path = Path(args.config).expanduser()
    try:
        if args.manual_terminate_confirmation_override is not None:
            raise ConfigError(
                "Process termination flags were removed in 0.2; close Codex manually and retry."
            )
        # Logging is configured with defaults first, then with file settings from config when context is built.
        configure_logging(LoggingConfig(level="INFO", file=None), verbose=args.verbose)
        cfg_for_verbose = None
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

        if args.command == "validate":
            validate_config_only(config_path)
            print("Config is valid.")
            return int(ExitCode.OK)

        if args.command == "init-config":
            output_path = Path(args.output).expanduser()
            created = init_config_template(output_path=output_path, force=args.force)
            print(f"Config template written: {created}")
            return int(ExitCode.OK)

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
            print_plan(ctx.plan, volatile=ctx.volatile)
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
                label = "SKIPPED_ACTIVE_GUARDIAN" if outcome.status == "BUSY" else outcome.status
                print(f"Guardian snapshot: {label}")
                return int(ExitCode.OK)
            if outcome.status == "QUARANTINED":
                print("Guardian snapshot: QUARANTINED")
                return int(ExitCode.CONFLICT_DETECTED)
            LOG.error("Guardian snapshot failed: %s", outcome.detail)
            return int(ExitCode.FAIL_SAFE)

        if args.command == "guardian" and args.guardian_command == "scheduler":
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
            plan = scan_session_transfer(
                config_path,
                source_machine=args.source_machine,
                target_machine=args.target_machine,
                resolutions_path=Path(args.resolutions).expanduser().resolve() if args.resolutions else None,
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
            return int(ExitCode.CONFLICT_DETECTED if plan.blocked_items else ExitCode.OK)

        if args.command == "sessions" and args.sessions_command == "index":
            report = audit_session_index(config_path)
            print(json.dumps(report, sort_keys=True, indent=2))
            # A divergent record for one session is a decision, exactly like a
            # divergent branch, so it leaves by the same door as `sessions scan`.
            return int(
                ExitCode.CONFLICT_DETECTED if report["differing"] else ExitCode.OK
            )

        if args.command == "sessions" and args.sessions_command == "resolve":
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

        if args.command == "sessions" and args.sessions_command == "apply":
            written = apply_session_transfer(
                config_path,
                plan_path=Path(args.plan).expanduser().resolve(),
                confirm_plan=args.confirm_plan,
                resolutions_path=Path(args.resolutions).expanduser().resolve() if args.resolutions else None,
                dry_run=args.dry_run,
            )
            label = "Session transfer dry-run finished" if args.dry_run else "Session transfer finished"
            print(f"{label}. branches={written}")
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

        if args.command == "recover" and args.recover_command == "inspect":
            journal = inspect_recovery(config_path, args.operation_id)
            print(json.dumps({
                "operation_id": journal.operation_id,
                "family": journal.family,
                "state": journal.state.value,
                "plan_hash": journal.plan_hash,
                "action_count": journal.action_count,
                "backup_snapshot": journal.backup_snapshot,
            }, sort_keys=True, indent=2))
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
            ctx = build_context(
                config_path,
                manual_terminate_confirmation_override=args.manual_terminate_confirmation_override,
                enforce_safety=True,
            )
            dry_run = ctx.config.sync.dry_run_default
            if args.dry_run:
                dry_run = True
            if args.apply:
                dry_run = False
            run_sync(ctx, dry_run=dry_run)
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
