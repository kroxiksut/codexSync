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
                platform, command=[str(executable), "-m", "codexsync"], config_path=config, log_dir=logs,
                user_id=r"PC\me",
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
            render_scheduler_templates("linux", command=["python", "-m", "codexsync"], config_path=config, log_dir=logs)
        with self.assertRaises(ValueError):
            render_scheduler_templates(
                "linux", command=[str(executable)], config_path=config, log_dir=logs, interval_seconds=30
            )


class TemplateCommandTests(unittest.TestCase):
    """CS-543: the templates ran `python -c config.toml ...` -- no `-m codexsync`."""

    def setUp(self) -> None:
        self.python = _absolute("Program Files/Python/pythonw.exe")
        self.config = _absolute("Users/me/My $Docs/config.toml")
        self.logs = _absolute("Users/me/logs")

    def render(self, platform: str) -> str:
        templates = render_scheduler_templates(
            platform, command=[str(self.python), "-m", "codexsync"], config_path=self.config, log_dir=self.logs,
            user_id=r"PC\me",
        )
        return b"".join(templates.files.values()).decode("utf-16" if platform == "windows" else "utf-8")

    def test_windows_runs_the_package_for_this_user(self) -> None:
        text = self.render("windows")
        self.assertIn(f"<Command>{self.python}</Command>", text)
        self.assertIn("<Arguments>-m codexsync -c", text)
        self.assertIn(f"&quot;{self.config}&quot; guardian snapshot --once", text)
        self.assertEqual(text.count(r"<UserId>PC\me</UserId>"), 2, "trigger and principal name the user")

    def test_macos_passes_each_argument_as_it_is(self) -> None:
        import plistlib

        templates = render_scheduler_templates(
            "macos", command=[str(self.python), "-m", "codexsync"], config_path=self.config, log_dir=self.logs,
        )
        plist = plistlib.loads(templates.files["io.codexsync.guardian.plist"])
        self.assertEqual(
            plist["ProgramArguments"],
            [str(self.python), "-m", "codexsync", "-c", str(self.config), "guardian", "snapshot", "--once"],
        )

    def test_linux_quotes_for_systemd_not_for_a_shell(self) -> None:
        text = self.render("linux")
        exec_start = next(line for line in text.splitlines() if line.startswith("ExecStart="))
        self.assertIn(" -m codexsync -c ", exec_start)
        # systemd expands `$` even inside single quotes, which shlex.quote used.
        self.assertIn("$$Docs", exec_start)
        self.assertNotIn("'", exec_start)


if __name__ == "__main__":
    unittest.main()
