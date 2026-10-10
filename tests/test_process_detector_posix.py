"""Reading `ps` on macOS and Linux, against recorded output.

The listings below are the shapes that break a naive parser: macOS prints a
full path in `comm` and the helper names contain spaces and brackets, Linux
cuts every name to fifteen characters. Both are matched here without a live
machine, which is also the limit of what these tests prove -- whether a running
Codex really produces these lines is what
`docs/dev/experiments/process-detector-macos.md` is for, and until someone runs it
`capability()` keeps the platform closed. Linux has been run (2026-10-09), and
`LinuxObservedTests` holds what that machine printed.
"""
from __future__ import annotations

import unittest
from unittest import mock

from codexsync.process_detector import (
    PROVEN_DETECTORS,
    CodexProcessDetector,
    ProcessInfo,
    _match_posix,
    _parse_ps,
)

# `ps -A -o pid=,comm=` on macOS 15 with the desktop build open.
MACOS_COMM = """\
  501 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT
  502 /Applications/ChatGPT.app/Contents/Frameworks/ChatGPT Helper (Renderer).app/Contents/MacOS/ChatGPT Helper (Renderer)
  503 /Applications/ChatGPT.app/Contents/Frameworks/ChatGPT Helper (GPU).app/Contents/MacOS/ChatGPT Helper (GPU)
  504 /Users/someone/.codex/bin/codex-app-server
  505 /Applications/ChatGPT.app/Contents/Resources/codex-execve-wrapper
  506 /System/Library/CoreServices/Finder.app/Contents/MacOS/Finder
  507 /Applications/ChatGPT.app/Contents/MacOS/chrome_crashpad_handler
"""

# A machine with the ordinary ChatGPT app and no Codex anywhere.
MACOS_INNOCENT = """\
  601 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT
"""
MACOS_INNOCENT_COMMAND = """\
  601 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT --enable-features=Foo
"""

# `ps -A -o pid=,comm=` on Linux: every name cut to 15 characters.
LINUX_COMM = """\
  900 codex
  901 codex-app-serve
  902 codex-linux-san
  903 codex-execve-wr
  904 node
  905 bwrap
"""
LINUX_COMMAND = """\
  900 codex --profile default
  901 /usr/lib/chatgpt/codex-app-server
  902 /usr/lib/chatgpt/codex-linux-sandbox /bin/sh
  903 /usr/lib/chatgpt/codex-execve-wrapper
  904 node /home/someone/project/server.js
  905 bwrap --dev-bind / /
"""


def processes(comm: str, command: str = "") -> list[ProcessInfo]:
    names = _parse_ps(comm)
    commands = _parse_ps(command)
    return [
        ProcessInfo(pid=pid, name=name, command_line=commands.get(pid, ""))
        for pid, name in sorted(names.items())
    ]


class ParsingTests(unittest.TestCase):
    def test_a_name_with_spaces_and_brackets_survives_the_split(self) -> None:
        rows = _parse_ps(MACOS_COMM)
        self.assertTrue(rows[502].endswith("ChatGPT Helper (Renderer)"))
        self.assertEqual(len(rows), 7)

    def test_a_line_without_a_pid_is_skipped_rather_than_raised(self) -> None:
        self.assertEqual(_parse_ps("header line\n  7 codex\n"), {7: "codex"})


class MacOsMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("codexsync.process_detector.sys.platform", "darwin")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.listing = processes(MACOS_COMM)

    def test_the_desktop_build_is_found_by_its_bundle_path(self) -> None:
        found = _match_posix(self.listing, ["ChatGPT.app/Contents/MacOS/"])
        self.assertEqual([proc.pid for proc in found], [501, 507])

    def test_a_bare_chatgpt_name_would_match_the_app_itself(self) -> None:
        """Which is why the shipped list names a path and never that name.

        The matcher compares whole basenames, and the desktop build's basename
        *is* `ChatGPT` -- so the rule cannot live in the matcher. It lives in
        the template, and the next test is what holds it there.
        """
        self.assertTrue(_match_posix(self.listing, ["ChatGPT"]))

    def test_the_shipped_list_names_no_bare_app_name(self) -> None:
        import tomllib
        from pathlib import Path

        template = Path(__file__).resolve().parents[1] / "src" / "codexsync" / "config.example.toml"
        shipped = tomllib.loads(template.read_text(encoding="utf-8"))
        names = shipped["process_detection"]["background_process_names"]
        for platform, entries in names.items():
            for entry in entries:
                with self.subTest(platform=platform, entry=entry):
                    self.assertNotIn(
                        entry.strip().casefold(), {"chatgpt", "codex", "node", "electron", "bwrap", "sandbox-exec"},
                        "too general to match by name; use a path marker",
                    )

    def test_the_app_server_and_the_wrapper_are_found_by_name(self) -> None:
        found = _match_posix(self.listing, ["codex-app-server", "codex-execve-wrapper"])
        self.assertEqual([proc.pid for proc in found], [504, 505])

    def test_an_unrelated_process_is_not_matched(self) -> None:
        self.assertEqual(_match_posix(self.listing, ["codex", "codex-app-server"]), [
            proc for proc in self.listing if proc.pid == 504
        ])

    def test_a_machine_with_only_the_chatgpt_app_still_matches_the_path_marker(self) -> None:
        """This is the case the marker is deliberately broad about.

        `ChatGPT.app` *is* the Codex desktop build since 2026-07-09, so a
        machine that has it reads as running. Being wrong in this direction
        refuses a sync; being wrong the other way writes into a live state.
        """
        listing = processes(MACOS_INNOCENT, MACOS_INNOCENT_COMMAND)
        self.assertTrue(_match_posix(listing, ["ChatGPT.app/Contents/MacOS/"]))


class LinuxMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("codexsync.process_detector.sys.platform", "linux")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.listing = processes(LINUX_COMM, LINUX_COMMAND)

    def test_a_truncated_name_matches_the_full_configured_one(self) -> None:
        found = _match_posix(self.listing, ["codex-app-server", "codex-linux-sandbox"])
        self.assertEqual([proc.pid for proc in found], [901, 902])

    def test_a_short_name_still_matches_exactly(self) -> None:
        self.assertEqual([proc.pid for proc in _match_posix(self.listing, ["codex"])], [900])

    def test_a_truncation_is_not_a_prefix_match(self) -> None:
        """`codex` must not match `codex-linux-san`: that is a different program."""
        found = _match_posix(self.listing, ["codex"])
        self.assertNotIn(902, [proc.pid for proc in found])

    def test_a_shared_runtime_is_never_matched_by_name(self) -> None:
        self.assertEqual(_match_posix(self.listing, ["node", "bwrap"]), [
            proc for proc in self.listing if proc.pid in (904, 905)
        ], "these names are matchable, which is exactly why the template does not list them")

    def test_the_install_path_marker_finds_the_helpers(self) -> None:
        found = _match_posix(self.listing, ["/usr/lib/chatgpt/"])
        self.assertEqual([proc.pid for proc in found], [901, 902, 903])


# Recorded on Ubuntu 26.04 with the Codex desktop deb 26.1002.52244 while a
# chat ran `sleep 600` in the sandbox (2026-10-09), trimmed to one process of
# each kind; home directory and working folder renamed. Renderer, zygote and
# GPU processes are more `ChatGPT` lines under the same path.
LINUX_OBSERVED_COMM = """\
 27204 ChatGPT
 27209 browser_crashpa
 28047 codex
 193343 codex-code-mode
 193758 codex-linux-san
 193763 bwrap
 193764 codex
 193765 sleep
"""
LINUX_OBSERVED_COMMAND = """\
 27204 /usr/lib/chatgpt/ChatGPT
 27209 /usr/lib/chatgpt/browser_crashpad_handler --monitor-self
 28047 /usr/lib/chatgpt/resources/codex -c features.code_mode_host=true app-server
 193343 /usr/lib/chatgpt/resources/codex-code-mode-host
 193758 /home/someone/.codex/tmp/arg0/codex-arg0j0lyuT/codex-linux-sandbox --sandbox-policy-cwd /home/someone/work
 193763 bwrap --as-pid-1 --new-session --die-with-parent --ro-bind / /
 193764 codex-linux-sandbox --sandbox-policy-cwd /home/someone/work
 193765 sleep 600
"""
# The same machine after the app was asked to quit (SIGTERM to the main
# process): the window, the app-server and the outer helper were gone, the
# command and the sandbox process holding it were not -- re-parented to the
# user's systemd and alive until `sleep` ended nine minutes later.
LINUX_AFTER_QUIT_COMM = """\
 1613 systemd
 193764 codex
 193765 sleep
"""
LINUX_AFTER_QUIT_COMMAND = """\
 1613 /usr/lib/systemd/systemd --user
 193764 codex-linux-sandbox --sandbox-policy-cwd /home/someone/work
 193765 sleep 600
"""


class LinuxObservedTests(unittest.TestCase):
    """The shipped names against what a live Linux Codex actually produced."""

    def setUp(self) -> None:
        patcher = mock.patch("codexsync.process_detector.sys.platform", "linux")
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def shipped() -> tuple[list[str], list[str]]:
        from codexsync.process_knowledge import BACKGROUND_PROCESS_NAMES, PROCESS_NAMES

        return list(PROCESS_NAMES), list(BACKGROUND_PROCESS_NAMES["linux"])

    def test_the_open_app_is_found_by_name_and_by_its_install_path(self) -> None:
        names, markers = self.shipped()
        listing = processes(LINUX_OBSERVED_COMM, LINUX_OBSERVED_COMMAND)
        self.assertEqual([p.pid for p in _match_posix(listing, names)], [28047, 193764])
        background = {p.pid for p in _match_posix(listing, markers)}
        self.assertTrue({27204, 27209, 28047, 193343, 193758} <= background)
        self.assertNotIn(193765, background, "the user's own command is not a marker")

    def test_the_sandbox_helper_lives_outside_the_install_path(self) -> None:
        """So the path marker alone would miss it; the truncated name finds it."""
        listing = processes(LINUX_OBSERVED_COMM, LINUX_OBSERVED_COMMAND)
        helper = [p for p in listing if p.pid == 193758]
        self.assertEqual(_match_posix(helper, ["/usr/lib/chatgpt/"]), [])
        self.assertEqual(_match_posix(helper, ["codex-linux-sandbox"]), helper)

    def test_a_command_that_outlives_the_app_still_reads_as_running(self) -> None:
        """Codex's work is still in flight, so a write must still wait."""
        names, markers = self.shipped()
        listing = processes(LINUX_AFTER_QUIT_COMM, LINUX_AFTER_QUIT_COMMAND)
        self.assertEqual([p.pid for p in _match_posix(listing, names + markers)], [193764])


class CapabilityTests(unittest.TestCase):
    def test_an_unproven_platform_is_not_supported_and_says_why(self) -> None:
        with mock.patch("codexsync.process_detector.sys.platform", "darwin"):
            capability = CodexProcessDetector(["codex"]).capability()
        self.assertFalse(capability.supported)
        self.assertIn("docs/dev/experiments", capability.detail)

    def test_a_proven_platform_becomes_supported(self) -> None:
        with mock.patch("codexsync.process_detector.sys.platform", "darwin"), \
                mock.patch.dict(PROVEN_DETECTORS, {"darwin": "macOS 15.5, Codex 0.51"}, clear=False):
            capability = CodexProcessDetector(["codex"]).capability()
        self.assertTrue(capability.supported)
        self.assertIn("macOS 15.5", capability.detail)

    def test_only_an_observed_platform_is_in_the_gate(self) -> None:
        """Filled from a live machine, never from reading the parser.

        Linux was watched on 2026-10-09 (`LinuxObservedTests` replays it); no
        Mac has been, so `darwin` must not appear until one is.
        """
        self.assertEqual(set(PROVEN_DETECTORS), {"linux"})
        self.assertIn("observed 2026-10-09", PROVEN_DETECTORS["linux"])

    def test_an_unsupported_platform_leaves_the_state_unknown(self) -> None:
        """And `fail_on_unknown` turns unknown into a refusal, not into a write."""
        from codexsync.models import ProcessDetectionConfig
        from codexsync.safety_gate import ProcessState

        self.assertIsNotNone(ProcessState.UNKNOWN)
        self.assertTrue(hasattr(ProcessDetectionConfig, "background_process_names"))


if __name__ == "__main__":
    unittest.main()
