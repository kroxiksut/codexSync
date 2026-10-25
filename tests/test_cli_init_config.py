from __future__ import annotations

import os
from pathlib import Path
import shutil
from unittest import mock
import unittest
import uuid

from codexsync.cli import main


class CliInitConfigTests(unittest.TestCase):
    def test_init_config_writes_template_file(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"init-config-{uuid.uuid4().hex}"
        output = root / "config.toml"
        try:
            code = main(["init-config", "--output", str(output)])
            self.assertEqual(code, 0)
            self.assertTrue(output.exists())
            text = output.read_text(encoding="utf-8")
            self.assertIn("[sync]", text)
            self.assertIn("[paths]", text)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_init_config_requires_force_for_overwrite(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"init-config-force-{uuid.uuid4().hex}"
        output = root / "config.toml"
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("original", encoding="utf-8")
            code = main(["init-config", "--output", str(output)])
            self.assertEqual(code, 4)
            self.assertEqual(output.read_text(encoding="utf-8"), "original")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_init_config_force_overwrites_existing_file(self) -> None:
        root = Path.cwd() / "test-sandbox" / f"init-config-overwrite-{uuid.uuid4().hex}"
        output = root / "config.toml"
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("old", encoding="utf-8")
            code = main(["init-config", "--output", str(output), "--force"])
            self.assertEqual(code, 0)
            text = output.read_text(encoding="utf-8")
            self.assertIn("[sync]", text)
            self.assertNotEqual(text, "old")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_init_config_force_refuses_codex_own_config(self) -> None:
        # CS-433: --force must not replace Codex's config.toml in CODEX_HOME.
        root = Path.cwd() / "test-sandbox" / f"init-config-codex-{uuid.uuid4().hex}"
        home = root / "codex-home"
        output = home / "config.toml"
        try:
            home.mkdir(parents=True)
            output.write_text("model = 'x'\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
                code = main(["init-config", "--output", str(output), "--force"])
                self.assertEqual(code, 4)
                missing = home / "sub" / "new.toml"
                self.assertEqual(main(["init-config", "--output", str(missing)]), 4)
            self.assertEqual(output.read_text(encoding="utf-8"), "model = 'x'\n")
            self.assertFalse((home / "sub").exists())
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
