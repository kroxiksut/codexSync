from __future__ import annotations

from pathlib import Path
import os
import unittest

from codexsync.scheduler import render_scheduler_templates

# The templates are plain text, but the caller-supplied paths are checked with
# Path.is_absolute(), which is host-shaped: "C:/x" is relative on POSIX and
# "/x" is relative on Windows.
_ABSOLUTE_PREFIX = "C:/" if os.name == "nt" else "/"


def _absolute(relative: str) -> Path:
    return Path(_ABSOLUTE_PREFIX + relative)


class SchedulerTemplateTests(unittest.TestCase):
    def test_all_platforms_run_only_guardian_once_without_secrets(self) -> None:
        executable = _absolute("Program Files/codexsync/python.exe")
        config = _absolute("Users/test/codexsync/config.toml")
        logs = _absolute("Users/test/codexsync/logs")
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
        executable = _absolute("python")
        config = _absolute("c")
        logs = _absolute("l")
        with self.assertRaises(ValueError):
            render_scheduler_templates("linux", executable=Path("python"), config_path=config, log_dir=logs)
        with self.assertRaises(ValueError):
            render_scheduler_templates(
                "linux", executable=executable, config_path=config, log_dir=logs, interval_seconds=30
            )


if __name__ == "__main__":
    unittest.main()
