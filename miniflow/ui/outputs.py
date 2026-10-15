"""Files a run wrote, and previews of them for the dashboard (standard library only).

A step's output file is the ``"path"`` in the mapping it returned (``write_csv``,
``write_xlsx``, ``write_json`` and ``s3_put`` all return one), or a returned string that
names an existing file with a known extension. Only those files can be previewed or
opened, so the dashboard never reads a path a browser asks for.

``find_outputs(store, run_id) -> list[dict]``
    ``{"task", "path", "name", "kind", "size", "exists"}`` for each successful step with an
    output file, in run order.
``output_path(store, run_id, task) -> Path``
    The file that step wrote; ``OutputError`` if it has none or the file is gone.
``preview(path) -> dict``
    ``{"kind", ...}`` ready to draw:

    - ``table`` (CSV, TSV, Excel): ``{"sheets": [{"name", "rows", "total", "columns"}]}``,
      rows as lists of cells in sheet order, the header row included, at most
      ``MAX_ROWS`` rows;
    - ``document`` (Word .docx): ``{"blocks": [...]}``, each ``{"type": "heading",
      "level", "runs"}``, ``{"type": "paragraph", "list", "runs"}`` or
      ``{"type": "table", "rows"}``; runs are ``{"text", "bold", "italic", "underline"}``;
    - ``markdown``, ``text``, ``html``: ``{"text", "truncated"}``;
    - ``image``, ``pdf``: nothing more; the page loads the file itself;
    - ``other``: the file can only be opened or downloaded.
``content_type(path) -> str``
    The media type to serve the raw file with.
``open_file(path) -> None``
    Open the file in the computer's default app (Excel, Word, Preview and so on).
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from miniflow.blocks.builtin.office import read_sheets
from miniflow.states import State
from miniflow.store import StateStore

__all__ = [
    "MAX_ROWS",
    "OutputError",
    "content_type",
    "find_outputs",
    "open_file",
    "output_path",
    "preview",
]

MAX_ROWS = 1000
MAX_TEXT = 256 * 1024
MAX_PREVIEW_BYTES = 50 * 1024 * 1024
MAX_BLOCKS = 3000

_KINDS = {
    ".csv": "table",
    ".tsv": "table",
    ".xlsx": "table",
    ".xlsm": "table",
    ".docx": "document",
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".log": "text",
    ".json": "text",
    ".xml": "text",
    ".yaml": "text",
    ".yml": "text",
    ".html": "html",
    ".htm": "html",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".svg": "image",
    ".pdf": "pdf",
}
_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
}
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class OutputError(LookupError):
    """The step wrote no file that can be shown."""


def _kind(path: Path) -> str:
    return _KINDS.get(path.suffix.lower(), "other")


def _candidate(value: Any) -> str | None:
    if isinstance(value, dict) and isinstance(value.get("path"), str):
        return str(value["path"])
    if isinstance(value, str) and Path(value).suffix.lower() in _KINDS and "\n" not in value:
        return value
    return None


def _paths(store: StateStore, run_id: str) -> dict[str, str]:
    tasks = [t for t in store.changes_since(run_id, 0.0) if t.state is State.SUCCESS]
    found = {t.task: p for t in tasks if (p := _candidate(t.sample)) is not None}
    # A sample keeps 240 characters of a string; read the stored result for long paths.
    if any(not Path(p).is_file() for p in found.values()) or any(
        isinstance(t.sample, dict) and "path" in t.sample and t.task not in found for t in tasks
    ):
        completed = store.load_completed(run_id)
        for t in tasks:
            full = _candidate(completed.get(t.task))
            if full is not None:
                found[t.task] = full
    order = [t.task for t in sorted(tasks, key=lambda t: t.finished_at or 0.0)]
    return {task: found[task] for task in order if task in found}


def find_outputs(store: StateStore, run_id: str) -> list[dict[str, Any]]:
    out = []
    for task, text in _paths(store, run_id).items():
        path = Path(text)
        exists = path.is_file()
        out.append(
            {
                "task": task,
                "path": str(path),
                "name": path.name,
                "kind": _kind(path),
                "size": path.stat().st_size if exists else None,
                "exists": exists,
            }
        )
    return out


def output_path(store: StateStore, run_id: str, task: str) -> Path:
    text = _paths(store, run_id).get(task)
    if text is None:
        raise OutputError(f"step {task!r} wrote no file in this run")
    path = Path(text)
    if not path.is_file():
        raise OutputError(f"{path.name} no longer exists at {path.parent}")
    return path


def content_type(path: Path) -> str:
    return _TYPES.get(path.suffix.lower(), "application/octet-stream")


# ------------------------------------------------------------------ previews
def _table_csv(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    if path.suffix.lower() == ".tsv":
        delimiter = "\t"
    else:
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    rows: list[list[Any]] = []
    total = 0
    for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter):
        total += 1
        if len(rows) < MAX_ROWS:
            rows.append(row)
    return {"name": path.stem, "rows": rows, "total": total, "columns": _width(rows)}


def _width(rows: list[list[Any]]) -> int:
    return max((len(r) for r in rows), default=0)


def _table(path: Path) -> dict[str, Any]:
    if path.suffix.lower() in (".csv", ".tsv"):
        sheets = [_table_csv(path)]
    else:
        sheets = []
        for name, grid in read_sheets(str(path)):
            rows = grid[:MAX_ROWS]
            sheets.append({"name": name, "rows": rows, "total": len(grid), "columns": _width(rows)})
    return {"kind": "table", "sheets": sheets}


def _runs(paragraph: ET.Element) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for run in paragraph.iter(f"{_W}r"):
        props = run.find(f"{_W}rPr")

        def on(tag: str, props: ET.Element | None = props) -> bool:
            node = None if props is None else props.find(f"{_W}{tag}")
            return node is not None and node.get(f"{_W}val", "true") not in ("0", "false", "none")

        text = []
        for part in run:
            if part.tag == f"{_W}t":
                text.append(part.text or "")
            elif part.tag == f"{_W}tab":
                text.append("\t")
            elif part.tag in (f"{_W}br", f"{_W}cr"):
                text.append("\n")
        if not text:
            continue
        item = {"text": "".join(text), "bold": on("b"), "italic": on("i"), "underline": on("u")}
        if runs and all(runs[-1][k] == item[k] for k in ("bold", "italic", "underline")):
            runs[-1]["text"] += item["text"]
        else:
            runs.append(item)
    return runs


def _paragraph(p: ET.Element) -> dict[str, Any]:
    props = p.find(f"{_W}pPr")
    style = ""
    listed = False
    if props is not None:
        node = props.find(f"{_W}pStyle")
        style = (node.get(f"{_W}val", "") if node is not None else "").lower()
        listed = props.find(f"{_W}numPr") is not None or style.startswith("list")
    runs = _runs(p)
    if style == "title":
        return {"type": "heading", "level": 1, "runs": runs}
    if style.startswith("heading") and style[7:].isdigit():
        return {"type": "heading", "level": min(int(style[7:]) + 1, 6), "runs": runs}
    if style == "subtitle":
        return {"type": "heading", "level": 2, "runs": runs}
    return {"type": "paragraph", "list": listed, "runs": runs}


def _cell_text(cell: ET.Element) -> str:
    return "\n".join("".join(r["text"] for r in _runs(p)) for p in cell.iter(f"{_W}p")).strip()


def _document(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > MAX_PREVIEW_BYTES:
                raise OutputError(f"{path.name} is too large to preview")
            data = z.read(info)
    except (zipfile.BadZipFile, KeyError):
        raise OutputError(f"{path.name} isn't a Word .docx file") from None
    # Word files never need a DTD; refusing one rules out entity-expansion bombs.
    if any(
        m in data
        for w in ("<!DOCTYPE", "<!ENTITY")
        for m in (w.encode(), w.encode("utf-16-le"), w.encode("utf-16-be"))
    ):
        raise OutputError(f"{path.name} contains a DTD, so it was not read")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise OutputError(f"{path.name} isn't a readable Word file") from None
    body = root.find(f"{_W}body")
    blocks: list[dict[str, Any]] = []
    for node in [] if body is None else list(body):
        if len(blocks) >= MAX_BLOCKS:
            break
        if node.tag == f"{_W}p":
            blocks.append(_paragraph(node))
        elif node.tag == f"{_W}tbl":
            rows = [
                [_cell_text(cell) for cell in row.findall(f"{_W}tc")]
                for row in node.findall(f"{_W}tr")
            ]
            blocks.append({"type": "table", "rows": rows[:MAX_ROWS]})
    return {"kind": "document", "blocks": blocks, "truncated": len(blocks) >= MAX_BLOCKS}


def _text(path: Path, kind: str) -> dict[str, Any]:
    with path.open("rb") as f:
        raw = f.read(MAX_TEXT + 1)
    text = raw[:MAX_TEXT].decode("utf-8", errors="replace")
    truncated = len(raw) > MAX_TEXT
    if path.suffix.lower() == ".json" and not truncated:
        with contextlib.suppress(ValueError):
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    return {"kind": kind, "text": text, "truncated": truncated}


def preview(path: Path) -> dict[str, Any]:
    kind = _kind(path)
    if kind in ("table", "document") and path.stat().st_size > MAX_PREVIEW_BYTES:
        raise OutputError(f"{path.name} is too large to preview; open it instead")
    if kind == "table":
        try:
            return _table(path)
        except ValueError as exc:
            raise OutputError(str(exc)) from None
    if kind == "document":
        return _document(path)
    if kind in ("markdown", "text", "html"):
        return _text(path, kind)
    return {"kind": kind}


def open_file(path: Path) -> None:
    """Open ``path`` with the app the computer uses for that kind of file."""
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined,unused-ignore]
        return
    command = "open" if sys.platform == "darwin" else "xdg-open"
    # an absolute path can't be mistaken for an option of the opener
    subprocess.Popen(
        [command, str(path.resolve())],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
