"""Operating-system notifications for work nobody is watching (CS-328).

The handoff watcher runs in the background from sign-in, so a refusal it
logs is a refusal nobody reads. A notification is the one channel that
reaches the person, and it is only a courtesy: it never decides anything,
and failing to show one never fails the work it reports.

Texts come from the window's language files (``notify.*`` keys), read as
data: core may not import the GUI package, but the sentences belong beside
every other sentence the user sees, where the parity tests check them. When
the files are not there (a build without them) the key's English fallback in
`_FALLBACK` is used.

Every platform command is run through an injected ``run``, like the
scheduler adapters, so no test ever shows a real notification.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Mapping

LOG = logging.getLogger(__name__)

LOCALE_DIR = Path(__file__).resolve().parent / "gui" / "locale"
DEFAULT_LANGUAGE = "en"

#: Used only when the language files are missing; `en.json` is the source.
_FALLBACK: dict[str, str] = {
    "notify.title": "CodexSync",
    "notify.handed_off": "Your work is in the cloud copy. You can continue on another machine once it has synced.",
    "notify.loaded": "Work from {machine} is loaded. You can start Codex.",
    "notify.loaded_partial": "Work from {machine} is loaded except {count} chat(s), which stay in the cloud copy: codexSync cannot place chats in Codex yet.",
    "notify.loaded_held": "Work from {machine} is loaded except {count} chat(s) your working set keeps in the cloud copy.",
    "notify.not_delivered": "The handoff from {machine} has not fully arrived ({arrived} of {total} files). Nothing was loaded; do not start Codex here yet.",
    "notify.conflict": "The handoff stopped on a conflict; nothing was written. Open CodexSync to decide.",
    "notify.failed": "The handoff did not finish: {reason}. Open CodexSync for details.",
    "notify.codex_open": "Codex was started before the handoff finished; nothing was loaded here.",
    "notify.working_elsewhere": "{machine} has been working since {since} and has not handed off. Close Codex there and wait for it to sync.",
    "notify.not_taken": "Work from {machine} ({since}) has not been loaded here. Close Codex and let CodexSync load it first.",
}

Runner = Callable[..., Any]

_POWERSHELL_TOAST = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$title = [System.Security.SecurityElement]::Escape($env:CODEXSYNC_NOTIFY_TITLE)
$body = [System.Security.SecurityElement]::Escape($env:CODEXSYNC_NOTIFY_BODY)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$title</text><text>$body</text></binding></visual></toast>")
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""

_APPLESCRIPT = (
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "end run",
)


def system_language(environ: Mapping[str, str] | None = None) -> str:
    """The user's interface language as a two-letter code, if it can be told."""
    env = os.environ if environ is None else environ
    for name in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = env.get(name, "")
        if value and value not in ("C", "POSIX"):
            return value[:2].lower()
    if sys.platform.startswith("win"):
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(85)
            if ctypes.windll.kernel32.GetUserDefaultLocaleName(buffer, len(buffer)):  # type: ignore[attr-defined]
                return buffer.value[:2].lower()
        except (AttributeError, OSError):
            pass
    return DEFAULT_LANGUAGE


def _catalog(language: str, locale_dir: Path) -> dict[str, Any]:
    try:
        return json.loads((locale_dir / f"{language}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def message(key: str, *, language: str | None = None, locale_dir: Path = LOCALE_DIR, **values: object) -> str:
    """The sentence for ``key`` in ``language``, falling back to English."""
    chosen = language or system_language()
    template: object = None
    for candidate in (chosen, DEFAULT_LANGUAGE):
        template = _catalog(candidate, locale_dir).get(key)
        if isinstance(template, str):
            break
    if not isinstance(template, str):
        template = _FALLBACK.get(key, key)
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return template


def _platform_key(platform: str | None) -> str:
    raw = (sys.platform if platform is None else platform).lower()
    if raw.startswith("win") or raw == "cygwin":
        return "windows"
    if raw in ("darwin", "macos"):
        return "macos"
    return "linux"


def notify(
    title: str,
    body: str,
    *,
    run: Runner = subprocess.run,
    platform: str | None = None,
) -> bool:
    """Show one notification; ``True`` if the platform command succeeded."""
    key = _platform_key(platform)
    kwargs: dict[str, Any] = {"capture_output": True, "text": True, "timeout": 30, "check": False}
    if key == "windows":
        env = dict(os.environ)
        env["CODEXSYNC_NOTIFY_TITLE"] = title
        env["CODEXSYNC_NOTIFY_BODY"] = body
        kwargs["env"] = env
        # The windowed build has no console; without this flag every
        # notification would flash one (CS-259).
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if flags:
            kwargs["creationflags"] = flags
        argv = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden", "-Command", _POWERSHELL_TOAST,
        ]
    elif key == "macos":
        argv = ["osascript", *(part for line in _APPLESCRIPT for part in ("-e", line)), title, body]
    else:
        argv = ["notify-send", "--app-name=CodexSync", title, body]
    try:
        result = run(argv, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.warning("notification not shown: %s", exc)
        return False
    code = getattr(result, "returncode", 1)
    if code != 0:
        LOG.warning("notification not shown: %s exited with %s", argv[0], code)
        return False
    return True


class Notifier:
    """What the watcher calls: a key and its values, or nothing when switched off."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        language: str | None = None,
        run: Runner = subprocess.run,
        platform: str | None = None,
        locale_dir: Path = LOCALE_DIR,
    ) -> None:
        self.enabled = enabled
        self.language = language
        self._run = run
        self._platform = platform
        self._locale_dir = locale_dir

    def __call__(self, key: str, **values: object) -> bool:
        text = message(key, language=self.language, locale_dir=self._locale_dir, **values)
        LOG.info("notify: %s", key)
        if not self.enabled:
            return False
        title = message("notify.title", language=self.language, locale_dir=self._locale_dir)
        return notify(title, text, run=self._run, platform=self._platform)


#: Every key a notification may use; the language files must hold each.
NOTIFY_KEYS: tuple[str, ...] = tuple(_FALLBACK)

__all__ = ["NOTIFY_KEYS", "Notifier", "message", "notify", "system_language"]
