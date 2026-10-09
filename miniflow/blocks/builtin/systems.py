"""Systems: shell (run a program) and sql (SQLite)."""

from __future__ import annotations

import os
import shlex
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from miniflow.blocks import Setting, block


@block(
    "shell",
    settings=[
        Setting("command", (str, list), help="Program and arguments, e.g. python report.py"),
        Setting("cwd", str, default=None, file=True, help="Folder to run in (default: the flow's)"),
        Setting("timeout", float, default=60.0),
        Setting("check", bool, default=True, help="Fail the step if the exit code isn't 0"),
        Setting("env", dict, default=None, help="Extra environment variables"),
    ],
)
def shell(
    command: str | list[str],
    cwd: str | None = None,
    timeout: float = 60.0,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run a program and capture its output (no shell features such as pipes)."""
    args = (
        shlex.split(command, posix=os.name != "nt") if isinstance(command, str) else list(command)
    )
    done = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **(env or {})},
        check=False,
    )
    if check and done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()[-5:]
        raise RuntimeError(
            f"{args[0]} failed with exit code {done.returncode}"
            + (": " + " / ".join(detail) if detail else "")
        )
    return {"code": done.returncode, "stdout": done.stdout, "stderr": done.stderr}


@block(
    "sql",
    settings=[
        Setting("database", str, file=True, help="SQLite database file (created if missing)"),
        Setting("query", str, help="One SQL statement; use ? or :name for values"),
        Setting("parameters", (list, dict), default=None, help="Values for ? or :name"),
        Setting("many", list, default=None, help="Run once per item, e.g. insert {{ rows }}"),
    ],
)
def sql(
    database: str,
    query: str,
    parameters: list[Any] | dict[str, Any] | None = None,
    many: list[Any] | None = None,
) -> Any:
    """Run a SQL statement on a SQLite database: rows for SELECT, otherwise a change count."""
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database)
    try:
        conn.row_factory = sqlite3.Row
        with conn:
            if many is not None:
                cur = conn.executemany(query, many)
            else:
                cur = conn.execute(query, parameters if parameters is not None else ())
            if cur.description is not None:
                return [dict(r) for r in cur.fetchall()]
            return {"changed": cur.rowcount}
    finally:
        conn.close()
