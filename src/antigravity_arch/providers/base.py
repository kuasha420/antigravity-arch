"""Stdlib HTTP client with retries, timeouts, and atomic streaming downloads."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import __version__
from ..models import ProviderError

_USER_AGENT = f"antigravity-arch/{__version__} (+https://github.com/kuasha420/antigravity-arch)"
_DOWNLOAD_CHUNK = 512 * 1024  # 512 KiB


@dataclass(frozen=True)
class HttpResponse:
    """Minimal immutable HTTP response wrapper."""

    url: str
    status: int
    headers: dict[str, list[str]]
    body: bytes

    def get_headers(self, name: str) -> list[str]:
        return list(self.headers.get(name.lower(), []))

    def get_header(self, name: str) -> str | None:
        values = self.headers.get(name.lower())
        return values[0] if values else None

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise ProviderError(f"Invalid JSON from {self.url}: {err}") from err


def _normalize_headers(msg: Any) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for key, value in msg.items():
        lower = key.lower()
        result.setdefault(lower, []).append(value)
    return result


class HttpClient:
    """Lightweight stdlib urllib client with bounded retries and atomic downloads."""

    def __init__(
        self,
        *,
        timeout: int = 20,
        max_retries: int = 2,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep_fn

    def _open_with_retries(self, req: urllib.request.Request) -> Any:
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return urllib.request.urlopen(req, timeout=self.timeout)
            except urllib.error.HTTPError as err:
                if 500 <= err.code < 600 and attempt < self.max_retries:
                    last_err = err
                    self._sleep(0.5 * (attempt + 1))
                    continue
                raise ProviderError(f"HTTP {err.code} fetching {req.full_url}: {err.reason}") from err
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                last_err = err
                if attempt < self.max_retries:
                    self._sleep(0.5 * (attempt + 1))
                    continue
                raise ProviderError(f"Network error fetching {req.full_url}: {err}") from err
        raise ProviderError(f"Failed fetching {req.full_url}: {last_err}")

    def request(self, url: str, *, method: str = "GET") -> HttpResponse:
        req = urllib.request.Request(
            url,
            method=method,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json, */*"},
        )
        with self._open_with_retries(req) as resp:
            body = b"" if method == "HEAD" else resp.read()
            return HttpResponse(
                url=resp.geturl(),
                status=int(resp.status),
                headers=_normalize_headers(resp.headers),
                body=body,
            )

    def get_json(self, url: str) -> Any:
        return self.request(url, method="GET").json()

    def head(self, url: str) -> HttpResponse:
        return self.request(url, method="HEAD")

    def download_file(
        self,
        url: str,
        dest: Path,
        *,
        progress_cb: Callable[[int, int | None], None] | None = None,
    ) -> Path:
        """Stream-download URL to dest via atomic .part rename."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(f".{dest.name}.part")
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with self._open_with_retries(req) as resp, part.open("wb") as out:
                raw_len = resp.headers.get("Content-Length")
                total = int(raw_len) if raw_len and raw_len.isdigit() else None
                downloaded = 0
                while chunk := resp.read(_DOWNLOAD_CHUNK):
                    out.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb is not None:
                        progress_cb(downloaded, total)
                if total is not None and downloaded != total:
                    raise ProviderError(
                        f"Incomplete download for {dest.name}: received {downloaded} of {total} bytes"
                    )
            os.replace(part, dest)
            return dest
        except Exception:
            if part.exists():
                part.unlink()
            raise
