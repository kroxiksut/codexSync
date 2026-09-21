"""What this version knows about Codex's processes, in one place.

Three copies of this knowledge existed before CS-256 and they disagreed: the
defaults in `config.py` still listed what 0.1 knew (`codex.exe`, `codex`, and
the single marker `codex-windows-sandbox`), while the shipped template and the
GUI's Settings fields carried the 0.2 lists. Nothing compared them, so a config
written by 0.1 -- or one with no `[process_detection]` section at all -- kept
detecting with 0.1's knowledge after the upgrade, silently.

That is not a cosmetic split. Matching is exact: `find_processes` compares
whole names (`_matches_marker` likewise), so a name this version knows and the
config does not is simply never seen. On a machine where the Codex window is
closed while `codex-windows-sandbox-service.exe` is still alive, the older list
finds nothing and the safety gate reads `STOPPED` for a machine that is not.

So: `config.py` takes its defaults from here, the Settings screen renders these
values, `config_migrate` proposes them for an older config, and
`tests/test_config_template_parity.py` fails if the shipped template drifts
from them. Add a newly observed process here and every one of those follows.

An entry containing `/` is a *path* marker matched against the process path
rather than its name -- `process_detector` decides that, and this module only
records which markers are of that shape. It is how `ChatGPT.app/Contents/MacOS/`
is told apart from the ordinary ChatGPT app: the macOS desktop build was
renamed `ChatGPT` in July 2026 while keeping the bundle id `com.openai.codex`.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

#: Process names that *are* Codex, matched as whole names on every platform.
PROCESS_NAMES: tuple[str, ...] = ("codex.exe", "codex", "codex-app-server")

#: Per-OS processes that mean "Codex has not finished yet" even with no window.
#:
#: **A name belongs here only once it has been observed to disappear when Codex
#: is closed.** A process that is always alive is not a marker: it reports the
#: same thing in both states, so adding it does not make detection stricter, it
#: makes the safety gate say `RUNNING` forever and closes every mutation
#: permanently (CS-264).
#:
#: That is not hypothetical. `codex-windows-sandbox-service` was listed here
#: from 2026-09-20 until 2026-09-21 and had to be removed: on Windows it is the
#: service `CodexSandboxService.OpenAI.Codex`, `StartMode=Auto`, running from
#: boot whether or not Codex was ever started. Every config generated from the
#: shipped template inherited it, and on such a config `sync`, `restore` and
#: `repair-projects apply` could never run. Checked on 2026-09-21 with Codex
#: closed: of the four names then listed, only the service matched.
#:
#: It is also the wrong *kind* of thing to look for. The service hosts sandboxes;
#: a sandbox still running is evidence that Codex has work in flight, the host
#: being up is not.
BACKGROUND_PROCESS_NAMES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "windows": (
        "codex-windows-sandbox",
        "codex-windows-sandbox-setup",
        "codex-command-runner",
    ),
    "macos": (
        "ChatGPT.app/Contents/MacOS/",
        "codex-app-server",
        "codex-execve-wrapper",
        "codex-code-mode-host",
    ),
    "linux": (
        "/usr/lib/chatgpt/",
        "codex-app-server",
        "codex-linux-sandbox",
        "codex-execve-wrapper",
        "codex-code-mode-host",
    ),
})

#: The keys `[process_detection.background_process_names]` may carry.
OS_KEYS: tuple[str, ...] = ("windows", "macos", "linux")


def default_process_names() -> list[str]:
    """A fresh mutable copy, because `ProcessDetectionConfig` owns a list."""
    return list(PROCESS_NAMES)


def default_background_process_names() -> dict[str, list[str]]:
    return {os_key: list(names) for os_key, names in BACKGROUND_PROCESS_NAMES.items()}


def missing_process_names(configured: list[str]) -> list[str]:
    """Names this version knows that `configured` does not, in this order.

    Comparison is case-insensitive and ignores surrounding space, the way the
    detector normalises a name; the `.exe` suffix is *not* added here, because
    a config that lists `codex` and one that lists `codex.exe` mean different
    things on POSIX.
    """
    known = {name.strip().casefold() for name in configured if name.strip()}
    return [name for name in PROCESS_NAMES if name.casefold() not in known]


def missing_background_process_names(configured: Mapping[str, list[str]]) -> dict[str, list[str]]:
    """Per OS, the markers this version knows and `configured` does not.

    An OS key with nothing missing is left out entirely, so an empty result
    means the config is current.
    """
    result: dict[str, list[str]] = {}
    for os_key in OS_KEYS:
        known = {name.strip().casefold() for name in configured.get(os_key, []) if name.strip()}
        missing = [name for name in BACKGROUND_PROCESS_NAMES[os_key] if name.casefold() not in known]
        if missing:
            result[os_key] = missing
    return result


__all__ = [
    "BACKGROUND_PROCESS_NAMES",
    "OS_KEYS",
    "PROCESS_NAMES",
    "default_background_process_names",
    "default_process_names",
    "missing_background_process_names",
    "missing_process_names",
]
