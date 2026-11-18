"""Project folders Codex's Windows sandbox cannot protect (CS-483).

The probe is injected, so each owner case is a planted descriptor answer; one
test on Windows runs the real security API against a folder this user made.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid

from codexsync.codex_sandbox import (
    ADMINISTRATORS_SID,
    FolderAccess,
    OwnerKind,
    SandboxReport,
    check_sandbox_folders,
    fix_command,
    powershell_literal,
)

SANDBOX = Path(__file__).resolve().parent.parent / "test-sandbox"
USER = "DESKTOP\\owner"


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SANDBOX / f"codex-sandbox-{uuid.uuid4().hex[:8]}"
        self.codex = self.root / ".codex"
        (self.codex / ".sandbox").mkdir(parents=True)
        self.project = self.root / "project"
        for name in (".codex", ".agents", ".git"):
            (self.project / name).mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.answers: dict[str, FolderAccess] = {}

    def probe(self, folder: Path) -> FolderAccess | None:
        return self.answers.get(folder.name, FolderAccess(USER, "S-1-5-21-1", True))

    def check(self, *roots: str, platform: str = "win32") -> SandboxReport:
        return check_sandbox_folders(
            self.codex, roots or (str(self.project),), probe=self.probe, user=USER, platform=platform,
        )

    def log(self, *lines: str, day: str = "2026-10-25") -> None:
        text = "".join(f"[{day}T10:00:00+00:00] {line}\n" for line in lines)
        (self.codex / ".sandbox" / f"sandbox.{day}.log").write_text(text, encoding="utf-8")


class OwnerTests(_Case):
    def test_folders_this_user_owns_are_clear(self) -> None:
        report = self.check()
        self.assertEqual((report.checked, report.findings), (3, ()))
        self.assertEqual(report.owners, {"user": 3})

    def test_a_folder_the_sandbox_account_owns_is_a_finding(self) -> None:
        self.answers[".codex"] = FolderAccess("DESKTOP\\CodexSandboxOffline", "S-1-5-21-9", False)
        (finding,) = self.check().findings
        self.assertEqual((finding.name, finding.owner_kind), (".codex", OwnerKind.SANDBOX))
        self.assertEqual(finding.folder, str(self.project / ".codex"))

    def test_the_sandbox_account_is_known_by_the_name_codex_recorded(self) -> None:
        (self.codex / ".sandbox" / "setup_marker.json").write_text(
            '{"offline_username": "SbxOff", "online_username": "SbxOn"}', encoding="utf-8",
        )
        self.answers[".agents"] = FolderAccess("PC\\SbxOn", "S-1-5-21-9", False)
        (finding,) = self.check().findings
        self.assertEqual(finding.owner_kind, OwnerKind.SANDBOX)

    def test_a_folder_administrators_own_is_a_finding(self) -> None:
        self.answers[".git"] = FolderAccess("BUILTIN\\Administratoren", ADMINISTRATORS_SID, False)
        (finding,) = self.check().findings
        self.assertEqual(finding.owner_kind, OwnerKind.ADMINISTRATORS)

    def test_another_owner_with_an_explicit_write_dac_entry_is_not_a_finding(self) -> None:
        self.answers[".codex"] = FolderAccess("DESKTOP\\CodexSandboxOffline", "S-1-5-21-9", True)
        report = self.check()
        self.assertEqual(report.findings, ())
        self.assertEqual(report.owners, {"sandbox": 1, "user": 2})

    def test_a_descriptor_that_cannot_be_read_is_a_finding_with_no_owner(self) -> None:
        self.answers[".git"] = FolderAccess(None, None, False)
        (finding,) = self.check().findings
        self.assertEqual((finding.owner, finding.owner_kind), (None, OwnerKind.UNKNOWN))

    def test_the_project_root_itself_is_not_checked(self) -> None:
        # Codex's setup writes deny entries only on the folders inside it.
        seen: list[str] = []
        self.probe = lambda folder: seen.append(folder.name) or FolderAccess(USER, "S", True)  # type: ignore[method-assign]
        self.check()
        self.assertEqual(sorted(seen), [".agents", ".codex", ".git"])

    def test_missing_folders_and_foreign_roots_are_skipped(self) -> None:
        shutil.rmtree(self.project / ".agents")
        report = self.check(str(self.project), "/home/someone/project", "relative/path")
        self.assertEqual(report.checked, 2)

    def test_two_projects_naming_one_folder_check_it_once(self) -> None:
        report = self.check(str(self.project), str(self.project) + os.sep)
        self.assertEqual(report.checked, 3)

    def test_a_probe_error_skips_that_folder_and_is_reported(self) -> None:
        def probe(folder: Path) -> FolderAccess:
            if folder.name == ".git":
                raise OSError(1, "broken")
            return FolderAccess(USER, "S", True)

        self.probe = probe  # type: ignore[method-assign]
        report = self.check()
        self.assertEqual(report.checked, 2)
        self.assertIn("broken", report.error or "")

    def test_off_windows_nothing_is_read(self) -> None:
        self.probe = lambda folder: self.fail("no probe off Windows")  # type: ignore[method-assign]
        self.assertFalse(self.check(platform="linux").applicable)
        self.assertFalse(self.check(platform="darwin").applicable)


@unittest.skipUnless(sys.platform.startswith("win"), "junctions are a Windows thing")
class LinkTests(_Case):
    def test_a_junction_is_never_probed(self) -> None:
        import _winapi

        target = self.root / "elsewhere"
        target.mkdir()
        os.rmdir(self.project / ".agents")
        _winapi.CreateJunction(str(target), str(self.project / ".agents"))
        self.addCleanup(lambda: os.path.lexists(self.project / ".agents") and os.rmdir(self.project / ".agents"))
        seen: list[str] = []
        self.probe = lambda folder: seen.append(folder.name) or FolderAccess(USER, "S", True)  # type: ignore[method-assign]
        report = self.check()
        self.assertNotIn(".agents", seen)
        self.assertEqual(report.checked, 2)
        self.assertTrue(target.is_dir(), "the junction's target is untouched")


class LogTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.answers[".agents"] = FolderAccess("DESKTOP\\CodexSandboxOffline", "S-1-5-21-9", False)
        self.agents = str(self.project / ".agents")

    def test_codex_s_log_confirms_a_finding(self) -> None:
        self.log(f"deny ACE failed on {self.agents}: open deny ACL target for update: denied (os error 5)")
        (finding,) = self.check().findings
        self.assertTrue(finding.confirmed)

    def test_a_later_success_cancels_the_failure(self) -> None:
        self.log(
            f"deny ACE failed on {self.agents}: open deny ACL target for update (os error 5)",
            f"applied deny ACE to protect {self.agents}",
        )
        (finding,) = self.check().findings
        self.assertFalse(finding.confirmed)

    def test_only_the_newest_log_counts(self) -> None:
        self.log(f"deny ACE failed on {self.agents}: x (os error 5)", day="2026-10-24")
        self.log("setup refresh: processed 3 write roots; errors=[]", day="2026-10-25")
        (finding,) = self.check().findings
        self.assertFalse(finding.confirmed)

    def test_folders_outside_the_projects_are_counted_never_named(self) -> None:
        self.log(
            "deny ACE failed on D:\\Other\\.git: x (os error 5)",
            "setup refresh completed with errors: [\"deny ACE failed on D:\\\\Other\\\\.git: x\"]",
        )
        report = self.check()
        self.assertEqual(report.failing_elsewhere, 1)
        self.assertNotIn("Other", repr(report.findings))

    def test_an_empty_log_and_no_setup_error_say_nothing(self) -> None:
        self.answers.clear()
        self.log("Windows sandbox selection: elevated")
        report = self.check()
        self.assertEqual((report.findings, report.failing_elsewhere, report.setup_error), ((), 0, False))

    def test_setup_error_json_is_reported(self) -> None:
        (self.codex / ".sandbox" / "setup_error.json").write_text("{}", encoding="utf-8")
        self.assertTrue(self.check().setup_error)


class FixCommandTests(unittest.TestCase):
    def test_only_git_recurses_and_every_command_works_on_links_themselves(self) -> None:
        self.assertEqual(
            fix_command("D:\\p\\.git", ".git", USER), "icacls 'D:\\p\\.git' /setowner 'DESKTOP\\owner' /T /C /L",
        )
        for name in (".codex", ".agents"):
            command = fix_command(f"D:\\p\\{name}", name, USER)
            self.assertNotIn("/T", command)
            self.assertTrue(command.endswith("/C /L"))
            self.assertNotIn("/grant", command)

    def test_an_unknown_user_gives_no_command_rather_than_a_guessed_account(self) -> None:
        self.assertIsNone(fix_command("D:\\p\\.codex", ".codex", None))
        self.assertIsNone(fix_command("D:\\p\\.codex", ".codex", ""))

    def test_a_folder_name_powershell_would_expand_stays_a_literal(self) -> None:
        # The line is pasted into PowerShell run as administrator (CS-490):
        # inside single quotes nothing expands, and a quote is doubled.
        for name in ("$tmp", "$(Get-Date)", "a`b", "it's", "it’s", "$env:USERNAME"):
            folder = f"D:\\Projects\\{name}\\.git"
            command = fix_command(folder, ".git", USER)
            literal = command.removeprefix("icacls ").partition(" /setowner ")[0]
            self.assertEqual(_powershell_single_quoted(literal), folder)
        self.assertEqual(powershell_literal("it's"), "'it''s'")
        self.assertEqual(powershell_literal("a‘b"), "'a‘‘b'")


def _powershell_single_quoted(token: str) -> str:
    """Read one PowerShell single-quoted token the way its tokenizer does."""
    quotes = "'‘’‚‛"
    assert token[0] in quotes and token[-1] in quotes, token
    body, out, index = token[1:-1], [], 0
    while index < len(body):
        char = body[index]
        if char in quotes:
            # Inside the literal a quote only ever appears doubled.
            assert index + 1 < len(body) and body[index + 1] in quotes, token
            index += 1
        out.append(char)
        index += 1
    return "".join(out)


@unittest.skipUnless(sys.platform.startswith("win"), "the Windows security API")
class WindowsProbeTests(unittest.TestCase):
    def test_a_folder_this_user_made_is_theirs_and_protectable(self) -> None:
        from codexsync.codex_sandbox import _windows_probe

        folder = SANDBOX / f"codex-sandbox-probe-{uuid.uuid4().hex[:8]}"
        folder.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        probe, user, close = _windows_probe()
        self.addCleanup(close)
        access = probe(folder)
        self.assertIsNotNone(access)
        self.assertTrue(access.can_protect)
        self.assertIsNotNone(user)
        self.assertIsNone(probe(folder / "missing"))


class PreflightTests(unittest.TestCase):
    def run_check(self, report: SandboxReport):
        from codexsync import preflight

        facts: dict = {}
        cfg = type("Cfg", (), {"guardian": type("G", (), {"max_state_bytes": 1 << 20})()})()
        with patch("codexsync.preflight.check_sandbox_folders", return_value=report):
            result = preflight._check_codex_sandbox(Path("nowhere"), cfg, facts)
        self.assertIs(facts["sandbox"], report)
        return result

    def test_clear_is_a_pass_with_the_count(self) -> None:
        result = self.run_check(SandboxReport(applicable=True, user=USER, checked=7))
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.parts, (("codex_sandbox.clear", {"count": 7}),))

    def test_off_windows_is_a_pass_that_says_so(self) -> None:
        result = self.run_check(SandboxReport(applicable=False))
        self.assertEqual((result.status, result.parts[0][0]), ("PASS", "codex_sandbox.not_applicable"))

    def test_a_finding_is_a_warning_with_owner_consequence_and_command(self) -> None:
        from codexsync.codex_sandbox import SandboxFinding

        finding = SandboxFinding(
            "D:\\p", "D:\\p\\.agents", ".agents", "DESKTOP\\CodexSandboxOffline", OwnerKind.SANDBOX, True,
            fix_command("D:\\p\\.agents", ".agents", USER),
        )
        result = self.run_check(SandboxReport(applicable=True, user=USER, checked=3, findings=(finding,)))
        self.assertEqual(result.status, "WARN")
        self.assertEqual([code for code, _ in result.parts], [
            "codex_sandbox.owned_by_sandbox", "codex_sandbox.confirmed", "codex_sandbox.consequence",
            "codex_sandbox.fix", "codex_sandbox.shared",
        ])
        self.assertIn(finding.command, result.details)

    def test_a_finding_without_a_known_user_has_no_command(self) -> None:
        from codexsync.codex_sandbox import SandboxFinding

        finding = SandboxFinding(
            "D:\\p", "D:\\p\\.git", ".git", "BUILTIN\\Administrators", OwnerKind.ADMINISTRATORS, False, None,
        )
        result = self.run_check(SandboxReport(applicable=True, checked=3, findings=(finding,)))
        self.assertEqual(result.status, "WARN")
        self.assertNotIn("codex_sandbox.fix", [code for code, _ in result.parts])

    def test_codex_s_own_error_alone_is_a_warning(self) -> None:
        result = self.run_check(SandboxReport(applicable=True, checked=3, setup_error=True, failing_elsewhere=2))
        self.assertEqual(result.status, "WARN")
        self.assertEqual([code for code, _ in result.parts], ["codex_sandbox.elsewhere", "codex_sandbox.setup_error"])

    def test_a_probe_that_could_not_run_is_a_warning_not_a_pass(self) -> None:
        result = self.run_check(SandboxReport(applicable=True, error="no token"))
        self.assertEqual((result.status, result.parts[0][0]), ("WARN", "codex_sandbox.not_checked"))

    def test_some_folders_not_checked_is_a_warning_even_when_others_were(self) -> None:
        result = self.run_check(SandboxReport(applicable=True, user=USER, checked=5, unchecked=2, error="denied"))
        self.assertEqual(result.status, "WARN")
        self.assertEqual(result.parts, (("codex_sandbox.unchecked", {"count": 2, "error": "denied"}),))


class UncheckedFolderTests(_Case):
    def test_a_folder_whose_access_cannot_be_read_is_counted_not_dropped(self) -> None:
        def probe(folder: Path) -> FolderAccess | None:
            if folder.name == ".git":
                raise OSError(5, "denied")
            return FolderAccess(USER, "S-1-5-21-1", True)

        report = check_sandbox_folders(self.codex, (str(self.project),), probe=probe, user=USER, platform="win32")
        self.assertEqual((report.checked, report.unchecked), (2, 1))
        self.assertIn("denied", report.error)

    @unittest.skipUnless(os.name == "nt", "Windows path length limit")
    def test_a_folder_past_260_characters_is_still_checked(self) -> None:
        # With long paths off, the short form of such a path reads as absent,
        # and absent was a pass (CS-522).
        deep = self.root / ("d" * 120) / ("e" * 120) / "project"
        for name in (".codex", ".agents", ".git"):
            os.makedirs("\\\\?\\" + str(deep / name))
        self.addCleanup(shutil.rmtree, "\\\\?\\" + str(self.root), ignore_errors=True)
        self.assertGreater(len(str(deep / ".agents")), 260)
        seen: list[str] = []

        def probe(folder: Path) -> FolderAccess | None:
            seen.append(str(folder))
            return FolderAccess(USER, "S-1-5-21-1", True)

        report = check_sandbox_folders(self.codex, (str(deep),), probe=probe, user=USER, platform="win32")
        self.assertEqual((report.checked, report.unchecked), (3, 0))
        self.assertTrue(all(path.startswith("\\\\?\\") for path in seen))


if __name__ == "__main__":
    unittest.main()
