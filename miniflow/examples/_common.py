"""Shared helpers for the real-world example pipelines (standard library only).

Network access
    ``fetch_json`` / ``fetch_text`` download live public data with ``urllib``.
    Timeouts, connection errors and 5xx/429 replies raise ``ConnectionError`` so
    MiniFlow's retries handle them; other 4xx replies are permanent.

Offline mode
    With ``MINIFLOW_OFFLINE=1`` every download is answered from a saved snapshot
    of real data in ``snapshots/`` (gzip JSON or text). Use it for demos without
    internet. ``MINIFLOW_RECORD_SNAPSHOTS=1`` saves fresh snapshots while running live.

Outputs
    Reports and simulated notifications are written under ``miniflow_reports/``
    (or ``$MINIFLOW_REPORTS``) in the current folder.
"""

from __future__ import annotations

import gzip
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

__all__ = [
    "PermanentFetchError",
    "fetch_json",
    "fetch_text",
    "inr",
    "offline",
    "reports_dir",
    "send_simulated",
    "write_report",
]

SNAPSHOTS = Path(__file__).parent / "snapshots"
USER_AGENT = "MiniFlow-examples/0.1 (+https://github.com/StutiArya2/miniflow)"
TEXT_SNAPSHOT_LIMIT = 60_000


class PermanentFetchError(RuntimeError):
    """The server refused for good (a 4xx reply other than 429); retrying won't help."""


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def offline() -> bool:
    return _flag("MINIFLOW_OFFLINE")


def _snapshot_path(key: str, ext: str) -> Path:
    return SNAPSHOTS / f"{key}.{ext}.gz"


def _read_snapshot(key: str, ext: str) -> str:
    path = _snapshot_path(key, ext)
    if not path.exists():
        failure = _snapshot_path(key, "err")
        if failure.exists():  # the live recording failed here: replay that failure
            message = gzip.decompress(failure.read_bytes()).decode("utf-8")
            raise ConnectionError(f"{message} (replayed from the saved snapshot)")
        raise FileNotFoundError(f"offline mode: no saved snapshot '{path.name}'")
    return gzip.decompress(path.read_bytes()).decode("utf-8")


def _recording() -> bool:
    return _flag("MINIFLOW_RECORD_SNAPSHOTS")


def _record(key: str, ext: str, text: str) -> None:
    if _recording():
        SNAPSHOTS.mkdir(exist_ok=True)
        _snapshot_path(key, ext).write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))
        _snapshot_path(key, "err").unlink(missing_ok=True)


def _record_failure(key: str, message: str) -> None:
    if _recording():
        SNAPSHOTS.mkdir(exist_ok=True)
        _snapshot_path(key, "err").write_bytes(gzip.compress(message.encode("utf-8"), mtime=0))


def _download(url: str, timeout: float, limit: int) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return str(resp.read(limit).decode(charset, errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429 or exc.code >= 500:
            raise ConnectionError(f"{url}: server busy (HTTP {exc.code})") from exc
        raise PermanentFetchError(f"{url}: refused (HTTP {exc.code})") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ConnectionError(f"{url}: {reason}") from exc


def fetch_json(url: str, *, snapshot: str, timeout: float = 15) -> Any:
    """Download and parse JSON (or read the snapshot ``snapshot`` in offline mode)."""
    if offline():
        return json.loads(_read_snapshot(snapshot, "json"))
    try:
        text = _download(url, timeout, limit=20_000_000)
    except ConnectionError as exc:
        _record_failure(snapshot, str(exc))
        raise
    _record(snapshot, "json", text)
    return json.loads(text)


def fetch_text(
    url: str,
    *,
    snapshot: str,
    timeout: float = 15,
    limit: int = 2_000_000,
    on_refused: str | None = None,
) -> str:
    """Download a web page as text (or read the snapshot ``snapshot`` in offline mode).

    ``on_refused``: returned instead of raising when the site refuses for good (4xx).
    """
    if offline():
        return _read_snapshot(snapshot, "txt")
    try:
        text = _download(url, timeout, limit=limit)
    except ConnectionError as exc:
        _record_failure(snapshot, str(exc))
        raise
    except PermanentFetchError:
        if on_refused is None:
            raise
        text = on_refused
    _record(snapshot, "txt", text[:TEXT_SNAPSHOT_LIMIT])
    return text


def reports_dir() -> Path:
    path = Path(os.environ.get("MINIFLOW_REPORTS", "miniflow_reports"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_report(name: str, text: str) -> Path:
    """Save a Markdown report as ``<reports>/<name>-<timestamp>.md``."""
    path = reports_dir() / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(text, encoding="utf-8")
    return path


def send_simulated(channel: str, messages: list[str]) -> Path:
    """Stand-in for email, SMS or push: messages are written to ``<reports>/outbox/``."""
    outbox = reports_dir() / "outbox"
    outbox.mkdir(exist_ok=True)
    path = outbox / f"{channel}-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    path.write_text("\n\n".join(messages) + "\n", encoding="utf-8")
    return path


def inr(amount: float) -> str:
    """Rupees with Indian digit grouping: 1234567.8 -> '₹12,34,568'."""
    n = round(amount)
    sign, digits = ("-" if n < 0 else ""), str(abs(n))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join([*groups, tail])
    return f"{sign}₹{digits}"
