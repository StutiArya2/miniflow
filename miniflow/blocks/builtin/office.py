"""Excel files: read_xlsx and write_xlsx (standard library only).

An ``.xlsx`` file is a zip of XML files, so ``zipfile`` and ``xml.etree`` are enough for
plain data sheets: text, numbers, true/false and empty cells. Formulas are read as the
value Excel last calculated; styles, charts and images are not read or written.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from miniflow.blocks import Setting, block
from miniflow.blocks.builtin.data import _open

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_MAX_PART = 200 * 1024 * 1024  # refuse to unpack a part bigger than this (zip bombs)
_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
_BAD_SHEET = re.compile(r"[\[\]:*?/\\]")


def _column_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref.upper())
    n = 0
    for ch in letters.group(0) if letters else "A":
        n = n * 26 + ord(ch) - 64
    return n


def _column_letters(n: int) -> str:
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _text(node: ET.Element | None) -> str:
    """All the text in a shared-string or inline-string element (rich-text runs joined)."""
    if node is None:
        return ""
    return "".join(t.text or "" for t in node.iter(f"{_NS}t"))


def _read_part(z: zipfile.ZipFile, name: str) -> ET.Element:
    info = z.getinfo(name)
    if info.file_size > _MAX_PART:
        raise ValueError(f"{name} is too large to read")
    data = z.read(name)
    # A spreadsheet never needs a DTD; refusing them rules out entity-expansion bombs
    # without a third-party XML library.
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError(f"{name} contains a DTD, which Excel files never need, so it was not read")
    return ET.fromstring(data)


def _number(text: str) -> int | float:
    try:
        return int(text)
    except ValueError:
        return float(text)


def _cell(c: ET.Element, shared: list[str]) -> Any:
    kind = c.get("t")
    if kind == "inlineStr":
        return _text(c.find(f"{_NS}is"))
    v = c.find(f"{_NS}v")
    if v is None or v.text is None:
        return None
    if kind == "s":
        return shared[int(v.text)]
    if kind == "b":
        return v.text.strip() == "1"
    if kind in ("str", "e"):
        return v.text
    return _number(v.text)


@block(
    "read_xlsx",
    settings=[
        Setting("path", str, file=True, help="Excel .xlsx file to read"),
        Setting("sheet", str, default="", help="Sheet name (default: the first sheet)"),
        Setting("header", bool, default=True, help="First row holds the column names"),
    ],
)
def read_xlsx(path: str, sheet: str = "", header: bool = True) -> list[dict[str, Any]]:
    """Read one sheet of an Excel file into a list of rows."""
    source = _open(path)
    try:
        z = zipfile.ZipFile(source)
        workbook = _read_part(z, "xl/workbook.xml")
        rels = {
            r.get("Id"): r.get("Target", "")
            for r in _read_part(z, "xl/_rels/workbook.xml.rels").iter(f"{_PKG_REL}Relationship")
        }
        sheets = [(s.get("name", ""), s.get(f"{_REL}id")) for s in workbook.iter(f"{_NS}sheet")]
        if not sheets:
            raise ValueError(f"{source} has no sheets")
        names = [name for name, _ in sheets]
        if sheet and sheet not in names:
            raise ValueError(f"no sheet {sheet!r} in {source.name} (sheets: {', '.join(names)})")
        rid = dict(sheets)[sheet] if sheet else sheets[0][1]
        target = rels[rid]
        part = (
            target[1:]
            if target.startswith("/")
            else posixpath.normpath(posixpath.join("xl", target))
        )
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            shared = [_text(si) for si in _read_part(z, "xl/sharedStrings.xml").iter(f"{_NS}si")]
        grid: list[dict[str, Any]] = []
        for row in _read_part(z, part).iter(f"{_NS}row"):
            cells = {
                _column_letters(_column_index(c.get("r", "A"))): _cell(c, shared)
                for c in row.iter(f"{_NS}c")
            }
            if any(v is not None for v in cells.values()):
                grid.append(cells)
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        raise ValueError(f"{source} isn't an Excel .xlsx file") from None
    if not header:
        return [{k: v for k, v in row.items() if v is not None} for row in grid]
    if not grid:
        return []
    titles = {col: str(v) if v is not None else col for col, v in grid[0].items()}
    return [{title: row.get(col) for col, title in titles.items()} for row in grid[1:]]


def _xml_text(value: str) -> str:
    return escape(_ILLEGAL.sub("", value))


def _write_cell(ref: str, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{int(value)}</v></c>'
    if isinstance(value, int | float) and value == value and abs(value) != float("inf"):
        return f'<c r="{ref}"><v>{value!r}</v></c>'
    text = value if isinstance(value, str) else str(value)
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_xml_text(text)}</t></is></c>'


@block(
    "write_xlsx",
    settings=[
        Setting("path", str, file=True, help="Excel .xlsx file to write; folders are created"),
        Setting("rows", list, help="Rows to write, e.g. {{ previous_step }}"),
        Setting("sheet", str, default="Sheet1"),
        Setting("columns", list, default=None, help="Column order (default: as they appear)"),
    ],
)
def write_xlsx(
    path: str, rows: list[dict[str, Any]], sheet: str = "Sheet1", columns: list[str] | None = None
) -> dict[str, Any]:
    """Write a list of rows to an Excel file (one sheet, with a header row)."""
    if not sheet or len(sheet) > 31 or _BAD_SHEET.search(sheet):
        raise ValueError(f"sheet name {sheet!r} must be 1 to 31 characters without [ ] : * ? / \\")
    if columns is None:
        columns = []
        for row in rows:
            columns += [k for k in row if k not in columns]
    lines = [
        '<row r="1">'
        + "".join(_write_cell(f"{_column_letters(i)}1", c) for i, c in enumerate(columns, 1))
        + "</row>"
    ]
    for r, row in enumerate(rows, start=2):
        cells = "".join(
            _write_cell(f"{_column_letters(i)}{r}", row.get(c)) for i, c in enumerate(columns, 1)
        )
        lines.append(f'<row r="{r}">{cells}</row>')
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    ctype_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    office = "application/vnd.openxmlformats-officedocument.spreadsheetml"
    head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    quoted = escape(sheet, {'"': "&quot;"})
    parts = {
        "[Content_Types].xml": (
            f'{head}<Types xmlns="{ctype_ns}">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/xl/workbook.xml" ContentType="{office}.sheet.main+xml"/>'
            f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="{office}.worksheet+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            f'{head}<Relationships xmlns="{pkg_ns}">'
            f'<Relationship Id="rId1" Type="{rel_ns}/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": (
            f'{head}<workbook xmlns="{main}" xmlns:r="{rel_ns}">'
            f'<sheets><sheet name="{quoted}" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            f'{head}<Relationships xmlns="{pkg_ns}">'
            f'<Relationship Id="rId1" Type="{rel_ns}/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": (
            f'{head}<worksheet xmlns="{main}"><sheetData>{"".join(lines)}</sheetData></worksheet>'
        ),
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in parts.items():
            z.writestr(name, text)
    return {"path": str(target), "rows": len(rows), "sheet": sheet}
