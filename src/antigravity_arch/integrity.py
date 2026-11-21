"""Cryptographic and GCS transport integrity verification utilities."""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
from pathlib import Path

from .models import ChecksumKind, IntegrityError

_CASTAGNOLI_POLY = 0x82F63B78
_CHUNK_SIZE = 1024 * 1024  # 1 MiB streaming buffer


def _build_slicing8_tables() -> tuple[list[int], ...]:
    t0 = [0] * 256
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ _CASTAGNOLI_POLY if (crc & 1) else (crc >> 1)
        t0[i] = crc
    tables = [t0]
    for _ in range(1, 8):
        prev = tables[-1]
        tables.append([t0[prev[i] & 0xFF] ^ (prev[i] >> 8) for i in range(256)])
    return tuple(tables)


_SLICING8 = _build_slicing8_tables()


def crc32c_pure_python(data: bytes | bytearray | memoryview, init_crc: int = 0) -> int:
    """Compute Castagnoli CRC32C using slicing-by-8 pure Python."""
    t0, t1, t2, t3, t4, t5, t6, t7 = _SLICING8
    mv = memoryview(data)
    n = len(mv)
    aligned = n & ~7
    crc = init_crc ^ 0xFFFFFFFF

    if aligned:
        for (q,) in struct.iter_unpack("<Q", mv[:aligned]):
            lo = crc ^ (q & 0xFFFFFFFF)
            hi = q >> 32
            crc = (
                t7[lo & 0xFF]
                ^ t6[(lo >> 8) & 0xFF]
                ^ t5[(lo >> 16) & 0xFF]
                ^ t4[lo >> 24]
                ^ t3[hi & 0xFF]
                ^ t2[(hi >> 8) & 0xFF]
                ^ t1[(hi >> 16) & 0xFF]
                ^ t0[hi >> 24]
            )

    for b in mv[aligned:]:
        crc = t0[(crc ^ b) & 0xFF] ^ (crc >> 8)

    return crc ^ 0xFFFFFFFF


def _try_af_alg_crc32c_file(path: Path) -> int | None:
    """Use Linux kernel AF_ALG hardware CRC32C when algif_hash is available."""
    if not hasattr(socket, "AF_ALG"):
        return None
    alg_sock: socket.socket | None = None
    op_sock: socket.socket | None = None
    try:
        alg_sock = socket.socket(socket.AF_ALG, socket.SOCK_SEQPACKET, 0)
        alg_sock.bind(("hash", "crc32c"))
        op_sock, _ = alg_sock.accept()
        with path.open("rb") as fh:
            while chunk := fh.read(_CHUNK_SIZE):
                op_sock.sendall(chunk)
        digest = op_sock.recv(4)
        if len(digest) != 4:
            return None
        return struct.unpack("<I", digest)[0]
    except OSError:
        return None
    finally:
        if op_sock is not None:
            op_sock.close()
        if alg_sock is not None:
            alg_sock.close()


def _try_optional_crc32c_module_file(path: Path) -> int | None:
    """Use optional google_crc32c or crc32c C extension if installed on host."""
    try:
        import google_crc32c  # type: ignore[import-not-found]

        hasher = google_crc32c.Checksum()
        with path.open("rb") as fh:
            while chunk := fh.read(_CHUNK_SIZE):
                hasher.update(chunk)
        return int.from_bytes(hasher.digest(), "big")
    except ImportError:
        pass

    try:
        import crc32c as crc32c_mod  # type: ignore[import-not-found]

        val = 0
        with path.open("rb") as fh:
            while chunk := fh.read(_CHUNK_SIZE):
                val = crc32c_mod.crc32c(chunk, val)
        return int(val) & 0xFFFFFFFF
    except ImportError:
        return None


def compute_crc32c_file(path: Path) -> int:
    """Compute unsigned 32-bit Castagnoli CRC32C for a file."""
    fast_ext = _try_optional_crc32c_module_file(path)
    if fast_ext is not None:
        return fast_ext

    fast_kernel = _try_af_alg_crc32c_file(path)
    if fast_kernel is not None:
        return fast_kernel

    crc = 0
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK_SIZE):
            crc = crc32c_pure_python(chunk, crc)
    return crc


def crc32c_to_gcs_base64(crc_value: int) -> str:
    """Encode an unsigned 32-bit CRC32C integer to GCS x-goog-hash base64 format."""
    return base64.b64encode(struct.pack(">I", crc_value & 0xFFFFFFFF)).decode("ascii")


def parse_gcs_crc32c_headers(header_values: list[str]) -> str | None:
    """Extract the base64 crc32c token from one or more x-goog-hash header values."""
    for raw in header_values:
        for part in raw.split(","):
            token = part.strip()
            if token.lower().startswith("crc32c="):
                val = token.split("=", 1)[1].strip()
                if val:
                    return val
    return None


def compute_hash_file(path: Path, kind: ChecksumKind) -> str:
    """Compute a file digest matching the requested ChecksumKind."""
    if kind is ChecksumKind.CRC32C_B64:
        return crc32c_to_gcs_base64(compute_crc32c_file(path))

    if kind is ChecksumKind.SHA256:
        hasher = hashlib.sha256()
    elif kind is ChecksumKind.SHA512:
        hasher = hashlib.sha512()
    elif kind is ChecksumKind.BLAKE2B:
        hasher = hashlib.blake2b()
    else:
        raise IntegrityError(f"Unsupported checksum algorithm: {kind}")

    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_file(
    path: Path,
    *,
    expected_kind: ChecksumKind,
    expected_value: str,
    expected_size: int | None = None,
) -> str:
    """Verify file size (if known) and cryptographic/transport digest; return actual digest."""
    if not path.is_file():
        raise IntegrityError(f"Cannot verify missing file: {path}")

    if expected_size is not None:
        actual_size = path.stat().st_size
        if actual_size != expected_size:
            raise IntegrityError(
                f"Size mismatch for {path.name}: expected {expected_size} bytes, got {actual_size}"
            )

    actual = compute_hash_file(path, expected_kind)
    expected_norm = expected_value.strip()
    if expected_kind is not ChecksumKind.CRC32C_B64:
        expected_norm = expected_norm.lower()
        actual = actual.lower()

    if actual != expected_norm:
        raise IntegrityError(
            f"{expected_kind.value} mismatch for {path.name}: "
            f"expected {expected_norm}, got {actual}"
        )
    return actual


def makepkg_sum_field_to_kind(field_prefix: str) -> ChecksumKind:
    """Map PKGBUILD checksum array prefix (b2, sha256, sha512) to ChecksumKind."""
    mapping = {
        "b2": ChecksumKind.BLAKE2B,
        "sha256": ChecksumKind.SHA256,
        "sha512": ChecksumKind.SHA512,
    }
    if field_prefix not in mapping:
        raise IntegrityError(f"Unsupported PKGBUILD checksum prefix: {field_prefix!r}")
    return mapping[field_prefix]
