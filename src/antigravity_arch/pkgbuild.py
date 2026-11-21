"""Deterministic, line-anchored PKGBUILD overlay patcher."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources

from .models import Arch, ChecksumKind, PackageName, PkgbuildError, UpstreamRelease

_ASSIGN_RE_CACHE: dict[str, re.Pattern[str]] = {}
_SUM_PREFIX_PRIORITY: tuple[tuple[str, ChecksumKind], ...] = (
    ("b2", ChecksumKind.BLAKE2B),
    ("sha256", ChecksumKind.SHA256),
    ("sha512", ChecksumKind.SHA512),
)

_WATCHER_GUARD_MARKER = "# antigravity-arch: conditional @parcel/watcher guard"
_WATCHER_INSTALL_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?:#[^\n]*\n(?P=indent))?"
    r"install\s+-Dm755\s+package/watcher\.node\s*(?:\\\n[ \t]*)?"
    r"\"\$pkgdir/opt/antigravity-ide/resources/app/node_modules/@parcel/watcher/build/Release/watcher\.node\"[ \t]*$"
)


@dataclass(frozen=True)
class SourceInspection:
    """Extracted source filename and checksum array metadata from a PKGBUILD."""

    filename: str
    url_template: str
    sum_prefix: str
    sum_kind: ChecksumKind


@dataclass(frozen=True)
class PatchResult:
    """Result of patching a PKGBUILD with upstream release metadata."""

    content: str
    sum_prefix: str
    watcher_guard_applied: bool


def _scalar_re(var_name: str) -> re.Pattern[str]:
    pat = _ASSIGN_RE_CACHE.get(var_name)
    if pat is None:
        pat = re.compile(rf"(?m)^({re.escape(var_name)}=)(.*)$")
        _ASSIGN_RE_CACHE[var_name] = pat
    return pat


def _find_array_span(text: str, array_name: str) -> tuple[int, int, int, int] | None:
    """Locate `array_name=(...)` returning (start, open_paren_end, close_paren_start, end)."""
    match = re.search(rf"(?m)^{re.escape(array_name)}=\(", text)
    if not match:
        return None

    open_end = match.end()
    in_single = False
    in_double = False
    escaped = False
    idx = open_end
    length = len(text)

    while idx < length:
        ch = text[idx]
        if escaped:
            escaped = False
            idx += 1
            continue
        if ch == "\\" and not in_single:
            escaped = True
            idx += 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == ")" and not in_single and not in_double:
            return (match.start(), open_end, idx, idx + 1)
        idx += 1

    raise PkgbuildError(f"Unterminated bash array {array_name}=( in PKGBUILD")


def _first_array_token_span(body: str) -> tuple[int, int]:
    """Return (start, end) offsets of the first whitespace-delimited bash token in array body."""
    idx = 0
    length = len(body)

    while idx < length:
        while idx < length and body[idx] in " \t\r\n":
            idx += 1
        if idx < length and body[idx] == "#":
            newline = body.find("\n", idx)
            idx = length if newline == -1 else newline + 1
            continue
        break

    if idx >= length:
        raise PkgbuildError("Empty bash array in PKGBUILD")

    start = idx
    in_single = False
    in_double = False
    escaped = False
    while idx < length:
        ch = body[idx]
        if escaped:
            escaped = False
            idx += 1
            continue
        if ch == "\\" and not in_single:
            escaped = True
            idx += 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch in " \t\r\n" and not in_single and not in_double:
            break
        idx += 1

    return start, idx


def _strip_quotes(token: str) -> tuple[str, str]:
    """Strip matching outer quotes and return (inner_value, quote_char)."""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1], token[0]
    return token, ""


def _expand_pkgbuild_vars(
    template: str,
    *,
    pkgname: str,
    pkgver: str,
    build_id: str,
) -> str:
    """Expand standard PKGBUILD variables used inside source filenames."""
    out = template
    replacements = (
        ("${pkgver//_/-}", pkgver.replace("_", "-")),
        ("${pkgname}", pkgname),
        ("$pkgname", pkgname),
        ("${pkgver}", pkgver),
        ("$pkgver", pkgver),
        ("${_build}", build_id),
        ("$_build", build_id),
    )
    for needle, value in replacements:
        out = out.replace(needle, value)
    return out


def inspect_pkgbuild(
    text: str,
    *,
    package: PackageName,
    arch: Arch,
    target_pkgver: str,
    target_build_id: str,
) -> SourceInspection:
    """Extract expected SRCDEST filename and declared checksum algorithm for arch."""
    source_array = f"source_{arch.value}"
    span = _find_array_span(text, source_array)
    if span is None:
        raise PkgbuildError(f"PKGBUILD is missing {source_array}=(...)")

    body = text[span[1] : span[2]]
    tok_start, tok_end = _first_array_token_span(body)
    raw_token = body[tok_start:tok_end]
    unquoted, _ = _strip_quotes(raw_token)

    if "::" in unquoted:
        raw_filename, url_template = unquoted.split("::", 1)
        filename = _expand_pkgbuild_vars(
            raw_filename,
            pkgname=package.value,
            pkgver=target_pkgver,
            build_id=target_build_id,
        )
    else:
        url_template = unquoted
        expanded_url = _expand_pkgbuild_vars(
            unquoted,
            pkgname=package.value,
            pkgver=target_pkgver,
            build_id=target_build_id,
        )
        filename = expanded_url.rsplit("/", 1)[-1]

    for prefix, kind in _SUM_PREFIX_PRIORITY:
        if _find_array_span(text, f"{prefix}sums_{arch.value}") is not None:
            return SourceInspection(
                filename=filename,
                url_template=url_template,
                sum_prefix=prefix,
                sum_kind=kind,
            )

    raise PkgbuildError(
        f"PKGBUILD has no supported checksum array (b2sums/sha256sums/sha512sums) for {arch.value}"
    )


def _replace_first_array_element(text: str, array_name: str, new_token: str) -> str:
    span = _find_array_span(text, array_name)
    if span is None:
        raise PkgbuildError(f"PKGBUILD is missing {array_name}=(...)")
    body = text[span[1] : span[2]]
    tok_start, tok_end = _first_array_token_span(body)
    patched_body = body[:tok_start] + new_token + body[tok_end:]
    return text[: span[1]] + patched_body + text[span[2] :]


def _apply_watcher_guard(text: str) -> tuple[str, bool]:
    """Wrap unconditional antigravity-ide watcher.node replacement in a vercmp guard."""
    if _WATCHER_GUARD_MARKER in text:
        return text, True

    match = _WATCHER_INSTALL_RE.search(text)
    if not match:
        return text, False

    indent = match.group("indent")
    template = resources.files("antigravity_arch.assets").joinpath("watcher-guard.sh.tmpl").read_text(
        encoding="utf-8"
    )
    indented_block = "\n".join(
        f"{indent}{line}" if line.strip() else "" for line in template.rstrip("\n").splitlines()
    )
    patched = text[: match.start()] + indented_block + text[match.end() :]
    return patched, True


def patch_pkgbuild_for_upstream(
    text: str,
    *,
    release: UpstreamRelease,
    target_pkgrel: str,
    target_digest: str,
    sum_prefix: str,
) -> PatchResult:
    """Patch pkgver, pkgrel, _build, source_<arch>[0], checksum[0], and IDE watcher guard."""
    patched = text

    if not _scalar_re("pkgver").search(patched):
        raise PkgbuildError("PKGBUILD is missing pkgver=")
    patched = _scalar_re("pkgver").sub(rf"\g<1>{release.pkgver}", patched, count=1)

    if not _scalar_re("pkgrel").search(patched):
        raise PkgbuildError("PKGBUILD is missing pkgrel=")
    patched = _scalar_re("pkgrel").sub(rf"\g<1>{target_pkgrel}", patched, count=1)

    if _scalar_re("_build").search(patched):
        patched = _scalar_re("_build").sub(rf"\g<1>{release.build_id}", patched, count=1)

    # Update URL portion of source_<arch>[0] while preserving its filename:: prefix
    source_array = f"source_{release.arch.value}"
    span = _find_array_span(patched, source_array)
    if span is None:
        raise PkgbuildError(f"PKGBUILD is missing {source_array}=(...)")
    body = patched[span[1] : span[2]]
    tok_start, tok_end = _first_array_token_span(body)
    raw_token = body[tok_start:tok_end]
    unquoted, quote = _strip_quotes(raw_token)
    if "::" in unquoted:
        filename_part, _ = unquoted.split("::", 1)
        new_inner = f"{filename_part}::{release.download_url}"
    else:
        new_inner = release.download_url
    wrapped_source = f"{quote}{new_inner}{quote}" if quote else f'"{new_inner}"'
    patched = _replace_first_array_element(patched, source_array, wrapped_source)

    # Update checksum element [0], preserving secondary entries (e.g. @parcel/watcher)
    sums_array = f"{sum_prefix}sums_{release.arch.value}"
    patched = _replace_first_array_element(patched, sums_array, f"'{target_digest}'")

    watcher_guard_applied = False
    if release.package is PackageName.ANTIGRAVITY_IDE:
        patched, watcher_guard_applied = _apply_watcher_guard(patched)

    return PatchResult(
        content=patched,
        sum_prefix=sum_prefix,
        watcher_guard_applied=watcher_guard_applied,
    )


def patch_pkgbuild_watcher_only(text: str, package: PackageName) -> tuple[str, bool]:
    """Apply only the conditional watcher guard when building an AUR PKGBUILD as-is."""
    if package is PackageName.ANTIGRAVITY_IDE:
        return _apply_watcher_guard(text)
    return text, False
