"""Files a run wrote: finding them, previewing them and serving them to the dashboard."""

from __future__ import annotations

import http.client
import json
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from miniflow import DAG, StateStore
from miniflow.blocks.builtin.data import write_csv
from miniflow.blocks.builtin.office import read_sheets, write_xlsx
from miniflow.ui import DashboardServer, outputs
from miniflow.ui.outputs import MAX_ROWS, OutputError, find_outputs, output_path, preview
from tests.conftest import make_tasks

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx(path: Path, body: str) -> Path:
    head = '<?xml version="1.0" encoding="UTF-8"?>'
    xml = f'{head}<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)
    return path


def run_with(db: Path, results: dict[str, object]) -> str:
    """A finished run whose steps returned ``results``."""
    with StateStore(db) as store:
        run_id = store.create_run("demo", DAG.from_tasks(make_tasks({n: [] for n in results})))
        for name, value in results.items():
            store.save_result(run_id, name, value)
        store.finish_run(run_id, "SUCCESS")
    return run_id


# ------------------------------------------------------------------ finding
def test_finds_files_steps_reported(tmp_path: Path):
    report = write_csv(str(tmp_path / "report.csv"), [{"a": 1}])
    gone = {"path": str(tmp_path / "gone.xlsx"), "rows": 0}
    text = tmp_path / "notes.md"
    text.write_text("# Hi", encoding="utf-8")
    run_id = run_with(
        tmp_path / "s.db",
        {"save": report, "lost": gone, "notes": str(text), "count": 3, "words": "plain text"},
    )
    with StateStore(tmp_path / "s.db") as store:
        found = {o["task"]: o for o in find_outputs(store, run_id)}
        assert set(found) == {"save", "lost", "notes"}
        assert found["save"]["kind"] == "table"
        assert found["save"]["exists"]
        assert found["save"]["size"] == (tmp_path / "report.csv").stat().st_size
        assert not found["lost"]["exists"]
        assert found["notes"]["kind"] == "markdown"
        assert output_path(store, run_id, "save") == tmp_path / "report.csv"
        with pytest.raises(OutputError):
            output_path(store, run_id, "count")  # returned no file
        with pytest.raises(OutputError):
            output_path(store, run_id, "lost")  # file no longer there


def test_long_paths_come_from_the_stored_result(tmp_path: Path):
    folder = tmp_path / ("x" * 120) / ("y" * 120)
    result = write_csv(str(folder / "out.csv"), [{"a": 1}])
    run_id = run_with(tmp_path / "s.db", {"save": result})
    with StateStore(tmp_path / "s.db") as store:
        assert output_path(store, run_id, "save") == folder / "out.csv"


# ----------------------------------------------------------------- previews
def test_csv_preview_is_a_sheet_with_the_header_row(tmp_path: Path):
    path = tmp_path / "rows.csv"
    path.write_text("name;qty\nnuts;3\nbolts;12\n", encoding="utf-8")
    sheet = preview(path)["sheets"][0]
    assert sheet["rows"] == [["name", "qty"], ["nuts", "3"], ["bolts", "12"]]
    assert (sheet["total"], sheet["columns"]) == (3, 2)


def test_big_csv_preview_is_capped_but_counted(tmp_path: Path):
    path = tmp_path / "big.csv"
    path.write_text("n\n" + "".join(f"{i}\n" for i in range(MAX_ROWS + 50)), encoding="utf-8")
    sheet = preview(path)["sheets"][0]
    assert len(sheet["rows"]) == MAX_ROWS
    assert sheet["total"] == MAX_ROWS + 51


def test_xlsx_preview_keeps_cell_positions(tmp_path: Path):
    path = tmp_path / "book.xlsx"
    write_xlsx(str(path), [{"a": 1, "b": None, "c": "x"}, {"a": 2.5, "c": True}], sheet="Data")
    data = preview(path)
    assert data["kind"] == "table"
    assert data["sheets"][0]["name"] == "Data"
    assert data["sheets"][0]["rows"] == [["a", "b", "c"], [1, None, "x"], [2.5, None, True]]


@settings(max_examples=30, deadline=None)
@given(
    st.lists(
        st.lists(
            st.one_of(st.none(), st.integers(-1000, 1000), st.text("abc xyz", max_size=5)),
            min_size=1,
            max_size=4,
        ),
        min_size=1,
        max_size=6,
    )
)
def test_xlsx_round_trips_through_read_sheets(tmp_path_factory, grid):
    """Whatever write_xlsx writes, read_sheets gives back in the same cells."""
    width = max(len(r) for r in grid)
    columns = [f"c{i}" for i in range(width)]
    rows = [{c: (r[i] if i < len(r) else None) for i, c in enumerate(columns)} for r in grid]
    path = tmp_path_factory.mktemp("x") / "t.xlsx"
    write_xlsx(str(path), rows, columns=columns)
    [(_, back)] = read_sheets(str(path))
    assert back[0] == columns
    for want, got in zip(rows, back[1:], strict=True):
        assert got + [None] * (width - len(got)) == [want[c] for c in columns]


def test_docx_preview_has_headings_lists_runs_and_tables(tmp_path: Path):
    path = docx(
        tmp_path / "memo.docx",
        '<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr><w:r><w:t>Weekly memo</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Sales</w:t></w:r></w:p>'
        '<w:p><w:r><w:t xml:space="preserve">Up </w:t></w:r>'
        "<w:r><w:rPr><w:b/></w:rPr><w:t>12%</w:t></w:r><w:r><w:t> this week.</w:t></w:r></w:p>"
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/></w:numPr></w:pPr>'
        "<w:r><w:t>North</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Region</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Total</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>North</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>40</w:t></w:r></w:p></w:tc></w:tr></w:tbl>",
    )
    blocks = preview(path)["blocks"]
    assert blocks[0] == {
        "type": "heading",
        "level": 1,
        "runs": [{"text": "Weekly memo", "bold": False, "italic": False, "underline": False}],
    }
    assert (blocks[1]["type"], blocks[1]["level"]) == ("heading", 2)
    assert [(r["text"], r["bold"]) for r in blocks[2]["runs"]] == [
        ("Up ", False),
        ("12%", True),
        (" this week.", False),
    ]
    assert blocks[3]["list"] is True
    assert blocks[4] == {"type": "table", "rows": [["Region", "Total"], ["North", "40"]]}


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le"])
def test_docx_with_a_dtd_is_refused(tmp_path: Path, encoding: str):
    xml = '<!DOCTYPE x [<!ENTITY a "aaaa">]><w:document xmlns:w="' + W + '"/>'
    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml.encode(encoding))
    with pytest.raises(OutputError, match="DTD"):
        preview(path)


def test_not_really_a_docx(tmp_path: Path):
    path = tmp_path / "fake.docx"
    path.write_text("hello", encoding="utf-8")
    with pytest.raises(OutputError):
        preview(path)


def test_text_previews(tmp_path: Path):
    (tmp_path / "a.json").write_text('{"b":[1,2]}', encoding="utf-8")
    assert preview(tmp_path / "a.json")["text"] == '{\n  "b": [\n    1,\n    2\n  ]\n}'
    (tmp_path / "a.md").write_text("# Title", encoding="utf-8")
    assert preview(tmp_path / "a.md") == {"kind": "markdown", "text": "# Title", "truncated": False}
    big = tmp_path / "big.log"
    big.write_bytes(b"x" * (outputs.MAX_TEXT + 10))
    assert preview(big)["truncated"]
    (tmp_path / "pic.png").write_bytes(b"\x89PNG")
    assert preview(tmp_path / "pic.png") == {"kind": "image"}


# --------------------------------------------------------------------- HTTP
def call(server, method, path, body=None, **headers):
    url = urlsplit(server.url)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
    data = None if body is None else json.dumps(body).encode()
    try:
        conn.request(method, path, data, headers={"Content-Type": "application/json", **headers})
        res = conn.getresponse()
        return res.status, dict(res.getheaders()), res.read()
    finally:
        conn.close()


def test_output_routes(tmp_path: Path, monkeypatch):
    opened: list[Path] = []
    monkeypatch.setattr("miniflow.ui.server.open_file", opened.append)
    page = tmp_path / "page.svg"
    page.write_text("<svg xmlns='http://www.w3.org/2000/svg'><script>x()</script></svg>")
    run_id = run_with(
        tmp_path / "s.db",
        {"save": write_csv(str(tmp_path / "out.csv"), [{"a": 1}]), "pic": str(page), "n": 1},
    )
    with DashboardServer(tmp_path / "s.db", port=0) as server:
        base = f"/api/runs/{run_id}/outputs"
        status, _, body = call(server, "GET", base)
        assert status == 200
        assert [o["task"] for o in json.loads(body)] == ["save", "pic"]

        status, _, body = call(server, "GET", f"{base}/save/preview")
        assert status == 200
        assert json.loads(body)["sheets"][0]["rows"] == [["a"], ["1"]]

        status, headers, body = call(server, "GET", f"{base}/save/file?download=1")
        assert (status, body) == (200, b"a\r\n1\r\n")
        assert headers["Content-Disposition"].startswith("attachment;")
        status, headers, _ = call(server, "GET", f"{base}/pic/file")
        assert headers["Content-Type"] == "image/svg+xml"
        assert headers["Content-Security-Policy"] == "sandbox"  # its script can't run here

        assert call(server, "GET", f"{base}/n/preview")[0] == 404  # no file
        assert call(server, "GET", f"{base}/nope/file")[0] == 404
        assert call(server, "GET", base, Origin="http://evil.test")[0] == 403
        assert call(server, "GET", base, Host="evil.test")[0] == 403

        status, _, body = call(server, "POST", f"{base}/save/open", {})
        assert (status, json.loads(body)) == (200, {"opened": "out.csv"})
        assert opened == [tmp_path / "out.csv"]
        assert call(server, "POST", f"{base}/save/open", {}, Origin="http://evil.test")[0] == 403
        assert call(server, "POST", f"{base}/n/open", {})[0] == 404
        assert opened == [tmp_path / "out.csv"]
