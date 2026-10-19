"""The Codex state folder a command works on is proven clear of codexSync's own (CS-287).

Loading a config compares every folder codexSync writes with
`paths.local_state_dir` *as configured*. When that folder does not exist, the
locator falls back to CODEX_HOME or ~/.codex -- a folder loading never saw. A
backup, temp or mirror folder that happened to sit inside it would then have
put codexSync's own writes into the Codex state directory.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import textwrap
import unittest
from unittest import mock
import uuid

from codexsync.config import external_roots, load_config
from codexsync.exceptions import ConfigError
from codexsync.preflight import run_preflight
from codexsync.state_locator import locate_local_state_dir, locate_state_dirs

SANDBOX = Path(__file__).resolve().parents[1] / "test-sandbox"

FIELDS = {
    "paths.cloud_root_dir": ("paths", "cloud_root_dir"),
    "paths.backup_dir": ("paths", "backup_dir"),
    "paths.temp_dir": ("paths", "temp_dir"),
    "guardian.root_dir": ("guardian", "root_dir"),
    "semantic.root_dir": ("semantic", "root_dir"),
    "state.manifest_file": ("state", "manifest_file"),
    "state_backup.root_dir": ("state_backup", "root_dir"),
    "logging.file": ("logging", "file"),
    "handoff.root_dir": ("handoff", "root_dir"),
}


class LocateStateDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"state-locator-{uuid.uuid4().hex[:8]}"
        self.addCleanup(shutil.rmtree, self.root, True)
        #: Where the fallback lands: CODEX_HOME, since the configured one is missing.
        self.codex = self.root / "codex-home"
        self.codex.mkdir(parents=True)
        (self.root / "outside" / "sync").mkdir(parents=True)
        env = mock.patch.dict("os.environ", {"CODEX_HOME": str(self.codex)})
        env.start()
        self.addCleanup(env.stop)

    def _config(self, inside: str | None = None) -> Path:
        outside = (self.root / "outside").as_posix()
        values = {
            ("paths", "cloud_root_dir"): f"{outside}/sync",
            ("paths", "backup_dir"): f"{outside}/backups",
            ("paths", "temp_dir"): f"{outside}/tmp",
            ("guardian", "root_dir"): f"{outside}/guardian",
            ("semantic", "root_dir"): f"{outside}/semantic",
            ("state", "manifest_file"): f"{outside}/manifest.json",
            ("state_backup", "root_dir"): f"{outside}/copies",
            ("logging", "file"): f"{outside}/logs/codexsync.log",
        }
        if inside is not None:
            values[FIELDS[inside]] = f"{self.codex.as_posix()}/{FIELDS[inside][1]}"
        sections: dict[str, list[str]] = {}
        for (section, key), value in values.items():
            sections.setdefault(section, []).append(f'{key} = "{value}"')
        body = "\n\n".join(f"[{name}]\n" + "\n".join(lines) for name, lines in sections.items())
        path = self.root / "config.toml"
        path.write_text(textwrap.dedent(f"""
            [identity]
            machine_id = "machine-a"

        """) + body.replace(
            "[paths]\n", f'[paths]\nlocal_state_dir = "{(self.root / "missing-codex").as_posix()}"\n'
        ) + "\n", encoding="utf-8")
        return path

    def test_every_external_root_is_covered(self) -> None:
        cfg = load_config(self._config())
        self.assertEqual({name for name, _ in external_roots(cfg)}, set(FIELDS))

    def test_a_clear_config_finds_the_fallback(self) -> None:
        cfg = load_config(self._config())
        self.assertEqual(locate_local_state_dir(cfg), self.codex)
        self.assertEqual(locate_state_dirs(cfg)[0], self.codex)

    def test_any_folder_inside_the_fallback_is_refused(self) -> None:
        for field in FIELDS:
            with self.subTest(field=field):
                # Loading passes: it compares with the configured, missing folder.
                cfg = load_config(self._config(inside=field))
                with self.assertRaisesRegex(ConfigError, f"{field} .*Codex state folder in use"):
                    locate_local_state_dir(cfg)
                with self.assertRaisesRegex(ConfigError, field):
                    locate_state_dirs(cfg)

    def test_doctor_reports_it_instead_of_passing(self) -> None:
        path = self._config(inside="paths.backup_dir")
        with mock.patch("codexsync.runtime.collect_process_snapshot"):
            report = run_preflight(path)
        check = next(item for item in report.checks if item.name == "state_dirs")
        self.assertEqual(check.status, "FAIL")
        self.assertIn("paths.backup_dir", check.details)

    def test_a_fresh_machine_without_the_workspace_folders_is_a_warning(self) -> None:
        # CS-413: the first sync creates codexSync's own folders; until then
        # `doctor` says so, creates nothing, and still checks `.codex`.
        path = self._config()
        cfg = load_config(path)
        cfg.paths.cloud_root_dir.rmdir()
        with mock.patch("codexsync.runtime.collect_process_snapshot"):
            report = run_preflight(path)
        checks = {item.name: item for item in report.checks}
        self.assertEqual(checks["state_dirs"].status, "WARN")
        self.assertIn("the first sync creates it", checks["state_dirs"].details)
        for name in ("backup_dir", "temp_dir"):
            self.assertEqual(checks[name].status, "WARN", name)
        self.assertIn("codex_compat", checks, "the checks of .codex still run")
        self.assertIn("local_state", checks)
        self.assertFalse(cfg.paths.cloud_root_dir.exists(), "doctor creates nothing")
        self.assertFalse(cfg.paths.backup_dir.exists())

    def test_a_cloud_path_that_is_a_file_still_fails(self) -> None:
        path = self._config()
        cfg = load_config(path)
        cfg.paths.cloud_root_dir.rmdir()
        cfg.paths.cloud_root_dir.write_text("not a folder", encoding="utf-8")
        with mock.patch("codexsync.runtime.collect_process_snapshot"):
            report = run_preflight(path)
        check = next(item for item in report.checks if item.name == "state_dirs")
        self.assertEqual(check.status, "FAIL")


if __name__ == "__main__":
    unittest.main()
