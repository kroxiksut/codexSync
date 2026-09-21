"""Bring a config written by an older codexSync up to what this one expects.

The need is not theoretical. The template 0.1.2 shipped sets
`process_detection.allow_terminate_if_running = true` and
`sync.session_mode = "last_date_only"`, and 0.2 refuses both for every
mutating command -- so a user who ran `init-config` on 0.1 upgrades into a
state where `sync`, `restore`, `repair-projects apply` and `recover` all exit
4, and the error tells them to close Codex, which is not the problem at all.
Worse, they cannot fix it from the window: the Settings screen has no such
field, and `save_config_text` runs the same compatibility check over the text
it is asked to save, so every save is refused until that key changes.

What this module produces is a *plan*, not a rewrite. Each finding carries its
code, how bad it is, and the exact edits that would settle it; a finding whose
fix cannot be expressed as an edit is reported and left alone, because the one
thing worse than an outdated config is one this program changed by guessing.
The plan's id covers the findings, their edits *and* the sha256 of the file
they were read from, so a plan confirmed after the file moved on is refused --
the same freshness guarantee the chat move gives, with no plan file to leave
lying around.

Two rules keep the user's file theirs. Every edit goes through `config_edit`,
which re-parses each one with `tomllib` and checks the result means exactly
what was intended, so comments and formatting survive -- arrays grow and
shrink line by line rather than being re-rendered, because a real config keeps
notes between their elements. And nothing is applied one piece at a time: a
text with only the first blocker fixed still fails the compatibility check, so
every accepted finding is written in a single `save_config_text` call, which
also validates, keeps a verified copy of the replaced file, and refuses if the
file changed since the plan was built.

Deliberately not touched: `manual_terminate_confirmation`,
`terminate_confirmation_mode` and `terminate_timeout_seconds`. They look as
dead as the flag next to them, but the loader still reads them and the CLI
still has options that override one, so removing them would be this module
guessing about live code rather than recording a fact.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tomllib
from typing import Any, Collection, Iterable

from .config import decode_config_bytes
from .config_edit import (
    TEMPLATE_PATH,
    SavedConfig,
    append_array_items,
    config_diff,
    remove_array_items,
    remove_key,
    save_config_text,
    set_value,
)
from .exceptions import ConfigError, ConflictError
from .models import MIN_SCHEDULER_INTERVAL_SECONDS
from .process_knowledge import (
    OS_KEYS,
    missing_background_process_names,
    missing_process_names,
)
from .runtime import _is_semantic_owned

CONFIG_MIGRATION_PLAN_VERSION = 1

_TEMPLATE_ROOTS: frozenset[str] | None = None

#: The exclusion CS-254 added. It lives only in the config -- no code refuses
#: that tree on its own -- so a config without it syncs skills the Codex
#: runtime installs and deletes, and a file it removed comes back every run.
SKILLS_SYSTEM_GLOB = "skills/.system/**"

#: Sections a newer version introduced whose absence changes nothing, because
#: the loader defaults to exactly what the template writes.
OPTIONAL_SECTIONS = ("guardian", "semantic")

# Levels, most serious first. `blocker` is the only one that stops commands.
BLOCKER = "blocker"
SAFETY = "safety"
CORRECTNESS = "correctness"
INFO = "info"
_LEVEL_ORDER = (BLOCKER, SAFETY, CORRECTNESS, INFO)

# Finding codes.
TERMINATE_FLAG_SET = "TERMINATE_FLAG_SET"
BACKUP_DISABLED = "BACKUP_DISABLED"
SESSION_MODE_LAST_DATE = "SESSION_MODE_LAST_DATE"
DETECTION_LIST_OUTDATED = "DETECTION_LIST_OUTDATED"
MISSING_EXCLUDE_SKILLS_SYSTEM = "MISSING_EXCLUDE_SKILLS_SYSTEM"
OBSOLETE_INCLUDE_ROOT = "OBSOLETE_INCLUDE_ROOT"
LEGACY_SCHEDULER_KEYS = "LEGACY_SCHEDULER_KEYS"
SCHEDULER_INTERVAL_MIGRATED = "SCHEDULER_INTERVAL_MIGRATED"
SECTION_ABSENT = "SECTION_ABSENT"


@dataclass(frozen=True, slots=True)
class ConfigEdit:
    """One change to the config text, named the way `config_edit` performs it."""

    #: ``set`` | ``append`` | ``remove_items`` | ``remove_key``
    kind: str
    section: str
    key: str
    value: Any = None

    def apply(self, text: str) -> str:
        if self.kind == "set":
            return set_value(text, self.section, self.key, self.value)
        if self.kind == "append":
            return append_array_items(text, self.section, self.key, list(self.value))
        if self.kind == "remove_items":
            return remove_array_items(text, self.section, self.key, list(self.value))
        if self.kind == "remove_key":
            return remove_key(text, self.section, self.key)
        raise ValueError(f"unknown config edit kind: {self.kind}")

    def describe(self) -> str:
        where = f"{self.section}.{self.key}" if self.section else self.key
        if self.kind == "set":
            return f"{where} = {json.dumps(self.value, ensure_ascii=False)}"
        if self.kind == "append":
            return f"{where} += {json.dumps(self.value, ensure_ascii=False)}"
        if self.kind == "remove_items":
            return f"{where} -= {json.dumps(self.value, ensure_ascii=False)}"
        return f"remove {where}"


@dataclass(frozen=True, slots=True)
class ConfigFinding:
    """Something this version would have written differently, and why."""

    code: str
    level: str
    detail: str
    edits: tuple[ConfigEdit, ...] = ()
    #: True when the user may keep the current value: the config still works.
    optional: bool = False

    @property
    def fixable(self) -> bool:
        return bool(self.edits)


@dataclass(frozen=True, slots=True)
class ConfigMigrationPlan:
    version: int
    plan_id: str
    source_sha256: str
    findings: tuple[ConfigFinding, ...] = field(default_factory=tuple)

    @property
    def blockers(self) -> tuple[ConfigFinding, ...]:
        return tuple(item for item in self.findings if item.level == BLOCKER)

    @property
    def fixable(self) -> tuple[ConfigFinding, ...]:
        return tuple(item for item in self.findings if item.fixable)

    @property
    def is_current(self) -> bool:
        return not self.findings

    def codes(self) -> tuple[str, ...]:
        return tuple(finding.code for finding in self.findings)


def config_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_config_source(path: Path) -> tuple[str, str]:
    """The config as text, and the sha256 of the bytes it came from.

    The digest is over the *bytes*, which is what `save_config_text` compares
    its `expected_sha256` against. Hashing the decoded text instead would
    differ for a file with a byte-order mark -- the very thing
    `decode_config_bytes` exists to accept -- and every apply on such a config
    would be refused as "changed since the plan was built".
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read config file: {path}. {exc}") from exc
    return decode_config_bytes(data, source=str(path)), hashlib.sha256(data).hexdigest()


def read_config_text(path: Path) -> str:
    """The config as text, decoded the way the loader decodes it."""
    return read_config_source(path)[0]


def inspect_config(
    text: str, *, include_defaults: bool = False, source_sha256: str | None = None
) -> ConfigMigrationPlan:
    """Read config text and report what this version would change. Reads only.

    `include_defaults` adds the purely descriptive findings: sections a newer
    version introduced whose absence changes no behaviour, because the loader
    already defaults to what the template writes. They are left out otherwise
    so that the common case -- a working config from the previous release --
    reports the handful of things that actually matter.
    """
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Config is not valid TOML: {exc}") from exc

    findings: list[ConfigFinding] = []
    findings.extend(_blocking_findings(document))
    findings.extend(_detection_findings(document))
    findings.extend(_correctness_findings(document))
    if include_defaults:
        findings.extend(_absent_section_findings(document))
    findings.sort(key=lambda item: (_LEVEL_ORDER.index(item.level), item.code))
    return _with_plan_id(
        ConfigMigrationPlan(
            CONFIG_MIGRATION_PLAN_VERSION,
            "",
            source_sha256 if source_sha256 is not None else config_sha256(text),
            tuple(findings),
        )
    )


def render_migrated_text(
    text: str, plan: ConfigMigrationPlan, *, skip: Collection[str] = ()
) -> str:
    """Apply the plan's edits to `text`, leaving every other byte where it is.

    Findings whose code is in `skip` are left out, which is how a user keeps a
    process list they shortened on purpose. A blocker cannot be skipped: with
    it in place the result would still be a config every mutating command
    refuses, and `save_config_text` would reject the save anyway.
    """
    unknown = sorted(set(skip) - set(plan.codes()))
    if unknown:
        raise ConfigError(f"Not in this plan: {', '.join(unknown)}")
    refused = sorted(
        finding.code for finding in plan.blockers if finding.code in set(skip)
    )
    if refused:
        raise ConfigError(
            "These make every mutating command exit 4 and cannot be kept: "
            + ", ".join(refused)
        )
    result = text
    for finding in plan.findings:
        if finding.code in set(skip):
            continue
        for edit in finding.edits:
            result = edit.apply(result)
    return result


@dataclass(frozen=True, slots=True)
class MigrationOutcome:
    plan: ConfigMigrationPlan
    saved: SavedConfig
    applied: tuple[str, ...]
    skipped: tuple[str, ...]
    diff: str


def apply_migration(
    path: Path,
    *,
    confirm_plan_id: str,
    skip: Collection[str] = (),
    include_defaults: bool = False,
    now: datetime | None = None,
) -> MigrationOutcome:
    """Rewrite `path` once, with every accepted finding settled together.

    The plan is rebuilt from the file as it is now and its id must equal the
    one the caller confirmed, so a file edited between the preview and the
    apply stops the apply rather than being written over.
    """
    text, source_sha256 = read_config_source(path)
    plan = inspect_config(
        text, include_defaults=include_defaults, source_sha256=source_sha256
    )
    if plan.plan_id != confirm_plan_id:
        raise ConflictError(
            "Config changed since the plan was built; re-run the check and confirm the new id"
        )
    if plan.is_current:
        raise ConfigError("Config is already up to date; nothing to apply")
    new_text = render_migrated_text(text, plan, skip=skip)
    if new_text == text:
        raise ConfigError("Every finding was skipped; nothing to apply")
    saved = save_config_text(path, new_text, expected_sha256=plan.source_sha256, now=now)
    skipped = tuple(code for code in plan.codes() if code in set(skip))
    applied = tuple(
        finding.code for finding in plan.findings
        if finding.fixable and finding.code not in set(skip)
    )
    return MigrationOutcome(
        plan=plan,
        saved=saved,
        applied=applied,
        skipped=skipped,
        diff=config_diff(text, new_text, path_label=str(path)),
    )


def migration_hint(path: Path | None = None) -> str:
    """The one line every refusal should end with, naming the way out."""
    where = f" -c {path}" if path is not None else ""
    return f"run `codexsync{where} config check` to see what this version would change"


# --------------------------------------------------------------------------
# The findings
# --------------------------------------------------------------------------


def _table(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name)
    return value if isinstance(value, dict) else {}


def _template_include_roots() -> frozenset[str]:
    """`targets.include_roots` of the template this version ships."""
    global _TEMPLATE_ROOTS
    if _TEMPLATE_ROOTS is None:
        try:
            document = tomllib.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):  # pragma: no cover - packaging fault
            document = {}
        roots = _table(document, "targets").get("include_roots", [])
        _TEMPLATE_ROOTS = frozenset(str(item) for item in roots)
    return _TEMPLATE_ROOTS


def _blocking_findings(document: dict[str, Any]) -> Iterable[ConfigFinding]:
    process = _table(document, "process_detection")
    if process.get("allow_terminate_if_running") is True:
        yield ConfigFinding(
            TERMINATE_FLAG_SET, BLOCKER,
            "process_detection.allow_terminate_if_running = true: codexSync never stops Codex, "
            "and every mutating command refuses this config",
            (ConfigEdit("set", "process_detection", "allow_terminate_if_running", False),),
        )
    backup = _table(document, "backup")
    if backup.get("backup_before_overwrite") is False:
        yield ConfigFinding(
            BACKUP_DISABLED, BLOCKER,
            "backup.backup_before_overwrite = false: a mutation without a verified backup "
            "is not offered",
            (ConfigEdit("set", "backup", "backup_before_overwrite", True),),
        )
    sync = _table(document, "sync")
    if str(sync.get("session_mode", "")).strip().lower() == "last_date_only":
        yield ConfigFinding(
            SESSION_MODE_LAST_DATE, BLOCKER,
            "sync.session_mode = \"last_date_only\" can drop branches, so it is readable by "
            "plan only and every mutating command refuses it",
            (ConfigEdit("set", "sync", "session_mode", "all"),),
        )


def _detection_findings(document: dict[str, Any]) -> Iterable[ConfigFinding]:
    """The one finding that is about safety rather than tidiness.

    Names are matched whole, so a process this version knows and the config
    does not is never seen at all -- and the state that matters is the Codex
    window being closed while its background processes are still running.
    """
    process = _table(document, "process_detection")
    if not process:
        # No section at all: the loader already supplies this version's lists.
        return
    background = process.get("background_process_names")
    configured_background = {
        os_key: [str(item) for item in background.get(os_key, [])]
        for os_key in OS_KEYS
    } if isinstance(background, dict) else {}
    names_missing = (
        missing_process_names([str(item) for item in process.get("process_names", [])])
        if "process_names" in process else []
    )
    markers_missing = (
        missing_background_process_names(configured_background)
        if isinstance(background, dict) else {}
    )
    if not names_missing and not markers_missing:
        return
    edits: list[ConfigEdit] = []
    if names_missing:
        edits.append(ConfigEdit("append", "process_detection", "process_names", names_missing))
    for os_key in OS_KEYS:
        if os_key in markers_missing:
            edits.append(ConfigEdit(
                "append", "process_detection.background_process_names", os_key,
                markers_missing[os_key],
            ))
    # One name can be missing from several lists (codex-app-server is a
    # process name and a marker on two platforms); the message names it once.
    described = ", ".join(dict.fromkeys(
        [*names_missing, *(name for names in markers_missing.values() for name in names)]
    ))
    yield ConfigFinding(
        DETECTION_LIST_OUTDATED, SAFETY,
        "Codex processes this version knows are missing from the config, so they would not be "
        f"seen at all: {described}",
        tuple(edits),
        optional=True,
    )


def _correctness_findings(document: dict[str, Any]) -> Iterable[ConfigFinding]:
    filters = _table(document, "filters")
    globs = [str(item) for item in filters.get("exclude_globs", [])]
    if "exclude_globs" in filters and SKILLS_SYSTEM_GLOB not in globs:
        yield ConfigFinding(
            MISSING_EXCLUDE_SKILLS_SYSTEM, CORRECTNESS,
            f"{SKILLS_SYSTEM_GLOB} is not excluded: the Codex runtime installs and removes those "
            "skills itself, and with delete_policy = \"never\" a file it deleted returns on every run",
            (ConfigEdit("append", "filters", "exclude_globs", [SKILLS_SYSTEM_GLOB]),),
        )

    targets = _table(document, "targets")
    roots = [str(item) for item in targets.get("include_roots", [])]
    # Semantic-owned, and not something the shipped template itself lists. The
    # template keeps `session_index.jsonl` on purpose -- `sync_candidates`
    # reports such a path *with* its reason instead of hiding it -- so the
    # rule is written against the template rather than against ownership
    # alone, which also keeps the current template a fixed point of this
    # migration. What is left is the state database a 0.1 config listed
    # expecting a copy that no version will ever make.
    reference = _template_include_roots()
    obsolete = [
        root for root in roots
        if _is_semantic_owned(root) and root not in reference
    ]
    if obsolete:
        yield ConfigFinding(
            OBSOLETE_INCLUDE_ROOT, CORRECTNESS,
            "these are owned by the session and state machinery and are never copied, so "
            f"listing them changes nothing: {', '.join(obsolete)}",
            (ConfigEdit("remove_items", "targets", "include_roots", obsolete),),
            optional=True,
        )

    scheduler = _table(document, "scheduler")
    minutes = scheduler.get("interval_minutes")
    carries_period = (
        isinstance(minutes, (int, float))
        and not isinstance(minutes, bool)
        and "interval_seconds" not in scheduler
    )
    if carries_period:
        seconds = max(int(minutes * 60), MIN_SCHEDULER_INTERVAL_SECONDS)
        # The old key is removed by this finding and not by the one below:
        # dropping it on its own would silently reset the period the user
        # chose to the default, which is the opposite of a migration.
        yield ConfigFinding(
            SCHEDULER_INTERVAL_MIGRATED, CORRECTNESS,
            f"scheduler.interval_minutes = {minutes} has been read by no version since the "
            f"scheduler moved to seconds; the same period is interval_seconds = {seconds}",
            (
                ConfigEdit("set", "scheduler", "interval_seconds", seconds),
                ConfigEdit("remove_key", "scheduler", "interval_minutes"),
            ),
        )
    legacy = ["kind"] if "kind" in scheduler else []
    if "interval_minutes" in scheduler and not carries_period:
        legacy.append("interval_minutes")
    if legacy:
        yield ConfigFinding(
            LEGACY_SCHEDULER_KEYS, CORRECTNESS,
            "scheduler keys this version ignores: " + ", ".join(legacy),
            tuple(ConfigEdit("remove_key", "scheduler", key) for key in legacy),
            optional=True,
        )


def _absent_section_findings(document: dict[str, Any]) -> Iterable[ConfigFinding]:
    """Sections a newer version introduced. Absent means default, not broken."""
    for name in OPTIONAL_SECTIONS:
        if name not in document:
            yield ConfigFinding(
                SECTION_ABSENT + "_" + name.upper(), INFO,
                f"[{name}] is absent, so its defaults apply; writing it out makes them visible "
                "and editable",
                (),
                optional=True,
            )


def _with_plan_id(plan: ConfigMigrationPlan) -> ConfigMigrationPlan:
    payload = {
        "version": plan.version,
        "source_sha256": plan.source_sha256,
        "findings": [
            {
                "code": finding.code,
                "level": finding.level,
                "optional": finding.optional,
                "edits": [
                    {
                        "kind": edit.kind,
                        "section": edit.section,
                        "key": edit.key,
                        "value": edit.value,
                    }
                    for edit in finding.edits
                ],
            }
            for finding in plan.findings
        ],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ConfigMigrationPlan(plan.version, digest, plan.source_sha256, plan.findings)


__all__ = [
    "BLOCKER",
    "CORRECTNESS",
    "ConfigEdit",
    "ConfigFinding",
    "ConfigMigrationPlan",
    "INFO",
    "MigrationOutcome",
    "SAFETY",
    "SKILLS_SYSTEM_GLOB",
    "apply_migration",
    "config_sha256",
    "inspect_config",
    "migration_hint",
    "read_config_source",
    "read_config_text",
    "render_migrated_text",
]
