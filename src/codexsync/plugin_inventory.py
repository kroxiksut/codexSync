"""Read just the Codex plugin declarations and the presence of cache markers."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re
import stat
import tomllib

from .fs_links import is_link


BUILT_IN_MARKETPLACES = frozenset({
    "openai-bundled", "openai-curated", "openai-curated-remote", "openai-primary-runtime",
})

MALFORMED_DECLARATION = "MALFORMED_PLUGIN_DECLARATION"
PAYLOAD_MISSING = "DECLARED_PLUGIN_PAYLOAD_MISSING"
CACHE_ONLY = "CACHE_ONLY_PLUGIN_UNKNOWN_PROVENANCE"
SOURCE_NOT_CONFIGURED = "CUSTOM_PLUGIN_SOURCE_NOT_CONFIGURED"
CONFIG_UNREADABLE = "PLUGIN_CONFIG_UNREADABLE"
CACHE_UNREADABLE = "PLUGIN_CACHE_UNREADABLE"

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]*")


@dataclass(frozen=True, slots=True)
class PluginDeclaration:
    plugin_id: str
    marketplace_id: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class PluginInventory:
    declarations: tuple[PluginDeclaration, ...]
    cached_declarations: int
    configured_marketplace_declarations: int
    findings: dict[str, int]


def _entry_type(path: Path) -> int | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if is_link(path, info):
        return stat.S_IFLNK
    return stat.S_IFMT(info.st_mode)


def _folders(root: Path, name_pattern: re.Pattern[str]):
    for path in root.iterdir():
        if name_pattern.fullmatch(path.name) and _entry_type(path) == stat.S_IFDIR:
            yield path


def _cached_plugins(local_dir: Path) -> set[tuple[str, str]]:
    """Match only the versioned cache layout and never open a manifest."""
    plugins = local_dir / "plugins"
    cache = plugins / "cache"
    for root in (plugins, cache):
        entry_type = _entry_type(root)
        if entry_type is None:
            return set()
        if entry_type != stat.S_IFDIR:
            raise NotADirectoryError
    found = set()
    for marketplace in _folders(cache, _ID):
        for plugin in _folders(marketplace, _ID):
            for version in _folders(plugin, _VERSION):
                marker_dir = version / ".codex-plugin"
                if _entry_type(marker_dir) == stat.S_IFDIR and _entry_type(marker_dir / "plugin.json") == stat.S_IFREG:
                    found.add((plugin.name, marketplace.name))
                    break
    return found


def _declarations(section: object, findings: Counter[str]) -> tuple[PluginDeclaration, ...]:
    if not isinstance(section, dict):
        findings[MALFORMED_DECLARATION] += 1
        return ()
    result = []
    for identifier, entry in sorted(section.items()):
        plugin, separator, marketplace = identifier.partition("@")
        if (
            not separator or not _ID.fullmatch(plugin) or not _ID.fullmatch(marketplace)
            or not isinstance(entry, dict) or entry.keys() != {"enabled"}
            or not isinstance(entry["enabled"], bool)
        ):
            findings[MALFORMED_DECLARATION] += 1
            continue
        result.append(PluginDeclaration(plugin, marketplace, entry["enabled"]))
    return tuple(result)


def read_plugin_inventory(local_dir: Path) -> PluginInventory:
    """Return strict declarations and aggregate findings, using local reads only.

    Cache markers show that a payload exists at a conventional path. They
    cannot establish a source or tell whether a cached plugin is usable.
    """
    config_path = local_dir / "config.toml"
    try:
        config = {}
        entry_type = _entry_type(config_path)
        if entry_type == stat.S_IFREG:
            with config_path.open("rb") as stream:
                config = tomllib.load(stream)
        elif entry_type is not None:
            return PluginInventory((), 0, 0, {CONFIG_UNREADABLE: 1})
    except (OSError, ValueError, RecursionError):
        return PluginInventory((), 0, 0, {CONFIG_UNREADABLE: 1})

    findings: Counter[str] = Counter()
    declarations = _declarations(config.get("plugins", {}), findings)
    marketplaces = config.get("marketplaces", {})
    configured = set(marketplaces) if isinstance(marketplaces, dict) else set()
    known = BUILT_IN_MARKETPLACES | configured
    configured_count = sum(item.marketplace_id in configured for item in declarations)
    for item in declarations:
        if item.marketplace_id not in known:
            findings[SOURCE_NOT_CONFIGURED] += 1

    try:
        cached = _cached_plugins(local_dir)
    except OSError:
        findings[CACHE_UNREADABLE] += 1
        return PluginInventory(declarations, 0, configured_count, dict(findings))
    declared = {(item.plugin_id, item.marketplace_id) for item in declarations}
    for code, count in (
        (PAYLOAD_MISSING, sum(marketplace in known for _, marketplace in declared - cached)),
        (CACHE_ONLY, len(cached - declared)),
    ):
        if count:
            findings[code] = count
    return PluginInventory(declarations, len(declared & cached), configured_count, dict(findings))
