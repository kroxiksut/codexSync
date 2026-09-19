"""Render the documentation screenshots of the window, in every language.

    python scripts/docs_screenshots.py [--out docs/screenshots] [--lang en --lang ru]

Nothing here reads a real machine. The window is driven by ``DemoController``,
which answers every read with invented data -- projects, paths, chats and
snapshots that belong to nobody -- and a demo ``config.toml`` written into a
temporary directory. Screenshots of a real ``.codex`` would publish project
names, folder layouts and the opening lines of private chats.

Rendering is offscreen, so no window appears. Qt needs a font directory then,
or every glyph is drawn as a box; on Windows that is ``C:/Windows/Fonts``.
Mutating calls are not implemented: a screenshot never needs one, and a demo
that could write would be a demo that could write somewhere real.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from codexsync.app import (  # noqa: E402
    __version__,
    AutomationView,
    BackupSnapshotInfo,
    GuardianInventory,
    JournalInfo,
    create_config,
)
from codexsync.chat_directory import Association, ChatDirectory, ChatEntry, ChatKind, ProjectView  # noqa: E402
from codexsync.guardian_inventory import GuardianSnapshotInfo, QuarantineInfo  # noqa: E402
from codexsync.gui.controller import (  # noqa: E402
    BuildInfo,
    CheckRow,
    Controller,
    Outcome,
    RepairScan,
    SessionScan,
    StateView,
    SyncPreview,
)
from codexsync.gui.i18n import available_languages  # noqa: E402
from codexsync.gui.locations import WorkspaceCandidate  # noqa: E402
from codexsync.mapping_hints import MappingHints  # noqa: E402
from codexsync.repair_plan import RepairAction, RepairActionKind, RepairPlan  # noqa: E402
from codexsync.semantic_merge import BranchRelation  # noqa: E402
from codexsync.semantic_transfer import TransferAction, TransferItem, TransferPlan  # noqa: E402
from codexsync.session_catalog import SessionState  # noqa: E402
from codexsync.session_scope import SessionScope  # noqa: E402
from codexsync.sync_candidates import SyncCandidate  # noqa: E402
from codexsync.system_scheduler import SchedulerStatus  # noqa: E402

SIZE = (1280, 860)
MACHINE = "desktop"
OTHER = "laptop"
HOME = "C:/Users/you"
WORKSPACE = "D:/Cloud/codexSync"

PROJECTS = {
    "8f1c2a90-1d4e-4b6a-9a51-2c7e0b3f5a11": ("Atlas", "D:/Projects/atlas"),
    "2b7d4e13-6f0a-4c8b-8d22-91a5c3e7f024": ("Harbor", "D:/Projects/harbor"),
    "c49e7a55-3b21-4f9d-a6c8-0d5b2e8f7136": ("Lumen docs", "D:/Projects/lumen-docs"),
    "71a0d3c8-9e64-45b2-b7f1-6c2d8a4e9b57": ("Scratchpad", f"{HOME}/Documents/Codex/scratchpad"),
}

#: Opening lines of the demo chats, per language; English is the fallback.
TITLES = {
    "ru": (
        "Добавь повтор с задержкой в клиент загрузки",
        "Почему интеграционный тест падает по таймауту в CI?",
        "Разбей форму настроек на вкладки",
        "Переведи обработчик очереди на asyncio",
        "Проверь Dockerfile для сборки на ноутбуке",
        "Перепиши быстрый старт простым языком",
        "Собери changelog из влитых pull request",
        "Регулярка для дат ISO со смещением",
        "Набросай план бенчмарка парсера",
    ),
    "zh": (
        "给上传客户端加上带退避的重试",
        "集成测试为什么会在 CI 上超时？",
        "把设置表单拆成多个标签页",
        "把队列工作进程迁移到 asyncio",
        "检查一下笔记本构建用的 Dockerfile",
        "用平实的语言重写快速上手",
        "根据已合并的 pull request 生成更新日志",
        "匹配带可选时区偏移的 ISO 日期的正则",
        "为解析器起草一个基准测试方案",
    ),
}

CHATS = (
    # id prefix, project, association, day, records, title
    ("01a0b1c2", "Atlas", Association.BOUND, "2026-09-16T09:12:00Z", 1840, "Add retry with backoff to the upload client"),
    ("01a0b0f7", "Atlas", Association.DERIVED, "2026-09-15T17:40:00Z", 612, "Why does the integration test time out on CI?"),
    ("01a0ae31", "Atlas", Association.DERIVED, "2026-09-12T11:05:00Z", 2310, "Split the settings form into tabs"),
    ("01a0ad02", "Harbor", Association.DERIVED, "2026-09-14T08:30:00Z", 954, "Migrate the queue worker to asyncio"),
    ("01a0ab9e", "Harbor", Association.DERIVED_VIA_MAPPING, "2026-09-10T19:22:00Z", 377, "Review the Dockerfile for the laptop build"),
    ("01a0aa40", "Lumen docs", Association.DERIVED, "2026-09-13T14:48:00Z", 205, "Rewrite the quick start in plain language"),
    ("01a0a8d5", "Lumen docs", Association.BOUND, "2026-09-08T10:01:00Z", 128, "Generate a changelog from merged pull requests"),
    ("01a0a711", "Scratchpad", Association.DERIVED, "2026-09-11T21:15:00Z", 64, "Regex for ISO dates with an optional offset"),
    ("01a0a5c3", None, Association.NONE, "2026-09-05T16:33:00Z", 431, "Draft a benchmark plan for the parser"),
)


def _project_id(name: str | None) -> str | None:
    return next((pid for pid, (label, _) in PROJECTS.items() if label == name), None)


def _titles(language: str) -> tuple[str, ...]:
    return TITLES.get(language, tuple(chat[5] for chat in CHATS))


def _chat_directory(language: str) -> ChatDirectory:
    titles = _titles(language)
    projects = {pid: ProjectView(pid, name, (root,)) for pid, (name, root) in PROJECTS.items()}
    chats = []
    for index, (prefix, project, association, when, records, _title) in enumerate(CHATS):
        session_id = f"{prefix}-{index:04x}-7a3e-9b1c-4d2e6f8a0b{index:02d}"
        root = PROJECTS[_project_id(project)][1] if project else "E:/experiments/parser-bench"
        if association is Association.DERIVED_VIA_MAPPING:
            root = "C:/Work/harbor"
        chats.append(ChatEntry(
            session_id,
            f"sessions/{when[:4]}/{when[5:7]}/{when[8:10]}/rollout-{when[:10]}-{session_id}.jsonl",
            SessionState.ACTIVE, ChatKind.TOP_LEVEL, association, _project_id(project),
            when, root, records, title=titles[index], byte_count=records * 900,
        ))
    return ChatDirectory(projects, tuple(chats), "electron-v2")


def _hash(seed: int) -> str:
    return f"{seed:02x}" * 32


def _transfer_plan() -> TransferPlan:
    items = [
        TransferItem(_hash(1), BranchRelation.FAST_FORWARD_REMOTE, TransferAction.FAST_FORWARD_REMOTE,
                     _hash(11), _hash(12), 1840, 1702, "sessions/2026/09/16/rollout-a.jsonl.xz"),
        TransferItem(_hash(2), BranchRelation.FAST_FORWARD_LOCAL, TransferAction.FAST_FORWARD_LOCAL,
                     _hash(21), _hash(22), 540, 612, "sessions/2026/09/15/rollout-b.jsonl"),
        TransferItem(_hash(3), BranchRelation.IDENTICAL, TransferAction.NOOP, _hash(31), _hash(31), 2310, 2310),
        TransferItem(_hash(4), BranchRelation.IDENTICAL, TransferAction.NOOP, _hash(41), _hash(41), 954, 954),
        TransferItem(_hash(5), BranchRelation.DIVERGED, TransferAction.BLOCKED_CONFLICT, _hash(51), _hash(52),
                     377, 391, conflict_id=_hash(53)),
    ]
    return TransferPlan(1, _hash(90), "2026-09-17T09:00:00Z", OTHER, MACHINE, "unproven", "v1", False, tuple(items))


def _repair_plan() -> RepairPlan:
    atlas, harbor = _project_id("Atlas"), _project_id("Harbor")
    actions = (
        RepairAction(RepairActionKind.KEEP_PROJECT, _hash(61), _hash(71), atlas, None, "D:/Projects/atlas", "01a0b1c2"),
        RepairAction(RepairActionKind.ADD_BINDING, _hash(62), _hash(72), harbor, "laptop-to-desktop",
                     "D:/Projects/harbor", "01a0ab9e", "C:/Work/harbor", _hash(73)),
        RepairAction(RepairActionKind.KEEP_BINDING, _hash(63), _hash(71), atlas, None, "D:/Projects/atlas", "01a0ae31"),
        RepairAction(RepairActionKind.SKIP_UNMAPPED, _hash(64), None, None, None, None, "01a0a5c3",
                     "E:/experiments/parser-bench", _hash(74)),
    )
    return RepairPlan(1, _hash(91), "2026-09-17T09:00:00Z", OTHER, MACHINE, _hash(92), _hash(93),
                      "electron-v2", False, actions)


class DemoController(Controller):
    """Invented answers for every read the window makes; no writes at all.

    The demo config really exists, in a temporary directory, because the
    settings screen edits real text. Where that file lives is not shown: the
    window is told it is ``<workspace>/config.toml``.
    """

    def __init__(self, config_file: Path) -> None:
        super().__init__(config_file)
        self.language = "en"

    @property
    def config_path(self) -> Path:
        return Path(WORKSPACE) / "config.toml"

    def open_config(self) -> Outcome:
        opened = super().open_config()
        if not opened.ok:
            return opened
        document = replace(opened.value.document, path=self.config_path)
        return Outcome(value=replace(opened.value, document=document))

    def about(self) -> BuildInfo:
        """A build that could exist, never this one.

        The real ``about()`` reports ``sys.argv[0]`` and the config path, which
        on the machine rendering the documentation are a checkout path and a
        user name -- exactly what these screenshots exist to keep out.
        """
        return BuildInfo(
            version=__version__,
            frozen=True,
            executable=f"{HOME}/AppData/Local/CodexSync/CodexSync.exe",
            python="3.13.2",
            system="Windows 11",
            architecture="AMD64",
            config_path=str(self.config_path),
            config_exists=True,
        )

    def state(self) -> Outcome:
        rows = (
            CheckRow("config", "PASS", f"Loaded {WORKSPACE}/config.toml"),
            CheckRow("state_dirs", "PASS", f"local={HOME}/.codex cloud={WORKSPACE}/sync"),
            CheckRow("codex_process", "PASS", "Codex is not running"),
            CheckRow("global_state_schema", "PASS", "electron-v2"),
            CheckRow("guardian_latest_good", "PASS", "Restorable snapshot 20260917T081500.000000Z"),
            CheckRow("session_catalog", "PASS", "sessions=184 archived=21 invalid=0"),
            CheckRow("session_index", "PASS", "local 191 records, cloud 188"),
            CheckRow("sqlite_audit", "PASS", "state_5.sqlite read-only, no WAL frames"),
            CheckRow("sync_rules", "PASS", "direction=bidirectional; delete_policy=never"),
            CheckRow("project_registry", "WARN", "Projects also live in state_5.sqlite; a move edits the JSON only"),
            CheckRow("manifest", "PASS", "Previous run recorded 1 412 files"),
            CheckRow("orphan_temp_files", "PASS", "No orphan temp files"),
        )
        return Outcome(value=StateView(rows, True, 0, 1))

    def automation(self) -> Outcome:
        return Outcome(value=AutomationView(
            True, "guardian_snapshot", 300, True, 30, 0,
            (f"{HOME}/AppData/Local/CodexSync/CodexSync.exe", "-c", f"{WORKSPACE}/config.toml",
             "guardian", "snapshot", "--once"),
            (), True,
            SchedulerStatus(True, True, "2026-09-17T08:15:00Z", "2026-09-17T08:20:00Z", 0, "", True),
            None,
        ))

    def preview_sync(self) -> Outcome:
        return Outcome(value=SyncPreview(
            ("skills/review/SKILL.md", "plugins/linear/config.json"),
            ("skills/release-notes/SKILL.md", "skills/release-notes/template.md", "config.toml.d/aliases.toml"),
            (),
            False,
        ))

    def sync_candidates(self, relative: str = "") -> Outcome:
        if relative:
            return Outcome(value=())
        return Outcome(value=(
            SyncCandidate("skills", "skills", True, True, True, False, False),
            SyncCandidate("plugins", "plugins", True, True, True, False, False),
            SyncCandidate("sessions", "sessions", True, True, True, True, False),
            SyncCandidate("cache", "cache", True, True, False, False, True),
        ))

    def chats(self, *, source_machine=None, target_machine=None, progress=None) -> Outcome:
        return Outcome(value=_chat_directory(self.language))

    def mapping_hints(self, *, progress=None) -> Outcome:
        return Outcome(value=MappingHints(
            machines=(MACHINE, OTHER),
            chat_roots=(("C:/Work/harbor", 7), ("E:/experiments/parser-bench", 1)),
            unmapped_roots=("E:/experiments/parser-bench",),
            local_project_roots=("D:/Projects/harbor",),
            remote_project_roots=("C:/Work/harbor",),
        ))

    def session_index(self) -> Outcome:
        return Outcome(value={
            "local": {"present": True, "records": 191, "sessions": 184},
            "cloud": {"present": True, "records": 188, "sessions": 182},
            "only_local": 2, "only_cloud": 0, "differing": 0, "differing_ids": [],
            "contract_proven": False, "codes": ["UNPROVEN_CONSUMER_CONTRACT"],
        })

    def working_set(self, *, source_machine, target_machine) -> Outcome:
        return Outcome(value=SessionScope(
            projects=("Atlas", "Harbor"), chats=(), session_hashes=(_hash(1), _hash(2), _hash(4)),
            chat_count=31, total_bytes=148 * 1024 * 1024,
        ))

    def scan_sessions(self, *, source_machine, target_machine, progress=None) -> Outcome:
        plans = Path(WORKSPACE) / "plans"
        return Outcome(value=SessionScan(
            _transfer_plan(), plans / "sessions.json", plans / "resolutions.json", False,
            titles={_hash(n + 1): title for n, title in enumerate(_titles(self.language)[:5])},
        ))

    def scan_repair(self, *, source_machine, target_machine, progress=None) -> Outcome:
        return Outcome(value=RepairScan(_repair_plan(), Path(WORKSPACE) / "plans" / "repair.json"))

    def guardian_inventory(self) -> Outcome:
        def snap(stamp, generation, projects, bindings, status="PASS", codes=(), latest=False):
            return GuardianSnapshotInfo(
                f"{stamp}-3d9f2261-2da6-43ea-bc50-e6d36069aebb-{generation:012x}", generation,
                f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}.000000Z",
                612_000 + generation * 1_400, projects, bindings, status, codes, "electron-v2", True, True, latest,
            )

        return Outcome(value=GuardianInventory(
            MACHINE, Path(WORKSPACE) / "guardian", "latest",
            (
                snap("20260917T081500", 6, 4, 2, latest=True),
                snap("20260916T174210", 5, 4, 2),
                snap("20260916T101102", 4, 4, 1),
                snap("20260915T090033", 3, 4, 1, "PASS_WITH_WARNING", ("PROJECT_NOT_IN_ORDER",)),
                snap("20260912T183009", 2, 3, 1),
            ),
            (QuarantineInfo("20260914T120405.118000Z-5c0e7a1d-4f3b-4a2e-9d61-2b8c7e5f1a30",
                            "2026-09-14T12:04:05.118000Z", ("READ_CHANGED",), None),),
            (),
        ))

    def backups(self) -> Outcome:
        return Outcome(value=[
            BackupSnapshotInfo(f"{MACHINE}-20260917T080210Z-8c41d2e0a9b3.zip", True, True, False, 5, 2_480_311,
                               "2026-09-17T08:02:14Z", MACHINE, "2026-09-17T08:02:10Z", None),
            BackupSnapshotInfo(f"{MACHINE}-20260915T193344Z-1f7a0c6e5d28.zip", True, True, False, 12, 6_912_004,
                               "2026-09-15T19:33:51Z", MACHINE, "2026-09-15T19:33:44Z", None),
            BackupSnapshotInfo(f"{OTHER}-20260914T071502Z-b25e9d13c4f7.zip", True, True, False, 3, 871_220,
                               "2026-09-14T07:15:06Z", OTHER, "2026-09-14T07:15:02Z", None),
        ])

    def journals(self) -> Outcome:
        return Outcome(value=[
            JournalInfo("9a3e1c70-5b2d-4e8f-a164-0c7d2b9e3f51", "sync", "COMMITTED", "2026-09-17T08:02:10Z", 5,
                        f"{MACHINE}-20260917T080210Z-8c41d2e0a9b3.zip", True, True, False, False, True),
            JournalInfo("4d7b2e19-8c0a-4f63-b5e2-7a1c9d3e6f08", "chats", "COMMITTED", "2026-09-16T18:40:02Z", 1,
                        f"{MACHINE}-20260916T184002Z-0b9d3c7e2a14.zip", True, True, False, False, True),
            JournalInfo("e2c5a8f1-3d7b-4a90-8e26-5f1b0c4d7a93", "sessions", "COMMITTED", "2026-09-15T19:33:44Z",
                        12, f"{MACHINE}-20260915T193344Z-1f7a0c6e5d28.zip", True, True, False, False, True),
        ])

    def config_history(self) -> Outcome:
        return Outcome(value=[])


def _demo_config(directory: Path) -> Path:
    path = directory / "config.toml"
    create_config(
        path, machine_id=MACHINE, local_state_dir=f"{HOME}/.codex",
        workspace_root_dir=WORKSPACE, cloud_root_dir="${workspace_root}/sync",
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n[[path_mappings]]\n"
            f'rule_id = "{OTHER}-to-{MACHINE}"\n'
            f'source_machine = "{OTHER}"\n'
            f'target_machine = "{MACHINE}"\n'
            'from = "C:/Work"\n'
            'to = "D:/Projects"\n'
        )
    return path


class _InlineRunner:
    busy = False

    def start(self, call, done, progress=None) -> None:
        done(call(progress=progress) if progress is not None else call())


#: What each page does after it opens, so it shows a filled screen rather than
#: an empty one waiting for a button.
PREPARE = {
    "sessions": lambda screen: screen.scan(),
    "projects": lambda screen: screen.scan(),
}


def render(out_dir: Path, languages: list[str]) -> list[Path]:
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    from codexsync.gui.window import PAGES, MainWindow

    app = QApplication.instance() or QApplication([])
    written: list[Path] = []
    # The first-run screen proposes this machine's own name, `.codex` and any
    # workspace it finds in the cloud folders. On a demo it proposes the
    # second machine joining an existing workspace instead.
    first_run = "codexsync.gui.screens.first_run"
    workspace = WorkspaceCandidate(Path(WORKSPACE), ("backups", "guardian", "sync"), (MACHINE,))
    with tempfile.TemporaryDirectory(prefix="codexsync-demo-") as temp,             mock.patch(f"{first_run}.find_workspaces", return_value=Outcome(value=(workspace,))),             mock.patch(f"{first_run}.suggested_machine_id", return_value=OTHER),             mock.patch(f"{first_run}.suggested_codex_dir", return_value=Path(HOME) / ".codex"):
        controller = DemoController(_demo_config(Path(temp)))
        for language in languages:
            target = out_dir / language
            target.mkdir(parents=True, exist_ok=True)
            controller.language = language
            window = MainWindow(controller, language=language, runner=_InlineRunner())
            window.confirm = lambda *args, **kwargs: False
            window.resize(*SIZE)
            window.show()
            for number, page in enumerate(PAGES, start=1):
                window.go_to(page)
                for _ in range(5):
                    QCoreApplication.processEvents()
                prepare = PREPARE.get(page)
                if prepare is not None:
                    prepare(window.screen(page))
                for _ in range(5):
                    QCoreApplication.processEvents()
                path = target / f"{number:02d}-{page.replace('_', '-')}.png"
                window.grab().save(str(path))
                written.append(path)
            window.close()
            window.deleteLater()
            QCoreApplication.processEvents()
    del app
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(REPO / "docs" / "screenshots"))
    parser.add_argument("--lang", action="append", choices=available_languages())
    args = parser.parse_args(argv)
    for path in render(Path(args.out), args.lang or list(available_languages())):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
