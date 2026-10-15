"""Excel and Word files: read_xlsx, write_xlsx and write_docx (standard library only).

``read_sheets(path) -> list[tuple[str, list[list[Any]]]]`` (not a block) returns every
sheet as rows of cells in column order, for the dashboard's file preview.

An ``.xlsx`` file is a zip of XML files, so ``zipfile`` and ``xml.etree`` are enough for
plain data sheets: text, numbers, true/false and empty cells. Formulas are read as the
value Excel last calculated; styles, charts and images are not read or written.

``write_docx`` writes a Word document the same way: a title, text with simple Markdown
(``#`` headings, ``-`` bullets, ``**bold**``, ``*italic*``) and an optional table of rows.
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
    if any(
        m in data
        for w in ("<!DOCTYPE", "<!ENTITY")
        for m in (w.encode(), w.encode("utf-16-le"), w.encode("utf-16-be"))
    ):
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


def _sheet_parts(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    """(sheet name, zip part) for every sheet, in workbook order."""
    workbook = _read_part(z, "xl/workbook.xml")
    rels = {
        r.get("Id"): r.get("Target", "")
        for r in _read_part(z, "xl/_rels/workbook.xml.rels").iter(f"{_PKG_REL}Relationship")
    }
    out = []
    for s in workbook.iter(f"{_NS}sheet"):
        target = rels[s.get(f"{_REL}id")]
        part = (
            target[1:]
            if target.startswith("/")
            else posixpath.normpath(posixpath.join("xl", target))
        )
        out.append((s.get("name", ""), part))
    return out


def read_sheets(path: str) -> list[tuple[str, list[list[Any]]]]:
    """Every sheet of an Excel file as rows of cells, blank rows and columns kept."""
    source = _open(path)
    try:
        with zipfile.ZipFile(source) as z:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in z.namelist():
                shared = [
                    _text(si) for si in _read_part(z, "xl/sharedStrings.xml").iter(f"{_NS}si")
                ]
            sheets = []
            for name, part in _sheet_parts(z):
                grid: list[list[Any]] = []
                for row in _read_part(z, part).iter(f"{_NS}row"):
                    index = int(row.get("r") or len(grid) + 1)
                    while len(grid) < index - 1:
                        grid.append([])
                    cells: list[Any] = []
                    for c in row.iter(f"{_NS}c"):
                        col = _column_index(c.get("r", "A")) if c.get("r") else len(cells) + 1
                        cells += [None] * (col - 1 - len(cells))
                        cells.append(_cell(c, shared))
                    grid.append(cells)
                sheets.append((name, grid))
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        raise ValueError(f"{source} isn't an Excel .xlsx file") from None
    return sheets


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


# ------------------------------------------------------------------- Word files
_WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_INLINE = re.compile(r"(\*\*[^*\n]+\*\*|\*[^*\n]+\*)")
_DOCX_STYLES = (
    "<w:docDefaults><w:rPrDefault><w:rPr>"
    '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
    '<w:sz w:val="22"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>'
    '<w:spacing w:after="120" w:line="276"'
    ' w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
    "</w:style>"
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>'
    '<w:basedOn w:val="Normal"/>'
    '<w:pPr><w:spacing w:after="240"/></w:pPr><w:rPr><w:sz w:val="52"/>'
    '<w:color w:val="1F3864"/></w:rPr>'
    "</w:style>"
    + "".join(
        f'<w:style w:type="paragraph" w:styleId="Heading{n}"><w:name w:val="heading {n}"/>'
        f'<w:basedOn w:val="Normal"/><w:pPr><w:keepNext/>'
        f'<w:spacing w:before="{280 - 40 * n}" w:after="80"/>'
        f'<w:outlineLvl w:val="{n - 1}"/></w:pPr><w:rPr><w:b/><w:sz w:val="{36 - 4 * n}"/>'
        '<w:color w:val="1F3864"/></w:rPr></w:style>'
        for n in (1, 2, 3)
    )
    + '<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/>'
    '<w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="60"/><w:ind w:left="720"/></w:pPr>'
    "</w:style>"
    '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:tblPr>'
    "<w:tblBorders>"
    + "".join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="BFBFBF"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    + '</w:tblBorders><w:tblCellMar><w:left w:w="100" w:type="dxa"/>'
    '<w:right w:w="100" w:type="dxa"/>'
    "</w:tblCellMar></w:tblPr></w:style>"
)
_DOCX_BULLETS = (
    '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
    '<w:numFmt w:val="bullet"/>'
    '<w:lvlText w:val="•"/><w:lvlJc w:val="left"/><w:pPr>'
    '<w:ind w:left="720" w:hanging="360"/></w:pPr>'
    '</w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
)


def _docx_runs(text: str, bold: bool = False) -> str:
    """``**bold**`` and ``*italic*`` inside a line become formatted runs."""
    out = []
    for part in _INLINE.split(text):
        if not part:
            continue
        strong, italic = bold, False
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            part, strong = part[2:-2], True
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            part, italic = part[1:-1], True
        props = ("<w:b/>" if strong else "") + ("<w:i/>" if italic else "")
        out.append(
            f"<w:r>{f'<w:rPr>{props}</w:rPr>' if props else ''}"
            f'<w:t xml:space="preserve">{_xml_text(part)}</w:t></w:r>'
        )
    return "".join(out)


def _docx_paragraph(text: str, style: str = "", bullet: bool = False, bold: bool = False) -> str:
    props = (f'<w:pStyle w:val="{style}"/>' if style else "") + (
        '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>' if bullet else ""
    )
    return f"<w:p>{f'<w:pPr>{props}</w:pPr>' if props else ''}{_docx_runs(text, bold)}</w:p>"


def _docx_body(text: str) -> list[str]:
    """Simple Markdown: ``#`` headings, ``-``/``*`` bullets, blank lines between paragraphs."""
    out: list[str] = []
    lines: list[str] = []

    def flush() -> None:
        if lines:
            out.append(_docx_paragraph(" ".join(lines)))
            lines.clear()

    for raw in text.splitlines():
        line = raw.strip()
        heading = re.match(r"(#{1,3})\s+(.*)", line)
        bullet = re.match(r"[-*•]\s+(.*)", line)
        if not line:
            flush()
        elif heading:
            flush()
            out.append(_docx_paragraph(heading.group(2), f"Heading{len(heading.group(1))}"))
        elif bullet:
            flush()
            out.append(_docx_paragraph(bullet.group(1), "ListParagraph", bullet=True))
        else:
            lines.append(line)
    flush()
    return out


def _docx_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    def cell(value: Any, header: bool = False) -> str:
        text = "" if value is None else value if isinstance(value, str) else str(value)
        paragraph = f"<w:p>{_docx_runs(text, bold=header) if header else _docx_plain(text)}</w:p>"
        return f'<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr>{paragraph}</w:tc>'

    head = (
        "<w:tr><w:trPr><w:tblHeader/></w:trPr>"
        + "".join(cell(c, True) for c in columns)
        + "</w:tr>"
    )
    body = "".join(
        "<w:tr>" + "".join(cell(row.get(c)) for c in columns) + "</w:tr>" for row in rows
    )
    return (
        '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="5000" w:type="pct"/></w:tblPr>'
        f"<w:tblGrid>{'<w:gridCol/>' * len(columns)}</w:tblGrid>{head}{body}</w:tbl>"
    )


def _docx_plain(text: str) -> str:
    """A cell's text as one run, with no Markdown, so values like a*b stay as they are."""
    return f'<w:r><w:t xml:space="preserve">{_xml_text(text)}</w:t></w:r>'


@block(
    "write_docx",
    settings=[
        Setting("path", str, file=True, help="Word .docx file to write; folders are created"),
        Setting("title", str, default="", help="Large title at the top (optional)"),
        Setting(
            "text",
            str,
            default="",
            help="# Heading, - bullet, **bold**, *italic*; a blank line starts a new paragraph",
        ),
        Setting(
            "rows", list, default=None, help="Rows shown as a table after the text, e.g. {{ step }}"
        ),
        Setting("columns", list, default=None, help="Table column order (default: as they appear)"),
    ],
)
def write_docx(
    path: str,
    title: str = "",
    text: str = "",
    rows: list[dict[str, Any]] | None = None,
    columns: list[str] | None = None,
) -> dict[str, Any]:
    """Write a Word document: a title, formatted text and an optional table of rows."""
    body = ([_docx_paragraph(title, "Title")] if title else []) + _docx_body(text or "")
    rows = [r for r in rows or [] if isinstance(r, dict)]
    if rows:
        if columns is None:
            columns = []
            for row in rows:
                columns += [k for k in row if k not in columns]
        body.append(_docx_table(rows, [str(c) for c in columns]))
    if not body:
        body.append("<w:p/>")
    office = "application/vnd.openxmlformats-officedocument.wordprocessingml"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    section = (
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440"'
        ' w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>'
    )
    parts = {
        "[Content_Types].xml": (
            f'{head}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/word/document.xml" ContentType="{office}.document.main+xml"/>'
            f'<Override PartName="/word/styles.xml" ContentType="{office}.styles+xml"/>'
            f'<Override PartName="/word/numbering.xml" ContentType="{office}.numbering+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            f'{head}<Relationships xmlns="{pkg_ns}">'
            f'<Relationship Id="rId1" Type="{rel_ns}/officeDocument" Target="word/document.xml"/>'
            "</Relationships>"
        ),
        "word/_rels/document.xml.rels": (
            f'{head}<Relationships xmlns="{pkg_ns}">'
            f'<Relationship Id="rId1" Type="{rel_ns}/styles" Target="styles.xml"/>'
            f'<Relationship Id="rId2" Type="{rel_ns}/numbering" Target="numbering.xml"/>'
            "</Relationships>"
        ),
        "word/document.xml": (
            f'{head}<w:document xmlns:w="{_WORD}"><w:body>{"".join(body)}{section}</w:body>'
            f"</w:document>"
        ),
        "word/styles.xml": f'{head}<w:styles xmlns:w="{_WORD}">{_DOCX_STYLES}</w:styles>',
        "word/numbering.xml": f'{head}<w:numbering xmlns:w="{_WORD}">{_DOCX_BULLETS}</w:numbering>',
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for name, part in parts.items():
            z.writestr(name, part)
    paragraphs = sum(1 for p in body if p.startswith("<w:p"))
    return {"path": str(target), "paragraphs": paragraphs, "rows": len(rows)}
