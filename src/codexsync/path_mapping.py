"""Host-independent path mapping between named personal machines."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re


class PathMappingError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PathMappingRule:
    rule_id: str
    source_machine: str
    target_machine: str
    source_prefix: str
    target_prefix: str
    case_sensitive: bool | None = None


@dataclass(frozen=True, slots=True)
class PathMappingResult:
    rule_id: str
    target_path: str
    source_hash: str
    target_hash: str


def apply_path_mapping(
    source_path: str,
    *,
    source_machine: str,
    target_machine: str,
    rules: list[PathMappingRule],
) -> PathMappingResult:
    if not source_machine or not target_machine or "unknown-machine" in {source_machine, target_machine}:
        raise PathMappingError("Machine ids are required for path mapping")
    source = _parse(source_path)
    matches: list[tuple[int, PathMappingRule, tuple[str, ...], str]] = []
    for rule in rules:
        if rule.source_machine != source_machine or rule.target_machine != target_machine:
            continue
        prefix = _parse(rule.source_prefix)
        if prefix[0] != source[0]:
            continue
        sensitive = rule.case_sensitive if rule.case_sensitive is not None else prefix[0] == "posix"
        if _prefix(source[1], prefix[1], sensitive):
            matches.append((len(prefix[1]), rule, source[1][len(prefix[1]):], source[0]))
    if not matches:
        raise PathMappingError("NO_MAPPING")
    specificity = max(item[0] for item in matches)
    candidates = [item for item in matches if item[0] == specificity]
    rendered: list[tuple[PathMappingRule, str]] = []
    for _, rule, suffix, _ in candidates:
        target = _parse(rule.target_prefix)
        combined = _render(target[0], target[1] + suffix)
        rendered.append((rule, combined))
    unique_targets = {value.casefold() if _parse(value)[0] != "posix" else value for _, value in rendered}
    if len(unique_targets) != 1:
        raise PathMappingError("AMBIGUOUS_MAPPING")
    rule, target = sorted(rendered, key=lambda item: item[0].rule_id)[0]
    normalized_source = _render(source[0], source[1])
    return PathMappingResult(
        rule.rule_id,
        target,
        hashlib.sha256(normalized_source.encode("utf-8")).hexdigest(),
        hashlib.sha256(target.encode("utf-8")).hexdigest(),
    )


def mapping_digest(rules: list[PathMappingRule]) -> str:
    digest = hashlib.sha256()
    for rule in sorted(rules, key=lambda item: item.rule_id):
        digest.update(repr(rule).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _parse(value: str) -> tuple[str, tuple[str, ...]]:
    raw = value.strip()
    if not raw:
        raise PathMappingError("Path prefix must not be empty")
    if raw.startswith(("\\\\", "//")):
        flavor = "unc"
        stripped = raw.lstrip("/\\")
        parts = tuple(part for part in re.split(r"[/\\]+", stripped) if part)
        if len(parts) < 2:
            raise PathMappingError("UNC path requires server and share")
    elif re.match(r"^[A-Za-z]:[/\\]", raw):
        flavor = "windows"
        drive = raw[:2].upper()
        parts = (drive, *(part for part in re.split(r"[/\\]+", raw[3:]) if part))
    elif raw.startswith("/"):
        flavor = "posix"
        parts = tuple(part for part in raw.split("/") if part)
    else:
        raise PathMappingError("Only absolute Windows, UNC, or POSIX paths are supported")
    if any(part in {".", ".."} for part in parts):
        raise PathMappingError("Path traversal is not allowed in mappings")
    return flavor, parts


def _prefix(path: tuple[str, ...], prefix: tuple[str, ...], sensitive: bool) -> bool:
    if len(prefix) > len(path):
        return False
    left = path[:len(prefix)]
    if sensitive:
        return left == prefix
    return tuple(item.casefold() for item in left) == tuple(item.casefold() for item in prefix)


def _render(flavor: str, parts: tuple[str, ...]) -> str:
    if flavor == "posix":
        return "/" + "/".join(parts)
    if flavor == "unc":
        return "\\\\" + "\\".join(parts)
    return parts[0] + "\\" + "\\".join(parts[1:]) if len(parts) > 1 else parts[0] + "\\"
