from __future__ import annotations

from pathlib import Path
import unittest

from codexsync.scheduler import render_scheduler_templates


class SchedulerTemplateTests(unittest.TestCase):
    def test_all_platforms_run_only_guardian_once_without_secrets(self) -> None:
        executable = Path("C:/Program Files/codexsync/python.exe")
        config = Path("C:/Users/test/codexsync/config.toml")
        logs = Path("C:/Users/test/codexsync/logs")
        for platform in ("windows", "macos", "linux"):
            templates = render_scheduler_templates(
                platform, executable=executable, config_path=config, log_dir=logs
            )
            rendered = b"\n".join(templates.files.values()).decode("utf-16" if platform == "windows" else "utf-8")
            self.assertIn("guardian", rendered)
            self.assertIn("snapshot", rendered)
            self.assertIn("--once", rendered)
            self.assertNotIn(" sync ", rendered)
            self.assertNotIn(" restore ", rendered)
            self.assertNotIn("api_key", rendered.lower())
            self.assertNotIn("bearer ", rendered.lower())
            self.assertNotIn("password", rendered.lower())

    def test_rejects_relative_paths_and_too_short_interval(self) -> None:
        with self.assertRaises(ValueError):
            render_scheduler_templates("linux", executable=Path("python"), config_path=Path("/c"), log_dir=Path("/l"))
        with self.assertRaises(ValueError):
            render_scheduler_templates("linux", executable=Path("/python"), config_path=Path("/c"), log_dir=Path("/l"), interval_seconds=30)


if __name__ == "__main__":
    unittest.main()
