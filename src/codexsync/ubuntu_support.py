"""Ubuntu runtime support boundary for codexSync.

The Linux implementation is intentionally narrower than "all Linux". Runtime
mutations are supported only on Ubuntu releases that this project explicitly
maintains and tests. Other Linux distributions fail closed instead of
inheriting Ubuntu assumptions about the desktop package, process layout, or
user services.
"""
from __future__ import annotations

from dataclasses import dataclass
import platform
from typing import Mapping


SUPPORTED_UBUNTU_RELEASES: tuple[str, ...] = ("24.04", "26.04")


@dataclass(frozen=True, slots=True)
class UbuntuRuntime:
    version: str | None
    supported: bool
    detail: str

    @property
    def detector_key(self) -> str | None:
        if not self.supported or self.version is None:
            return None
        return f"ubuntu:{self.version}"


def inspect_ubuntu_runtime(os_release: Mapping[str, str] | None = None) -> UbuntuRuntime:
    """Return the Ubuntu support status without guessing missing OS metadata."""
    if os_release is None:
        try:
            os_release = platform.freedesktop_os_release()
        except OSError as exc:
            return UbuntuRuntime(
                version=None,
                supported=False,
                detail=f"Cannot read Linux distribution metadata: {exc}",
            )

    distribution = str(os_release.get("ID", "")).strip().casefold()
    version = str(os_release.get("VERSION_ID", "")).strip() or None

    if distribution != "ubuntu":
        shown = distribution or "unknown"
        return UbuntuRuntime(
            version=version,
            supported=False,
            detail=(
                f"Linux distribution {shown!r} is outside the supported runtime scope; "
                "codexSync currently supports Ubuntu only"
            ),
        )

    if version not in SUPPORTED_UBUNTU_RELEASES:
        shown = version or "unknown"
        return UbuntuRuntime(
            version=version,
            supported=False,
            detail=(
                f"Ubuntu {shown} is outside the supported runtime scope; supported releases are "
                + ", ".join(SUPPORTED_UBUNTU_RELEASES)
            ),
        )

    return UbuntuRuntime(
        version=version,
        supported=True,
        detail=f"Ubuntu {version}",
    )


def current_ubuntu_runtime() -> UbuntuRuntime:
    """Inspect the current host's Ubuntu release."""
    return inspect_ubuntu_runtime()


__all__ = [
    "SUPPORTED_UBUNTU_RELEASES",
    "UbuntuRuntime",
    "current_ubuntu_runtime",
    "inspect_ubuntu_runtime",
]
