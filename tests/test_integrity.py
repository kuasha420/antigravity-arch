"""Hermetic tests for CRC32C, GCS header parsing, and cryptographic verification."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from antigravity_arch.integrity import (
    compute_crc32c_file,
    compute_hash_file,
    crc32c_pure_python,
    crc32c_to_gcs_base64,
    parse_gcs_crc32c_headers,
    verify_file,
)
from antigravity_arch.models import ChecksumKind, IntegrityError


def test_crc32c_rfc3720_standard_vector() -> None:
    # RFC 3720 Appendix B.4 standard check value for ASCII "123456789" is 0xe3069283
    val = crc32c_pure_python(b"123456789")
    assert val == 0xE3069283
    assert crc32c_to_gcs_base64(val) == "4waSgw=="


def test_crc32c_incremental_matches_oneshot() -> None:
    payload = b"antigravity-arch-castagnoli-vector-" * 4099
    oneshot = crc32c_pure_python(payload)
    crc = 0
    for idx in range(0, len(payload), 137):
        crc = crc32c_pure_python(payload[idx : idx + 137], crc)
    assert crc == oneshot


def test_parse_gcs_crc32c_headers() -> None:
    assert parse_gcs_crc32c_headers(["crc32c=BqScAQ=="]) == "BqScAQ=="
    assert parse_gcs_crc32c_headers(["md5=abc123==, crc32c=4waSgw=="]) == "4waSgw=="
    assert parse_gcs_crc32c_headers(["md5=abc==", "crc32c=OOlQ1g=="]) == "OOlQ1g=="
    assert parse_gcs_crc32c_headers(["md5=only=="]) is None
    assert parse_gcs_crc32c_headers([]) is None


def test_verify_file_all_algorithms(tmp_path: Path) -> None:
    target = tmp_path / "sample.tar.gz"
    content = b"123456789"
    target.write_bytes(content)

    assert compute_crc32c_file(target) == 0xE3069283
    assert verify_file(
        target,
        expected_kind=ChecksumKind.CRC32C_B64,
        expected_value="4waSgw==",
        expected_size=len(content),
    ) == "4waSgw=="

    expected_sha256 = hashlib.sha256(content).hexdigest()
    assert verify_file(
        target,
        expected_kind=ChecksumKind.SHA256,
        expected_value=expected_sha256.upper(),
    ) == expected_sha256

    expected_sha512 = hashlib.sha512(content).hexdigest()
    assert verify_file(
        target,
        expected_kind=ChecksumKind.SHA512,
        expected_value=expected_sha512,
    ) == expected_sha512

    expected_b2 = hashlib.blake2b(content).hexdigest()
    assert compute_hash_file(target, ChecksumKind.BLAKE2B) == expected_b2


def test_verify_file_detects_corruption_and_size_mismatch(tmp_path: Path) -> None:
    target = tmp_path / "corrupt.tar.gz"
    target.write_bytes(b"123456789")

    with pytest.raises(IntegrityError, match="Size mismatch"):
        verify_file(
            target,
            expected_kind=ChecksumKind.CRC32C_B64,
            expected_value="4waSgw==",
            expected_size=999,
        )

    with pytest.raises(IntegrityError, match="mismatch"):
        verify_file(
            target,
            expected_kind=ChecksumKind.SHA256,
            expected_value="0" * 64,
        )
