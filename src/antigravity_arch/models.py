"""Core immutable domain models for antigravity-arch."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class PackageName(StrEnum):
    """Supported packages in the Google Antigravity 2.x suite."""

    ANTIGRAVITY = "antigravity"
    ANTIGRAVITY_CLI = "antigravity-cli"
    ANTIGRAVITY_IDE = "antigravity-ide"


ALL_PACKAGES: tuple[PackageName, ...] = (
    PackageName.ANTIGRAVITY,
    PackageName.ANTIGRAVITY_CLI,
    PackageName.ANTIGRAVITY_IDE,
)


class Arch(StrEnum):
    """Supported Arch Linux architectures."""

    X86_64 = "x86_64"
    AARCH64 = "aarch64"

    @classmethod
    def from_machine(cls, machine: str) -> Arch:
        normalized = machine.strip().lower()
        if normalized in ("x86_64", "amd64"):
            return cls.X86_64
        if normalized in ("aarch64", "arm64"):
            return cls.AARCH64
        raise ValueError(f"Unsupported architecture: {machine!r}. Supported: x86_64, aarch64")


class ChecksumKind(StrEnum):
    """Supported cryptographic and transport verification digests."""

    CRC32C_B64 = "crc32c_b64"
    SHA256 = "sha256"
    SHA512 = "sha512"
    BLAKE2B = "b2"


class InstallMode(StrEnum):
    """How built packages are published or installed."""

    LOCAL_REPO = "local-repo"
    DIRECT = "direct"


class RecipeOrigin(StrEnum):
    """Which tier of the 3-tier PKGBUILD recipe cascade was used."""

    AUR_LIVE = "aur-live"
    AUR_CACHED = "aur-cached"
    BUNDLED_FALLBACK = "bundled-fallback"


class SyncAction(StrEnum):
    """Planned action for a single package."""

    BUILD_UPSTREAM = "build-upstream"
    BUILD_AUR = "build-aur"
    UP_TO_DATE = "up-to-date"
    HOLD_NEWER_INSTALLED = "hold-newer-installed"


@dataclass(frozen=True)
class UpstreamRelease:
    """Resolved official upstream release metadata from Google's Cloud Run APIs."""

    package: PackageName
    arch: Arch
    version: str
    build_id: str
    pkgver: str
    download_url: str
    checksum_kind: ChecksumKind
    checksum_value: str
    content_length: int | None = None
    source_endpoint: str = ""
    raw_timestamp: str | None = None


@dataclass(frozen=True)
class AurInfo:
    """Metadata fetched from AUR RPC v5."""

    package: PackageName
    version: str | None
    last_modified: int | None
    out_of_date: int | None
    git_url: str
    reachable: bool = True
    error: str | None = None


@dataclass(frozen=True)
class InstalledPackage:
    """Local pacman installed state for a package."""

    package: PackageName
    version: str | None
    is_foreign: bool = False

    @property
    def is_installed(self) -> bool:
        return self.version is not None


@dataclass(frozen=True)
class PackagePlan:
    """Reconciled sync decision comparing installed, AUR, and upstream versions."""

    package: PackageName
    action: SyncAction
    reason: str
    installed: InstalledPackage
    aur: AurInfo
    upstream: UpstreamRelease
    target_pkgver: str
    target_pkgrel: str

    @property
    def target_full_version(self) -> str:
        return f"{self.target_pkgver}-{self.target_pkgrel}"

    @property
    def needs_build(self) -> bool:
        return self.action in (SyncAction.BUILD_UPSTREAM, SyncAction.BUILD_AUR)


@dataclass(frozen=True)
class RecipeWorkspace:
    """Prepared build directory containing a PKGBUILD and supporting files."""

    package: PackageName
    work_dir: Path
    origin: RecipeOrigin
    aur_version: str | None = None
    warning: str | None = None


@dataclass(frozen=True)
class BuildArtifact:
    """Output produced by building a package with makepkg."""

    package: PackageName
    full_version: str
    pkg_path: Path
    sig_path: Path | None
    recipe_origin: RecipeOrigin
    watcher_guard_applied: bool = False


class AntigravityArchError(Exception):
    """Base exception for all antigravity-arch failures."""


class ProviderError(AntigravityArchError):
    """Raised when querying an upstream release provider fails."""


class IntegrityError(AntigravityArchError):
    """Raised when cryptographic or transport verification fails."""


class PkgbuildError(AntigravityArchError):
    """Raised when parsing or patching a PKGBUILD fails."""


class BuildError(AntigravityArchError):
    """Raised when makepkg, repo-add, or pacman execution fails."""


class LockError(AntigravityArchError):
    """Raised when another antigravity-arch process holds the sync lock."""
