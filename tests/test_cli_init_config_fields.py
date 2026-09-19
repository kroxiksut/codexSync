"""`init-config` with real values: written through the validating writer.

The plain form keeps generating the template (AI_RULES 5); these tests cover
the second form, where the values arrive on the command line and nothing is
written unless the result validates.
"""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
import shutil
import unittest
import uuid

from codexsync.cli import main
from codexsync.config import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX = REPO_ROOT / "test-sandbox"
TEMPLATE = REPO_ROOT / "src" / "codexsync" / "config.example.toml"


def _comments(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip().startswith("#")]


class CliInitConfigFieldsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"cli-init-fields-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.state = self.root / "codex-state"
        self.workspace = self.root / "workspace"
        self.output = self.root / "settings" / "config.toml"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, *extra: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return main(["init-config", "--output", str(self.output), *extra])

    def _values(self) -> list[str]:
        return [
            "--machine-id", "laptop-1",
            "--local-state-dir", self.state.as_posix(),
            "--workspace-root", self.workspace.as_posix(),
        ]

    def test_values_are_written_validated_and_keep_every_template_comment(self) -> None:
        code = self._run(*self._values())

        self.assertEqual(code, 0)
        cfg = load_config(self.output)
        self.assertEqual(cfg.identity.machine_id, "laptop-1")
        self.assertEqual(cfg.paths.local_state_dir, self.state.resolve())
        self.assertEqual(cfg.paths.workspace_root_dir, self.workspace.resolve())
        written = self.output.read_text(encoding="utf-8")
        self.assertEqual(_comments(written), _comments(TEMPLATE.read_text(encoding="utf-8")))
        # Nothing was created inside the Codex state directory.
        self.assertFalse(self.state.exists())

    def test_cloud_root_is_optional_and_carried_when_given(self) -> None:
        cloud = self.root / "cloud"
        code = self._run(*self._values(), "--cloud-root", cloud.as_posix())

        self.assertEqual(code, 0)
        self.assertEqual(load_config(self.output).paths.cloud_root_dir, cloud.resolve())

    def test_a_missing_required_value_writes_nothing(self) -> None:
        for dropped in ("--machine-id", "--local-state-dir", "--workspace-root"):
            with self.subTest(dropped=dropped):
                values = self._values()
                index = values.index(dropped)
                del values[index:index + 2]
                with self.assertLogs("codexsync.cli", level="ERROR") as logs:
                    code = self._run(*values)
                self.assertEqual(code, 4)
                self.assertIn(dropped, "\n".join(logs.output))
                self.assertFalse(self.output.exists())

    def test_cloud_root_alone_is_not_the_plain_template(self) -> None:
        code = self._run("--cloud-root", (self.root / "cloud").as_posix())

        self.assertEqual(code, 4)
        self.assertFalse(self.output.exists())

    def test_an_existing_file_is_refused_and_left_untouched(self) -> None:
        self.output.parent.mkdir(parents=True)
        self.output.write_text("original", encoding="utf-8")

        code = self._run(*self._values())

        self.assertEqual(code, 4)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "original")

    def test_force_is_refused_with_values_even_over_an_existing_file(self) -> None:
        self.output.parent.mkdir(parents=True)
        self.output.write_text("original", encoding="utf-8")

        with self.assertLogs("codexsync.cli", level="ERROR") as logs:
            code = self._run(*self._values(), "--force")

        self.assertEqual(code, 4)
        self.assertIn("--force", "\n".join(logs.output))
        self.assertEqual(self.output.read_text(encoding="utf-8"), "original")

    def test_an_invalid_value_writes_nothing(self) -> None:
        values = self._values()
        values[1] = "..."
        code = self._run(*values)

        self.assertEqual(code, 4)
        self.assertFalse(self.output.exists())

    def test_the_plain_form_still_writes_the_unchanged_template(self) -> None:
        code = self._run()

        self.assertEqual(code, 0)
        self.assertEqual(
            self.output.read_text(encoding="utf-8"), TEMPLATE.read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
