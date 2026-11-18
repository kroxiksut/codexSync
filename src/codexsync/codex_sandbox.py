"""Project folders Codex's Windows sandbox cannot protect (CS-483).

Before each command an agent runs, Codex's Windows sandbox setup puts deny
entries on the folders inside a writable root that the agent must not touch
(`.codex`, `.agents`, `.git`). It does so as the signed-in user, unelevated,
and that needs ``READ_CONTROL | WRITE_DAC`` on each folder. A folder created by
a command *inside* the sandbox is owned by the sandbox account
(`CodexSandboxOffline`), and one created from an elevated shell by
Administrators; the user then holds only the inherited "Modify", without
``WRITE_DAC``, the setup fails with `os error 5` and Codex, after a while,
refuses every command in every chat of that project (2026-10-25 incident).

This module only reads: each folder's security descriptor, checked with
``AccessCheck`` against the token Codex's setup would use (this user,
unelevated), and Codex's own sandbox log for confirmation. It never changes a
permission -- the folders are the user's, not codexSync's or Codex's state --
it says which folder, who owns it, and the command that gives it back.

The deny entries for the sandbox account that a *successful* setup leaves on
these folders are Codex's protection working, not a finding: they deny the
sandbox, not the user, so they never show up as a missing right here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import os
from pathlib import Path
import re
import sys
from typing import Callable, Iterable

from .fs_links import extended_path, is_link

__all__ = [
    "PROTECTED_FOLDER_NAMES",
    "FolderAccess",
    "OwnerKind",
    "SandboxFinding",
    "SandboxReport",
    "check_sandbox_folders",
    "fix_command",
]

#: Folders inside a writable root that Codex's sandbox setup protects with a
#: deny entry (`sandbox.<date>.log`: "applied deny ACE to protect ...").
PROTECTED_FOLDER_NAMES = (".codex", ".agents", ".git")
#: Folders that hold links into other places shared with other agents (Claude
#: Code reads `.agents/skills`): a fix changes the owner and never recreates them.
SHARED_FOLDER_NAMES = frozenset({".agents"})
#: Folders whose fix may recurse: git writes no links into its own folder, and a
#: folder created inside the sandbox has every file inside it owned the same way.
RECURSIVE_FIX_NAMES = frozenset({".git"})

SANDBOX_DIR = ".sandbox"
SETUP_ERROR_FILE = "setup_error.json"
SETUP_MARKER_FILE = "setup_marker.json"
#: The prefix of both sandbox accounts Codex creates, used when the marker that
#: names them cannot be read.
SANDBOX_ACCOUNT_PREFIX = "CodexSandbox"
ADMINISTRATORS_SID = "S-1-5-32-544"
#: Read at most this much of the newest sandbox log, from its end: a day of
#: commands is about half a megabyte.
LOG_TAIL_BYTES = 4 * 1024 * 1024

_LOG_NAME = re.compile(r"^sandbox\.(\d{4}-\d{2}-\d{2})\.log$")
# Each line starts with "[<time>] "; the same failure repeated inside an
# `errors=[...]` summary is escaped and is not matched.
_FAILED = re.compile(r"^\[[^\]]*\] deny ACE failed on (.+?): ")
_APPLIED = re.compile(r"^\[[^\]]*\] applied deny ACE to protect (.+?)\s*$")


class OwnerKind(str, Enum):
    SANDBOX = "sandbox"
    ADMINISTRATORS = "administrators"
    USER = "user"
    OTHER = "other"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FolderAccess:
    """What one folder's security descriptor says for this user."""

    #: ``DOMAIN\\name`` of the owner; ``None`` when the descriptor could not be read.
    owner: str | None
    owner_sid: str | None
    #: Whether this user, unelevated, holds ``READ_CONTROL | WRITE_DAC``.
    can_protect: bool


@dataclass(frozen=True, slots=True)
class SandboxFinding:
    project_root: str
    folder: str
    #: The folder's name under the project root (one of PROTECTED_FOLDER_NAMES).
    name: str
    owner: str | None
    owner_kind: OwnerKind
    #: Codex's own newest sandbox log names this folder as failing.
    confirmed: bool
    #: The PowerShell line that fixes it; ``None`` when the user is unknown.
    command: str | None


@dataclass(frozen=True, slots=True)
class SandboxReport:
    #: False off Windows: there is no such setup to fail.
    applicable: bool
    user: str | None = None
    #: Folders whose descriptor was read.
    checked: int = 0
    #: Folders that are there but whose descriptor could not be read.
    unchecked: int = 0
    findings: tuple[SandboxFinding, ...] = ()
    #: Folders Codex's newest log reports failing that are not among the
    #: project folders checked; counted, never named.
    failing_elsewhere: int = 0
    #: Codex left `setup_error.json`: its last sandbox setup ended with an error.
    setup_error: bool = False
    #: A probe that could not run at all (no token, no `advapi32`).
    error: str | None = None
    owners: dict[str, int] = field(default_factory=dict)


Probe = Callable[[Path], "FolderAccess | None"]


# PowerShell ends a single-quoted string at any of these, not only at `'`.
_POWERSHELL_QUOTES = "'‘’‚‛"


def powershell_literal(text: str) -> str:
    """``text`` as a PowerShell single-quoted string: nothing inside expands.

    A folder name may hold `$`, `(` and a backtick, and the line is pasted into
    a PowerShell run as administrator, so a double-quoted path would run code
    (CS-490). Inside single quotes only a quote is special; it is doubled.
    """
    return "'" + "".join(char * 2 if char in _POWERSHELL_QUOTES else char for char in text) + "'"


def fix_command(folder: str, name: str, user: str | None) -> str | None:
    """The PowerShell `icacls` line that gives ``folder`` back to ``user`` (run as administrator).

    Only the owner changes; no permission is touched, and `/L` works on a link
    itself rather than on what it points at. Only `.git` recurses: `.agents`
    and `.codex` may hold junctions into folders other agents share. Without a
    known user there is no line: a guessed account could be someone else's.
    """
    if not user:
        return None
    recurse = " /T" if name in RECURSIVE_FIX_NAMES else ""
    return f"icacls {powershell_literal(folder)} /setowner {powershell_literal(user)}{recurse} /C /L"


def check_sandbox_folders(
    codex_dir: Path,
    project_roots: Iterable[str],
    *,
    probe: Probe | None = None,
    user: str | None = None,
    platform: str = sys.platform,
) -> SandboxReport:
    """Every project folder Codex's sandbox setup could not protect.

    ``probe`` reads one folder's access (``None``: nothing there); the default
    uses the Windows security API. Tests inject both it and ``user``.
    """
    if not platform.startswith("win"):
        return SandboxReport(applicable=False)
    close: Callable[[], None] = lambda: None
    if probe is None:
        try:
            probe, user, close = _windows_probe()
        except OSError as exc:
            return SandboxReport(applicable=True, error=str(exc))
    try:
        return _check(codex_dir, project_roots, probe, user)
    finally:
        close()


def _check(codex_dir: Path, project_roots: Iterable[str], probe: Probe, user: str | None) -> SandboxReport:
    sandbox_names = _sandbox_accounts(codex_dir)
    failing, protected = _log_reading(codex_dir)
    seen: set[str] = set()
    findings: list[SandboxFinding] = []
    owners: dict[str, int] = {}
    checked = 0
    unchecked = 0
    error: str | None = None
    for root in project_roots:
        base = Path(root)
        if not base.is_absolute():
            # A root written the other system's way names nothing here (D-033).
            continue
        # The root itself is not checked: the setup writes its deny entries
        # only on these folders, and a root owned by Administrators never
        # showed up in Codex's log as failing (2026-10-26, this machine).
        for name in PROTECTED_FOLDER_NAMES:
            folder = base / name
            key = _key(str(folder))
            if key in seen:
                continue
            seen.add(key)
            # A `.git` deep in a project passes 260 characters: asked in the
            # short form it would read as absent, and absent is a pass (CS-522).
            target = Path(extended_path(str(folder)))
            try:
                info = os.lstat(target)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as exc:
                unchecked += 1
                error = error or str(exc)
                continue
            if is_link(target, info):
                # A link's own owner is not what the setup protects, and its
                # target is outside the project.
                continue
            try:
                access = probe(target)
            except OSError as exc:
                unchecked += 1
                error = error or str(exc)
                continue
            if access is None:
                continue
            checked += 1
            kind = _owner_kind(access, user, sandbox_names)
            owners[kind.value] = owners.get(kind.value, 0) + 1
            if access.can_protect:
                continue
            findings.append(SandboxFinding(
                project_root=str(base), folder=str(folder), name=name, owner=access.owner, owner_kind=kind,
                confirmed=key in failing and key not in protected,
                command=fix_command(str(folder), name, user),
            ))
    elsewhere = sum(1 for key in failing if key not in seen and key not in protected)
    return SandboxReport(
        applicable=True, user=user, checked=checked, unchecked=unchecked, findings=tuple(findings),
        failing_elsewhere=elsewhere,
        setup_error=(codex_dir / SANDBOX_DIR / SETUP_ERROR_FILE).is_file(),
        error=error, owners=dict(sorted(owners.items())),
    )


def _key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path.removeprefix("\\\\?\\")))


def _owner_kind(access: FolderAccess, user: str | None, sandbox_names: frozenset[str]) -> OwnerKind:
    if access.owner is None:
        return OwnerKind.UNKNOWN
    if access.owner_sid == ADMINISTRATORS_SID:
        return OwnerKind.ADMINISTRATORS
    account = access.owner.rpartition("\\")[2].casefold()
    if account in sandbox_names or account.startswith(SANDBOX_ACCOUNT_PREFIX.casefold()):
        return OwnerKind.SANDBOX
    if user is not None and access.owner.casefold() == user.casefold():
        return OwnerKind.USER
    return OwnerKind.OTHER


def _sandbox_accounts(codex_dir: Path) -> frozenset[str]:
    """The sandbox account names Codex recorded; only those two keys are read."""
    try:
        marker = json.loads((codex_dir / SANDBOX_DIR / SETUP_MARKER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    if not isinstance(marker, dict):
        return frozenset()
    return frozenset(
        value.casefold() for key in ("offline_username", "online_username")
        if isinstance(value := marker.get(key), str) and value
    )


def _log_reading(codex_dir: Path) -> tuple[set[str], set[str]]:
    """Folders the newest sandbox log last saw failing, and last saw protected.

    Only the two kinds of line naming a folder are matched, and a folder's
    later success cancels an earlier failure: the log is evidence about the
    folders codexSync already checks, nothing it prints.
    """
    directory = codex_dir / SANDBOX_DIR
    try:
        logs = sorted(
            (match.group(1), entry.name)
            for entry in os.scandir(directory)
            if (match := _LOG_NAME.match(entry.name)) and entry.is_file()
        )
    except OSError:
        return set(), set()
    if not logs:
        return set(), set()
    try:
        with open(directory / logs[-1][1], "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - LOG_TAIL_BYTES))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return set(), set()
    last: dict[str, bool] = {}
    for line in text.splitlines():
        if match := _FAILED.match(line):
            last[_key(match.group(1))] = False
        elif match := _APPLIED.match(line):
            last[_key(match.group(1))] = True
    return {key for key, ok in last.items() if not ok}, {key for key, ok in last.items() if ok}


# --- Windows -----------------------------------------------------------------


def _windows_probe() -> tuple[Probe, str | None, Callable[[], None]]:  # pragma: no cover - Windows API
    """A probe over this process's user, as Codex's setup runs it: unelevated.

    An elevated process checks with its linked (filtered) token, so a folder
    owned by Administrators reads as unreachable here too, which it is for the
    setup.
    """
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    HANDLE, DWORD, BOOL, PVOID = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.c_void_p
    TOKEN_QUERY, TOKEN_DUPLICATE = 0x0008, 0x0002
    TOKEN_USER, TOKEN_ELEVATION_TYPE, TOKEN_LINKED_TOKEN = 1, 18, 19
    ELEVATION_FULL = 2
    SECURITY_IDENTIFICATION = 1
    SE_FILE_OBJECT = 1
    OWNER, GROUP, DACL = 0x1, 0x2, 0x4
    READ_CONTROL, WRITE_DAC = 0x00020000, 0x00040000
    ERROR_ACCESS_DENIED = 5
    NOT_THERE = {2, 3}

    class GENERIC_MAPPING(ctypes.Structure):
        _fields_ = [("read", DWORD), ("write", DWORD), ("execute", DWORD), ("all", DWORD)]

    kernel.GetCurrentProcess.restype = HANDLE
    kernel.CloseHandle.argtypes = [HANDLE]
    kernel.LocalFree.argtypes = [PVOID]
    kernel.LocalFree.restype = PVOID
    advapi.OpenProcessToken.argtypes = [HANDLE, DWORD, ctypes.POINTER(HANDLE)]
    advapi.OpenProcessToken.restype = BOOL
    advapi.GetTokenInformation.argtypes = [HANDLE, ctypes.c_int, PVOID, DWORD, ctypes.POINTER(DWORD)]
    advapi.GetTokenInformation.restype = BOOL
    advapi.DuplicateToken.argtypes = [HANDLE, ctypes.c_int, ctypes.POINTER(HANDLE)]
    advapi.DuplicateToken.restype = BOOL
    advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, DWORD,
        ctypes.POINTER(PVOID), ctypes.POINTER(PVOID), ctypes.POINTER(PVOID), ctypes.POINTER(PVOID),
        ctypes.POINTER(PVOID),
    ]
    advapi.GetNamedSecurityInfoW.restype = DWORD
    advapi.AccessCheck.argtypes = [
        PVOID, HANDLE, DWORD, ctypes.POINTER(GENERIC_MAPPING), PVOID, ctypes.POINTER(DWORD),
        ctypes.POINTER(DWORD), ctypes.POINTER(BOOL),
    ]
    advapi.AccessCheck.restype = BOOL
    advapi.LookupAccountSidW.argtypes = [
        wintypes.LPCWSTR, PVOID, wintypes.LPWSTR, ctypes.POINTER(DWORD), wintypes.LPWSTR,
        ctypes.POINTER(DWORD), ctypes.POINTER(DWORD),
    ]
    advapi.LookupAccountSidW.restype = BOOL
    advapi.ConvertSidToStringSidW.argtypes = [PVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = BOOL

    def fail(what: str) -> OSError:
        code = ctypes.get_last_error()
        return OSError(code, f"{what} failed: {ctypes.FormatError(code).strip()}")

    def sid_string(sid: int) -> str | None:
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            return None
        try:
            return text.value
        finally:
            kernel.LocalFree(ctypes.cast(text, PVOID))

    def account(sid: int) -> str | None:
        name, domain = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        name_len, domain_len, use = DWORD(256), DWORD(256), DWORD()
        if not advapi.LookupAccountSidW(None, sid, name, ctypes.byref(name_len), domain,
                                        ctypes.byref(domain_len), ctypes.byref(use)):
            return sid_string(sid)
        return f"{domain.value}\\{name.value}" if domain.value else name.value

    process_token = HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), TOKEN_QUERY | TOKEN_DUPLICATE,
                                   ctypes.byref(process_token)):
        raise fail("OpenProcessToken")
    try:
        user_buffer = ctypes.create_string_buffer(512)
        size = DWORD()
        if not advapi.GetTokenInformation(process_token, TOKEN_USER, user_buffer, 512, ctypes.byref(size)):
            raise fail("GetTokenInformation(TokenUser)")
        # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first field is the SID pointer.
        user = account(ctypes.cast(user_buffer, ctypes.POINTER(PVOID))[0])

        elevation = DWORD()
        check_token = HANDLE()
        if (advapi.GetTokenInformation(process_token, TOKEN_ELEVATION_TYPE, ctypes.byref(elevation),
                                       ctypes.sizeof(elevation), ctypes.byref(size))
                and elevation.value == ELEVATION_FULL):
            linked = HANDLE()
            if not advapi.GetTokenInformation(process_token, TOKEN_LINKED_TOKEN, ctypes.byref(linked),
                                              ctypes.sizeof(linked), ctypes.byref(size)):
                raise fail("GetTokenInformation(TokenLinkedToken)")
            check_token = linked
        elif not advapi.DuplicateToken(process_token, SECURITY_IDENTIFICATION, ctypes.byref(check_token)):
            raise fail("DuplicateToken")
    finally:
        kernel.CloseHandle(process_token)

    mapping = GENERIC_MAPPING(0x120089, 0x120116, 0x1200A0, 0x1F01FF)

    def probe(folder: Path) -> FolderAccess | None:
        owner_sid, group_sid, dacl, sacl, descriptor = PVOID(), PVOID(), PVOID(), PVOID(), PVOID()
        status = advapi.GetNamedSecurityInfoW(
            str(folder), SE_FILE_OBJECT, OWNER | GROUP | DACL,
            ctypes.byref(owner_sid), ctypes.byref(group_sid), ctypes.byref(dacl), ctypes.byref(sacl),
            ctypes.byref(descriptor),
        )
        if status in NOT_THERE:
            return None
        if status == ERROR_ACCESS_DENIED:
            # Not even READ_CONTROL: the setup's open fails the same way.
            return FolderAccess(owner=None, owner_sid=None, can_protect=False)
        if status != 0:
            raise OSError(status, f"GetNamedSecurityInfoW failed for {folder}: {ctypes.FormatError(status).strip()}")
        try:
            privileges = ctypes.create_string_buffer(256)
            privileges_len, granted, allowed = DWORD(256), DWORD(), BOOL()
            if not advapi.AccessCheck(descriptor, check_token, READ_CONTROL | WRITE_DAC, ctypes.byref(mapping),
                                      privileges, ctypes.byref(privileges_len), ctypes.byref(granted),
                                      ctypes.byref(allowed)):
                raise fail(f"AccessCheck for {folder}")
            return FolderAccess(
                owner=account(owner_sid.value), owner_sid=sid_string(owner_sid.value), can_protect=bool(allowed),
            )
        finally:
            kernel.LocalFree(descriptor)

    return probe, user, lambda: kernel.CloseHandle(check_token)
