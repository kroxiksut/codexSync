"""Read the version resource back out of built exes and compare it with the generator (CS-273).

    python scripts/check_exe_version.py [dist/codexsync.exe dist/codexsync-gui.exe]

Windows only, and not part of the suite: it needs a real build. It reads the
resource the way Explorer does (`GetFileVersionInfoW` / `VerQueryValueW`,
through ctypes, so nothing is installed for it) and exits 1 on any difference
from what `exe_version_info.version_fields` says the build should carry.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from exe_version_info import KINDS, version_fields  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def read_resource(path: Path) -> tuple[tuple[int, ...], int, list[tuple[int, int]], dict[str, dict[str, str]]]:
    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, wintypes.LPDWORD]
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version.VerQueryValueW.argtypes = [
        ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT),
    ]
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        raise SystemExit(f"{path}: no version resource (error {ctypes.get_last_error()})")
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise SystemExit(f"{path}: GetFileVersionInfoW failed")

    def query(block: str) -> tuple[int, int]:
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version.VerQueryValueW(buffer, block, ctypes.byref(pointer), ctypes.byref(length)):
            return 0, 0
        return pointer.value or 0, length.value

    address, length = query("\\")
    fixed = ctypes.string_at(address, length)
    ms, ls = struct.unpack_from("<II", fixed, 8)
    numeric = (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    (flags,) = struct.unpack_from("<I", fixed, 28)

    address, length = query("\\VarFileInfo\\Translation")
    raw = ctypes.string_at(address, length)
    translations = [struct.unpack_from("<HH", raw, offset) for offset in range(0, length, 4)]

    tables: dict[str, dict[str, str]] = {}
    for language_id, codepage in translations:
        table: dict[str, str] = {}
        for name in ("FileDescription", "FileVersion", "InternalName", "LegalCopyright",
                     "LegalTrademarks", "OriginalFilename", "ProductName", "ProductVersion",
                     "CompanyName"):
            address, length = query(f"\\StringFileInfo\\{language_id:04X}{codepage:04X}\\{name}")
            if address:
                table[name] = ctypes.wstring_at(address, max(length - 1, 0))
        tables[f"{language_id:04X}"] = table
    return numeric, flags, translations, tables


def check(path: Path, kind: str) -> list[str]:
    expected = version_fields(kind)
    numeric, flags, translations, tables = read_resource(path)
    problems: list[str] = []
    if numeric != tuple(expected["numeric"]):
        problems.append(f"fixed version {numeric} != {expected['numeric']}")
    if flags != expected["flags"]:
        problems.append(f"file flags {flags:#x} != {expected['flags']:#x}")
    if translations != [tuple(item) for item in expected["translations"]]:
        problems.append(f"translations {translations} != {expected['translations']}")
    from exe_version_info import LANGUAGE_IDS

    for language, table in expected["tables"].items():
        found = tables.get(f"{LANGUAGE_IDS[language]:04X}", {})
        if found != table:
            problems.append(f"{language}: {found} != {table}")
    return problems


def main(argv: list[str]) -> int:
    if sys.platform != "win32":
        print("check_exe_version.py reads a Windows resource; run it on Windows")
        return 2
    paths = [Path(item) for item in argv] or [REPO_ROOT / "dist" / spec["file"] for spec in KINDS.values()]
    by_file = {spec["file"].lower(): kind for kind, spec in KINDS.items()}
    failed = False
    for path in paths:
        kind = by_file.get(path.name.lower())
        if kind is None:
            print(f"{path}: not one of {sorted(by_file)}")
            failed = True
            continue
        problems = check(path, kind)
        print(f"{path}: {'OK' if not problems else 'MISMATCH'}")
        for problem in problems:
            print(f"  {problem}")
        failed = failed or bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
