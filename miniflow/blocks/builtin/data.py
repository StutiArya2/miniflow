"""Data in and out: http_request, read_csv, write_csv, read_json, write_json."""

from __future__ import annotations

import csv
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from miniflow.blocks import Setting, block

_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


@block(
    "http_request",
    settings=[
        Setting("url", str, help="Address to call, e.g. https://api.example.com/items"),
        Setting("method", str, default="GET", choices=_METHODS),
        Setting("headers", dict, default=None, help="Extra headers, e.g. Authorization"),
        Setting("query", dict, default=None, help="Added to the URL as ?name=value"),
        Setting("body", object, default=None, help="Sent as JSON (object or list) or as text"),
        Setting("timeout", float, default=30.0, help="Seconds to wait for an answer"),
        Setting("fail_on_error", bool, default=True, help="Fail the step on a 4xx/5xx status"),
    ],
)
def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
    body: Any = None,
    timeout: float = 30.0,
    fail_on_error: bool = True,
) -> dict[str, Any]:
    """Call a web address and return its status, headers, text and JSON (if any)."""
    if query:
        url += ("&" if urllib.parse.urlsplit(url).query else "?") + urllib.parse.urlencode(query)
    all_headers = dict(headers or {})
    data = None
    if body is not None:
        if isinstance(body, dict | list):
            data = json.dumps(body).encode()
            all_headers.setdefault("Content-Type", "application/json")
        else:
            data = str(body).encode()
            all_headers.setdefault("Content-Type", "text/plain; charset=utf-8")
    request = urllib.request.Request(url, data=data, headers=all_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw, info = response.status, response.read(), response.headers
    except urllib.error.HTTPError as err:
        if fail_on_error:
            raise RuntimeError(f"{method} {url} returned {err.code} {err.reason}") from None
        status, raw, info = err.code, err.read(), err.headers
    text = raw.decode(info.get_content_charset() or "utf-8", errors="replace")
    try:
        parsed = json.loads(text) if "json" in (info.get_content_type() or "") else None
    except ValueError:
        parsed = None
    return {"status": status, "headers": dict(info.items()), "text": text, "json": parsed}


def _number(text: str) -> Any:
    for convert in (int, float):
        try:
            return convert(text)
        except ValueError:
            pass
    return text


def _open(path: str) -> Path:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"there is no such file: {p}")
    return p


@block(
    "read_csv",
    settings=[
        Setting("path", str, file=True, help="CSV file to read (first row = column names)"),
        Setting("delimiter", str, default=","),
        Setting("convert_numbers", bool, default=True, help="Turn '120' into the number 120"),
    ],
)
def read_csv(path: str, delimiter: str = ",", convert_numbers: bool = True) -> list[dict[str, Any]]:
    """Read a CSV file into a list of rows."""
    with _open(path).open(newline="", encoding="utf-8-sig") as f:
        rows: list[dict[str, Any]] = list(csv.DictReader(f, delimiter=delimiter))
    if convert_numbers:
        rows = [{k: _number(v) if isinstance(v, str) else v for k, v in r.items()} for r in rows]
    return rows


@block(
    "write_csv",
    settings=[
        Setting("path", str, file=True, help="CSV file to write; folders are created"),
        Setting("rows", list, help="Rows to write, e.g. {{ previous_step }}"),
        Setting("columns", list, default=None, help="Column order (default: as they appear)"),
        Setting("delimiter", str, default=","),
    ],
)
def write_csv(
    path: str, rows: list[dict[str, Any]], columns: list[str] | None = None, delimiter: str = ","
) -> dict[str, Any]:
    """Write a list of rows to a CSV file."""
    if columns is None:
        columns = []
        for row in rows:
            columns += [k for k in row if k not in columns]
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, delimiter=delimiter, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return {"path": str(target), "rows": len(rows)}


@block("read_json", settings=[Setting("path", str, file=True)])
def read_json(path: str) -> Any:
    """Read a JSON file."""
    return json.loads(_open(path).read_text(encoding="utf-8"))


@block(
    "write_json",
    settings=[
        Setting("path", str, file=True, help="JSON file to write; folders are created"),
        Setting("data", object, help="What to write, e.g. {{ previous_step }}"),
        Setting("indent", int, default=2),
    ],
)
def write_json(path: str, data: Any, indent: int = 2) -> dict[str, Any]:
    """Write a value to a JSON file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=indent, ensure_ascii=False, default=str) + "\n"
    target.write_text(text, encoding="utf-8")
    return {"path": str(target), "bytes": target.stat().st_size}
