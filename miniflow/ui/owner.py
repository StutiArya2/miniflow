"""Which dashboard runs flows for a state database (standard library only).

Only one process may launch runs for a database: :class:`~miniflow.ui.control.RunController`
matches each process it starts to "the newest run of that flow since launch", so two
controllers on one database would take each other's runs. A dashboard with a flows folder
claims ``<db>.owner.json`` once it is serving, and removes it when it stops:

    owner_path(db) -> Path
    claim(db, url, flows) -> Owner          OwnerError if a live dashboard holds the file
    release(db, owner) -> None              removes the file only if it is still ``owner``'s
    read_owner(db) -> Owner | None          the file's contents, live or not
    is_live(owner, timeout=2.0) -> bool     GET <url>/api/owner answers with owner's token

The file is created with exclusive create, so two dashboards starting together can't both
win. A file whose dashboard doesn't answer with its token (it crashed, or another program
now has the port) is replaced, but only if it is unchanged since it was judged dead. A
fresh file that can't be read yet belongs to a dashboard that is writing it, so the claim is
refused rather than taken.
"""

from __future__ import annotations

import http.client
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

__all__ = ["Owner", "OwnerError", "claim", "is_live", "owner_path", "read_owner", "release"]

_STARTING_SECONDS = 5.0


class OwnerError(ValueError):
    """Another dashboard runs flows for this database."""


@dataclass(frozen=True)
class Owner:
    pid: int
    url: str
    flows: str
    token: str
    started_at: float


def owner_path(db: str | os.PathLike[str]) -> Path:
    return Path(f"{Path(db).resolve()}.owner.json")


def read_owner(db: str | os.PathLike[str]) -> Owner | None:
    try:
        data = json.loads(owner_path(db).read_text(encoding="utf-8"))
        return Owner(
            pid=int(data["pid"]),
            url=str(data["url"]),
            flows=str(data["flows"]),
            token=str(data["token"]),
            started_at=float(data["started_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def is_live(owner: Owner, timeout: float = 2.0) -> bool:
    try:
        url = urlsplit(owner.url)
        if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost") or not url.port:
            return False
    except ValueError:
        return False
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=timeout)
    try:
        conn.request("GET", "/api/owner")
        response = conn.getresponse()
        body = json.loads(response.read() or b"null")
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        conn.close()
    return response.status == 200 and isinstance(body, dict) and body.get("token") == owner.token


def claim(db: str | os.PathLike[str], url: str, flows: str | os.PathLike[str]) -> Owner:
    path = owner_path(db)
    owner = Owner(os.getpid(), url, str(Path(flows).resolve()), secrets.token_hex(16), time.time())
    for _ in range(3):
        try:
            with path.open("x", encoding="utf-8") as f:
                try:
                    f.write(json.dumps(asdict(owner)))
                except OSError:
                    f.close()
                    path.unlink(missing_ok=True)
                    raise
            return owner
        except FileExistsError:
            current = read_owner(db)
            stale: str | tuple[int, int]
            if current is None:
                try:
                    seen = path.stat()
                except FileNotFoundError:
                    continue  # it was just released; try again
                if time.time() - seen.st_mtime < _STARTING_SECONDS:
                    raise OwnerError(
                        "another dashboard for this database is starting; try again"
                    ) from None
                stale = (seen.st_ino, seen.st_mtime_ns)
            elif is_live(current):
                raise OwnerError(
                    f"a dashboard for this database is already running at {current.url}"
                ) from None
            else:
                stale = current.token
            if _fingerprint(db) == stale:
                path.unlink(missing_ok=True)  # left by a dashboard that stopped uncleanly
    raise OwnerError(f"couldn't claim {path}; another dashboard is starting")


def _fingerprint(db: str | os.PathLike[str]) -> str | tuple[int, int] | None:
    """The token of a readable owner file, else its inode and mtime (None if absent)."""
    current = read_owner(db)
    if current is not None:
        return current.token
    try:
        seen = owner_path(db).stat()
    except OSError:
        return None
    return (seen.st_ino, seen.st_mtime_ns)


def release(db: str | os.PathLike[str], owner: Owner) -> None:
    current = read_owner(db)
    if current is not None and current.token == owner.token:
        owner_path(db).unlink(missing_ok=True)
