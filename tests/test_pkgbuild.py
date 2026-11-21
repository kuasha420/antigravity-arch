"""Hermetic tests for PKGBUILD overlay patching and conditional @parcel/watcher guard."""

from __future__ import annotations

from importlib import resources

from antigravity_arch.models import Arch, ChecksumKind, PackageName, UpstreamRelease
from antigravity_arch.pkgbuild import inspect_pkgbuild, patch_pkgbuild_for_upstream

AUR_HUB_SAMPLE = """\
pkgname=antigravity
pkgver=2.33.0
pkgrel=1
_build=4731201358004224
source=(antigravity.desktop antigravity.png)
source_x86_64=(Antigravity-$pkgver-x86_64.tar.gz::https://storage.googleapis.com/antigravity-public/antigravity-hub/$pkgver-$_build/linux-x64/Antigravity.tar.gz)
source_aarch64=(Antigravity-$pkgver-aarch64.tar.gz::https://storage.googleapis.com/antigravity-public/antigravity-hub/$pkgver-$_build/linux-arm/Antigravity.tar.gz)
b2sums=('aaa' 'bbb')
b2sums_aarch64=('old_arm')
b2sums_x86_64=('old_x64')
"""

AUR_CLI_SAMPLE = """\
pkgname=antigravity-cli
pkgver=1.5.13_5999302598852608
pkgrel=1
source=("LICENSE")
sha256sums=('license_sum')
sha256sums_x86_64=('old_cli_x64')
sha256sums_aarch64=('old_cli_arm')
source_x86_64=("${pkgname}-${pkgver}-x86_64.tar.gz::https://storage.googleapis.com/antigravity-public/antigravity-cli/${pkgver//_/-}/linux-x64/cli_linux_x64.tar.gz")
source_aarch64=("${pkgname}-${pkgver}-aarch64.tar.gz::https://storage.googleapis.com/antigravity-public/antigravity-cli/${pkgver//_/-}/linux-arm/cli_linux_arm64.tar.gz")
"""

AUR_IDE_SAMPLE = """\
pkgname=antigravity-ide
pkgver=2.7.0
pkgrel=1
_watcher_ver=2.5.6
_build=4507555968450560
source_x86_64=($pkgname-$pkgver-x86_64.tar.gz::https://dl.google.com/release2/j0qc3/antigravity/stable/$pkgver-$_build/linux-x64/Antigravity%20IDE.tar.gz
               watcher-x64-$_watcher_ver.tgz::https://registry.npmjs.org/@parcel/watcher-linux-x64-glibc/-/watcher-linux-x64-glibc-$_watcher_ver.tgz)
b2sums_x86_64=('old_ide_b2sum'
               'watcher_keep_me_intact')

package() {
    install -d "$pkgdir/opt"
    cp -a "Antigravity IDE" "$pkgdir/opt/antigravity-ide"

    # Replace faulty upstream @parcel/watcher binary
    install -Dm755 package/watcher.node \\
        "$pkgdir/opt/antigravity-ide/resources/app/node_modules/@parcel/watcher/build/Release/watcher.node"

    install -Dm755 antigravity-ide.sh "$pkgdir/usr/bin/$pkgname"
}
"""


def test_patch_hub_pkgbuild() -> None:
    rel = UpstreamRelease(
        package=PackageName.ANTIGRAVITY,
        arch=Arch.X86_64,
        version="2.34.0",
        build_id="5887025308631040",
        pkgver="2.34.0",
        download_url="https://storage.googleapis.com/antigravity-public/antigravity-hub/2.34.0-5887025308631040/linux-x64/Antigravity.tar.gz",
        checksum_kind=ChecksumKind.CRC32C_B64,
        checksum_value="BqScAQ==",
    )
    insp = inspect_pkgbuild(
        AUR_HUB_SAMPLE,
        package=rel.package,
        arch=rel.arch,
        target_pkgver=rel.pkgver,
        target_build_id=rel.build_id,
    )
    assert insp.filename == "Antigravity-2.34.0-x86_64.tar.gz"
    assert insp.sum_prefix == "b2"
    assert insp.sum_kind is ChecksumKind.BLAKE2B

    res = patch_pkgbuild_for_upstream(
        AUR_HUB_SAMPLE,
        release=rel,
        target_pkgrel="1",
        target_digest="new_b2_digest",
        sum_prefix=insp.sum_prefix,
    )
    assert "pkgver=2.34.0" in res.content
    assert "_build=5887025308631040" in res.content
    assert "b2sums_x86_64=('new_b2_digest')" in res.content
    assert "b2sums_aarch64=('old_arm')" in res.content
    assert not res.watcher_guard_applied


def test_patch_cli_pkgbuild() -> None:
    rel = UpstreamRelease(
        package=PackageName.ANTIGRAVITY_CLI,
        arch=Arch.X86_64,
        version="1.5.14",
        build_id="5302690847260672",
        pkgver="1.5.14_5302690847260672",
        download_url="https://storage.googleapis.com/antigravity-public/antigravity-cli/1.5.14-5302690847260672/linux-x64/cli_linux_x64.tar.gz",
        checksum_kind=ChecksumKind.SHA512,
        checksum_value="c" * 128,
    )
    insp = inspect_pkgbuild(
        AUR_CLI_SAMPLE,
        package=rel.package,
        arch=rel.arch,
        target_pkgver=rel.pkgver,
        target_build_id=rel.build_id,
    )
    assert insp.filename == "antigravity-cli-1.5.14_5302690847260672-x86_64.tar.gz"
    assert insp.sum_prefix == "sha256"

    res = patch_pkgbuild_for_upstream(
        AUR_CLI_SAMPLE,
        release=rel,
        target_pkgrel="1",
        target_digest="new_sha256_digest",
        sum_prefix=insp.sum_prefix,
    )
    assert "pkgver=1.5.14_5302690847260672" in res.content
    assert "sha256sums_x86_64=('new_sha256_digest')" in res.content
    assert "sha256sums=('license_sum')" in res.content


def test_patch_ide_pkgbuild_preserves_watcher_source_and_guards_package() -> None:
    rel = UpstreamRelease(
        package=PackageName.ANTIGRAVITY_IDE,
        arch=Arch.X86_64,
        version="2.8.0",
        build_id="6733682917113856",
        pkgver="2.8.0",
        download_url="https://edgedl.me.gvt1.com/edgedl/release2/j0qc3/antigravity/stable/2.8.0-6733682917113856/linux-x64/Antigravity%20IDE.tar.gz",
        checksum_kind=ChecksumKind.SHA256,
        checksum_value="d" * 64,
    )
    insp = inspect_pkgbuild(
        AUR_IDE_SAMPLE,
        package=rel.package,
        arch=rel.arch,
        target_pkgver=rel.pkgver,
        target_build_id=rel.build_id,
    )
    assert insp.filename == "antigravity-ide-2.8.0-x86_64.tar.gz"
    assert insp.sum_prefix == "b2"

    res = patch_pkgbuild_for_upstream(
        AUR_IDE_SAMPLE,
        release=rel,
        target_pkgrel="1",
        target_digest="new_ide_b2_digest",
        sum_prefix=insp.sum_prefix,
    )
    assert "pkgver=2.8.0" in res.content
    assert "_build=6733682917113856" in res.content
    assert "'new_ide_b2_digest'" in res.content
    assert "'watcher_keep_me_intact'" in res.content
    assert "watcher-x64-$_watcher_ver.tgz::https://registry.npmjs.org/" in res.content
    assert res.watcher_guard_applied is True
    assert "antigravity-arch: conditional @parcel/watcher guard" in res.content

    # Idempotency check: re-patching already-guarded PKGBUILD does not double-wrap
    res2 = patch_pkgbuild_for_upstream(
        res.content,
        release=rel,
        target_pkgrel="1",
        target_digest="new_ide_b2_digest",
        sum_prefix=insp.sum_prefix,
    )
    assert res2.content.count("antigravity-arch: conditional @parcel/watcher guard") == 1


def test_bundled_fallback_pkgbuilds_are_inspectable() -> None:
    for pkg in PackageName:
        pb = resources.files("antigravity_arch.assets").joinpath(
            "fallback", pkg.value, "PKGBUILD"
        ).read_text(encoding="utf-8")
        for arch in (Arch.X86_64, Arch.AARCH64):
            insp = inspect_pkgbuild(
                pb,
                package=pkg,
                arch=arch,
                target_pkgver="2.34.0" if pkg is not PackageName.ANTIGRAVITY_CLI else "1.5.14_123",
                target_build_id="123",
            )
            assert insp.filename.endswith(".tar.gz")
            assert insp.sum_prefix in ("b2", "sha256")
