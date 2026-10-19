from __future__ import annotations

from pathlib import Path

from .config import codex_state_homes, require_outside_state_dir
from .exceptions import ConfigError
from .models import AppConfig


def locate_local_state_dir(cfg: AppConfig) -> Path:
    """The Codex state folder this config works on, proven clear of its own folders.

    Use this, not `detect_local_state_dir`, wherever a config is at hand: the
    fallback to CODEX_HOME or ~/.codex can land on a folder config validation
    never compared with the backup, temp, mirror, Guardian or copy folders.
    """
    state_dir = detect_local_state_dir(cfg.paths.local_state_dir)
    require_outside_state_dir(cfg, state_dir)
    return state_dir


def locate_state_dirs(cfg: AppConfig) -> tuple[Path, Path]:
    """`resolve_state_dirs` with the same proof as `locate_local_state_dir`."""
    # The overlap first: a mirror inside `.codex` is the reason, not its absence.
    local = locate_local_state_dir(cfg)
    _, cloud = resolve_state_dirs(local, cfg.paths.cloud_root_dir)
    return local, cloud


def resolve_state_dirs(local_state_dir: Path | None, cloud_root_dir: Path) -> tuple[Path, Path]:
    """
    Validates and returns the pair of state directories participating in sync.
    """
    local = detect_local_state_dir(local_state_dir)
    cloud = cloud_root_dir.expanduser()

    if not cloud.exists():
        raise ConfigError(f"Cloud state dir does not exist: {cloud}", code="CLOUD_DIR_MISSING", params={"path": str(cloud)})
    if not cloud.is_dir():
        raise ConfigError(f"Cloud state dir is not a directory: {cloud}", code="NOT_A_FOLDER", params={"path": str(cloud)})

    return local, cloud


def detect_local_state_dir(configured_path: Path | None) -> Path:
    candidates: list[Path] = []
    if configured_path:
        candidates.append(configured_path.expanduser())

    candidates.extend(codex_state_homes())

    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate

    tried = ", ".join(str(path) for path in candidates)
    raise ConfigError(
        f"Cannot detect Codex state directory. Tried: {tried}", code="CODEX_DIR_NOT_FOUND", params={"tried": tried},
    )
