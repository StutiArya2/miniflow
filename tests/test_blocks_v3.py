"""V3 milestone 1: alert, cloud, AI, Excel, date, text and database blocks."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import types
import zipfile
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from miniflow.blocks import get_block
from miniflow.blocks.sigv4 import sign_request
from miniflow.ui.outputs import preview


def call(name: str, **settings):
    return get_block(name).fn(**settings)


# --------------------------------------------------------------- fake server
class Fake:
    """A local HTTP server that records requests and answers with ``respond``."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.respond = lambda request: (200, b"ok", "text/plain")
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                request = {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": self.rfile.read(length) if length else b"",
                }
                fake.requests.append(request)
                status, body, ctype = fake.respond(request)
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def json_reply(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.respond = lambda request: (status, body, "application/json")


@pytest.fixture
def fake():
    server = Fake()
    yield server
    server.server.shutdown()


# ------------------------------------------------------------------- SigV4
def test_sigv4_matches_the_aws_documentation_example():
    """The GET Object example from AWS's Signature V4 documentation, with its published
    signature (also reproduced independently with AWS's own botocore library)."""
    headers = sign_request(
        "GET",
        "https://examplebucket.s3.amazonaws.com/test.txt",
        {"Range": "bytes=0-9"},
        b"",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
        now=datetime(2013, 5, 24, tzinfo=UTC),
    )
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request,"
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date,"
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )
    assert headers["x-amz-date"] == "20130524T000000Z"
    assert headers["x-amz-content-sha256"] == hashlib.sha256(b"").hexdigest()


def test_sigv4_signs_the_payload_and_query():
    a = sign_request(
        "PUT",
        "https://b.s3.amazonaws.com/k?x=1",
        {},
        b"one",
        "AK",
        "SK",
        "eu-west-1",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    b = sign_request(
        "PUT",
        "https://b.s3.amazonaws.com/k?x=1",
        {},
        b"two",
        "AK",
        "SK",
        "eu-west-1",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    c = sign_request(
        "PUT",
        "https://b.s3.amazonaws.com/k?x=2",
        {},
        b"one",
        "AK",
        "SK",
        "eu-west-1",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert len({a["Authorization"], b["Authorization"], c["Authorization"]}) == 3
    assert a["x-amz-content-sha256"] == hashlib.sha256(b"one").hexdigest()


def test_sigv4_query_parameter_order_does_not_matter():
    """AWS sorts query parameters before signing, so ?b=2&a=1 and ?a=1&b=2 are the same request."""
    when = datetime(2026, 1, 1, tzinfo=UTC)
    one = sign_request(
        "GET", "https://b.s3.amazonaws.com/k?b=2&a=1", {}, b"", "AK", "SK", "us-east-1", now=when
    )
    two = sign_request(
        "GET", "https://b.s3.amazonaws.com/k?a=1&b=2", {}, b"", "AK", "SK", "us-east-1", now=when
    )
    three = sign_request(
        "GET", "https://b.s3.amazonaws.com/k?a=1&b=3", {}, b"", "AK", "SK", "us-east-1", now=when
    )
    assert one["Authorization"] == two["Authorization"]
    assert one["Authorization"] != three["Authorization"]


# ------------------------------------------------------------ chat and web
def test_webhook_posts_json_and_returns_the_reply(fake):
    fake.json_reply({"accepted": True})
    out = call("webhook", url=f"{fake.url}/hook", body={"a": 1}, headers={"X-Team": "ops"})
    assert out["status"] == 200
    assert out["json"] == {"accepted": True}
    (req,) = fake.requests
    assert req["method"] == "POST"
    assert req["path"] == "/hook"
    assert json.loads(req["body"]) == {"a": 1}
    assert req["headers"]["content-type"] == "application/json"
    assert req["headers"]["x-team"] == "ops"


def test_webhook_failure_names_the_status(fake):
    fake.respond = lambda request: (500, b"boom", "text/plain")
    with pytest.raises(RuntimeError, match="500"):
        call("webhook", url=fake.url, body={})


def test_slack_message(fake):
    out = call(
        "slack_message",
        webhook_url=f"{fake.url}/services/T/B/X",
        text="3 failures",
        username="miniflow",
    )
    assert out == {"status": 200, "response": "ok"}
    assert json.loads(fake.requests[0]["body"]) == {"text": "3 failures", "username": "miniflow"}


def test_slack_rejection_is_explained(fake):
    fake.respond = lambda request: (404, b"no_service", "text/plain")
    with pytest.raises(RuntimeError, match="no_service"):
        call("slack_message", webhook_url=fake.url, text="x")


def test_teams_message_is_an_adaptive_card(fake):
    call("teams_message", webhook_url=fake.url, text="Report ready", title="Nightly")
    body = json.loads(fake.requests[0]["body"])
    assert body["type"] == "message"
    card = body["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    assert [b["text"] for b in card["body"]] == ["Nightly", "Report ready"]


def test_alert_urls_are_secrets():
    for name, setting in [
        ("slack_message", "webhook_url"),
        ("teams_message", "webhook_url"),
        ("llm_prompt", "api_key"),
    ]:
        assert get_block(name).setting(setting).secret, (name, setting)


# ------------------------------------------------------------------- LLM
def test_llm_prompt_anthropic(fake):
    fake.json_reply(
        {
            "content": [{"type": "text", "text": "Two "}, {"type": "text", "text": "items."}],
            "model": "claude-sonnet-5-5",
            "usage": {"input_tokens": 9, "output_tokens": 3},
        }
    )
    out = call(
        "llm_prompt", prompt="Summarise", api_key="sk-test", base_url=fake.url, system="Be brief"
    )
    assert out["text"] == "Two items."
    assert out["usage"] == {"input_tokens": 9, "output_tokens": 3}
    (req,) = fake.requests
    assert req["path"] == "/v1/messages"
    assert req["headers"]["x-api-key"] == "sk-test"
    assert req["headers"]["anthropic-version"]
    body = json.loads(req["body"])
    assert body["model"] == "claude-sonnet-5-5"
    assert body["system"] == "Be brief"
    assert body["messages"] == [{"role": "user", "content": "Summarise"}]
    assert body["max_tokens"] == 1024


def test_llm_prompt_openai_compatible(fake):
    fake.json_reply(
        {"choices": [{"message": {"content": "Hi"}}], "model": "m", "usage": {"total_tokens": 5}}
    )
    out = call(
        "llm_prompt", prompt="Hello", api_key="k", provider="openai", base_url=fake.url, model="m"
    )
    assert out["text"] == "Hi"
    (req,) = fake.requests
    assert req["path"] == "/v1/chat/completions"
    assert req["headers"]["authorization"] == "Bearer k"
    assert json.loads(req["body"])["messages"][-1] == {"role": "user", "content": "Hello"}


def test_llm_error_shows_the_providers_message_not_the_key(fake):
    # some providers echo the key back in their error text
    fake.json_reply({"error": {"message": "invalid x-api-key sk-secret-123"}}, status=401)
    with pytest.raises(RuntimeError) as err:
        call("llm_prompt", prompt="x", api_key="sk-secret-123", base_url=fake.url)
    assert "401" in str(err.value)
    assert "invalid x-api-key ••••" in str(err.value)
    assert "sk-secret-123" not in str(err.value)


# --------------------------------------------------------------------- S3
S3 = {
    "access_key": "AKIDEXAMPLE",
    "secret_key": "secret",
    "region": "us-east-1",
    "bucket": "reports",
}


def test_s3_put_and_get_round_trip_with_signed_requests(fake, tmp_path: Path):
    store: dict[str, bytes] = {}

    def respond(request):
        auth = request["headers"].get("authorization", "")
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
        assert (
            request["headers"]["x-amz-content-sha256"]
            == hashlib.sha256(request["body"]).hexdigest()
        )
        if request["method"] == "PUT":
            store[request["path"]] = request["body"]
            return 200, b"", "text/plain"
        if request["path"] in store:
            return 200, store[request["path"]], "application/octet-stream"
        return 404, b"<Error><Code>NoSuchKey</Code></Error>", "application/xml"

    fake.respond = respond
    source = tmp_path / "in.csv"
    source.write_bytes(b"a,b\n1,2\n")
    put = call("s3_put", key="2026/in.csv", path=str(source), endpoint=fake.url, **S3)
    assert put == {"bucket": "reports", "key": "2026/in.csv", "bytes": 8}
    assert store["/reports/2026/in.csv"] == b"a,b\n1,2\n"  # path-style with a custom endpoint

    target = tmp_path / "out" / "copy.csv"
    got = call("s3_get", key="2026/in.csv", path=str(target), endpoint=fake.url, **S3)
    assert got == {"bucket": "reports", "key": "2026/in.csv", "bytes": 8, "path": str(target)}
    assert target.read_bytes() == b"a,b\n1,2\n"

    with pytest.raises(RuntimeError, match="NoSuchKey"):
        call("s3_get", key="missing.csv", path=str(tmp_path / "m.csv"), endpoint=fake.url, **S3)


def test_s3_put_can_send_text_instead_of_a_file(fake):
    fake.respond = lambda request: (200, b"", "text/plain")
    out = call("s3_put", key="note.txt", content="hello", endpoint=fake.url, **S3)
    assert out["bytes"] == 5
    assert fake.requests[0]["body"] == b"hello"
    with pytest.raises(ValueError, match="path or content"):
        call("s3_put", key="x", endpoint=fake.url, **S3)


def test_s3_credentials_are_secrets():
    for name in ("s3_get", "s3_put"):
        assert get_block(name).setting("access_key").secret
        assert get_block(name).setting("secret_key").secret


# ------------------------------------------------------------------- Excel
def build_xlsx(path: Path) -> None:
    """A workbook built by hand the way Excel writes it: shared strings, two sheets, a gap."""
    parts = {
        "[Content_Types].xml": '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "xl/workbook.xml": (
            '<?xml version="1.0"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            '<sheet name="Summary" sheetId="1" r:id="rId1"/>'
            '<sheet name="Sales" sheetId="2" r:id="rId2"/>'
            "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="x" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="x" Target="/xl/worksheets/sheet2.xml"/></Relationships>'
        ),
        "xl/sharedStrings.xml": (
            '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<si><t>item</t></si><si><t>qty</t></si><si><t>note</t></si>"
            "<si><t>pen &amp; ink</t></si>"
            "<si><r><t>rich </t></r><r><t>text</t></r></si></sst>"
        ),
        "xl/worksheets/sheet1.xml": (
            '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>only the summary</t></is></c>'
            "</row></sheetData></worksheet>"
        ),
        "xl/worksheets/sheet2.xml": (
            '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
            '<c r="C1" t="s"><v>2</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>3</v></c><c r="B2"><v>4</v></c></row>'
            '<row r="3"><c r="A3" t="s"><v>4</v></c><c r="B3"><v>2.5</v></c>'
            '<c r="C3" t="b"><v>1</v></c></row>'
            "</sheetData></worksheet>"
        ),
    }
    with zipfile.ZipFile(path, "w") as z:
        for name, text in parts.items():
            z.writestr(name, text)


def test_read_xlsx_handles_shared_strings_gaps_and_sheet_choice(tmp_path: Path):
    path = tmp_path / "book.xlsx"
    build_xlsx(path)
    assert call("read_xlsx", path=str(path), sheet="Sales") == [
        {"item": "pen & ink", "qty": 4, "note": None},
        {"item": "rich text", "qty": 2.5, "note": True},
    ]
    assert call("read_xlsx", path=str(path), header=False) == [
        {"A": "only the summary"}
    ]  # first sheet
    with pytest.raises(ValueError, match=r"no sheet 'Nope'.*Summary, Sales"):
        call("read_xlsx", path=str(path), sheet="Nope")


def test_write_xlsx_round_trips_and_escapes(tmp_path: Path):
    rows = [
        {"name": "A & B <co>", "n": 3, "price": 2.5, "ok": True, "gap": None},
        {"name": 'say "hi"', "n": -7, "price": 0.1, "ok": False},
    ]
    path = tmp_path / "out" / "r.xlsx"
    assert call("write_xlsx", path=str(path), rows=rows, sheet="Report") == {
        "path": str(path),
        "rows": 2,
        "sheet": "Report",
    }
    assert zipfile.ZipFile(path).testzip() is None
    back = call("read_xlsx", path=str(path), sheet="Report")
    assert back == [
        {"name": "A & B <co>", "n": 3, "price": 2.5, "ok": True, "gap": None},
        {"name": 'say "hi"', "n": -7, "price": 0.1, "ok": False, "gap": None},
    ]
    assert call("write_xlsx", path=str(path), rows=rows, columns=["n", "name"])["rows"] == 2
    assert list(call("read_xlsx", path=str(path))[0]) == ["n", "name"]


def test_write_docx_is_a_well_formed_word_file(tmp_path: Path):
    path = tmp_path / "out" / "memo.docx"
    text = (
        "Intro line one\nstill intro.\n\n# Results\n"
        "- **East** led\n- *West* <lagged> & more\n\nDone."
    )
    rows = [{"region": "North", "units": 120}, {"region": "a*b", "units": None}]
    result = call("write_docx", path=str(path), title="Weekly memo", text=text, rows=rows)
    assert result == {"path": str(path), "paragraphs": 6, "rows": 2}
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None
        names = set(z.namelist())
        assert {"[Content_Types].xml", "word/document.xml", "word/styles.xml"} <= names
        for name in names:  # every part is well-formed XML
            ET.fromstring(z.read(name))

    blocks = preview(path)["blocks"]  # what the dashboard's viewer shows
    text_of = lambda b: "".join(r["text"] for r in b.get("runs", []))  # noqa: E731
    assert [(b["type"], text_of(b)) for b in blocks[:3]] == [
        ("heading", "Weekly memo"),
        ("paragraph", "Intro line one still intro."),
        ("heading", "Results"),
    ]
    east, west = blocks[3], blocks[4]
    assert east["list"]
    assert [(r["text"], r["bold"]) for r in east["runs"]] == [("East", True), (" led", False)]
    assert west["runs"][0] == {"text": "West", "bold": False, "italic": True, "underline": False}
    assert text_of(west).endswith("<lagged> & more")  # escaped on write, back intact
    assert blocks[-1] == {
        "type": "table",
        "rows": [["region", "units"], ["North", "120"], ["a*b", ""]],
    }


def test_write_docx_with_nothing_still_opens(tmp_path: Path):
    path = tmp_path / "empty.docx"
    assert call("write_docx", path=str(path))["paragraphs"] == 1
    assert preview(path)["blocks"] == [{"type": "paragraph", "list": False, "runs": []}]


def test_xlsx_errors_are_plain(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="no such file"):
        call("read_xlsx", path=str(tmp_path / "nope.xlsx"))
    bad = tmp_path / "bad.xlsx"
    bad.write_text("not a zip", encoding="utf-8")
    with pytest.raises(ValueError, match=r"isn't an Excel \.xlsx file"):
        call("read_xlsx", path=str(bad))


def test_read_xlsx_refuses_xml_with_entity_declarations(tmp_path: Path):
    """A file built to blow up the XML parser (billion laughs) is refused, not parsed."""
    path = tmp_path / "bomb.xlsx"
    build_xlsx(path)
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE sst [<!ENTITY a "aaaaaaaaaa">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>&b;</t></si></sst>'
    )
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp_path / "out.xlsx", "w") as dst:
        for item in src.infolist():
            dst.writestr(
                item.filename,
                bomb if item.filename == "xl/sharedStrings.xml" else src.read(item.filename),
            )
    with pytest.raises(ValueError, match="DTD"):
        call("read_xlsx", path=str(tmp_path / "out.xlsx"), sheet="Sales")


# ------------------------------------------------------------------- dates
def test_date_format():
    assert call("date_format", value="2026-10-09", format="%d/%m/%Y") == "09/10/2026"
    assert call("date_format", value="2026-10-09T14:30:00", format="%H:%M") == "14:30"
    assert call("date_format", value=0, format="%Y-%m-%d") == "1970-01-01"  # epoch seconds, UTC
    assert (
        call("date_format", value="09/10/2026", input_format="%d/%m/%Y", format="%Y-%m-%d")
        == "2026-10-09"
    )


def test_date_add_keeps_the_kind_of_value():
    assert call("date_add", value="2026-10-30", days=3) == "2026-11-02"
    assert (
        call("date_add", value="2026-10-09T10:00:00", hours=5, minutes=30) == "2026-10-09T15:30:00"
    )
    assert call("date_add", value="2026-10-09", weeks=1, days=-2) == "2026-10-14"


def test_date_diff():
    assert call("date_diff", start="2026-10-07", end="2026-10-09") == 2
    assert (
        call("date_diff", start="2026-10-09T00:00:00", end="2026-10-09T03:30:00", unit="hours")
        == 3.5
    )
    assert call("date_diff", start="2026-10-09", end="2026-10-07") == -2


def test_date_blocks_can_fill_a_column_of_rows():
    rows = [{"d": "2026-01-31", "x": 1}, {"d": "2026-02-01", "x": 2}]
    out = call("date_add", rows=rows, field="d", into="due", days=30)
    assert out == [
        {"d": "2026-01-31", "x": 1, "due": "2026-03-02"},
        {"d": "2026-02-01", "x": 2, "due": "2026-03-03"},
    ]
    assert "due" not in rows[0]  # the input isn't changed
    formatted = call("date_format", rows=rows, field="d", format="%b %d")
    assert [r["d"] for r in formatted] == ["Jan 31", "Feb 01"]


def test_date_errors_are_plain():
    with pytest.raises(ValueError, match=r"'31 Feb'.*isn't a date"):
        call("date_format", value="31 Feb")
    with pytest.raises(ValueError, match="give either value, or rows and field"):
        call("date_format")
    with pytest.raises(ValueError, match=r"row 2.*no field 'd'"):
        call("date_add", rows=[{"d": "2026-01-01"}, {"e": 1}], field="d", days=1)


# -------------------------------------------------------------------- text
def test_text_template_builds_a_report_body():
    rows = [{"city": "Pune", "aqi": 80}, {"city": "Delhi", "aqi": 310}]
    out = call(
        "text_template",
        rows=rows,
        header="Air quality:",
        row="- {city}: {aqi}",
        footer="{count} cities",
    )
    assert out == {"text": "Air quality:\n- Pune: 80\n- Delhi: 310\n2 cities", "count": 2}
    assert call("text_template", rows=[], row="{x}", footer="nothing") == {
        "text": "nothing",
        "count": 0,
    }
    assert call("text_template", rows=rows, row="{city}", separator=", ")["text"] == "Pune, Delhi"


def test_text_template_explains_a_missing_field():
    with pytest.raises(ValueError, match=r"row 1.*no field 'cty'.*Did you mean 'city'"):
        call("text_template", rows=[{"city": "Pune"}], row="{cty}")


# --------------------------------------------------------------- databases
class FakeCursor:
    def __init__(self, log, named):
        self.log, self.named = log, named
        self.description = None
        self.rowcount = 2

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.log.append(("execute", query, params))
        if query.lstrip().upper().startswith("SELECT"):
            cols = ["id", "name"]
            self.description = (
                [types.SimpleNamespace(name=c) for c in cols]
                if self.named
                else [(c,) for c in cols]
            )

    def executemany(self, query, seq):
        self.log.append(("executemany", query, list(seq)))

    def fetchall(self):
        return [(1, "pen"), (2, "cup")]


class FakeConnection:
    def __init__(self, log, named):
        self.log, self.named = log, named

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return FakeCursor(self.log, self.named)

    def commit(self):
        self.log.append(("commit",))

    def close(self):
        self.log.append(("close",))


@pytest.mark.parametrize(
    ("block", "module", "named"), [("postgres", "psycopg", True), ("mysql", "pymysql", False)]
)
def test_database_blocks_use_the_driver_when_installed(monkeypatch, block, module, named):
    log: list = []
    fake_module = types.ModuleType(module)

    def connect(**kwargs):
        log.append(("connect", kwargs))
        return FakeConnection(log, named)

    fake_module.connect = connect
    monkeypatch.setitem(sys.modules, module, fake_module)
    settings = {"host": "db.local", "database": "shop", "user": "bot", "password": "pw"}
    rows = call(block, query="SELECT id, name FROM items WHERE id > %s", parameters=[0], **settings)
    assert rows == [{"id": 1, "name": "pen"}, {"id": 2, "name": "cup"}]
    connect_kwargs = log[0][1]
    assert connect_kwargs["host"] == "db.local"
    assert "pw" in connect_kwargs.values()
    assert ("execute", "SELECT id, name FROM items WHERE id > %s", [0]) in log

    log.clear()
    out = call(block, query="DELETE FROM items", **settings)
    assert out == {"changed": 2}
    assert (
        "commit",
    ) in log or block == "postgres"  # psycopg commits when its connection block ends


@pytest.mark.parametrize(
    ("block", "module", "package"),
    [("postgres", "psycopg", "psycopg[binary]"), ("mysql", "pymysql", "pymysql")],
)
def test_database_blocks_explain_a_missing_driver(monkeypatch, block, module, package):
    monkeypatch.setitem(sys.modules, module, None)  # makes "import module" fail
    with pytest.raises(RuntimeError) as err:
        call(block, query="SELECT 1", database="d", user="u", password="p")
    assert f"pip install {package}" in str(err.value)


def test_database_passwords_are_secrets():
    assert get_block("postgres").setting("password").secret
    assert get_block("mysql").setting("password").secret


# ------------------------------------------------------------- whole flows
def test_new_blocks_work_together_in_a_json_flow(fake, tmp_path: Path, monkeypatch):
    """CSV -> filter -> text for a chat message + an Excel report, alert sent to a webhook."""
    from miniflow import Scheduler, StateStore
    from miniflow.flowfile import FlowFileError, load_json_flow

    (tmp_path / "air.csv").write_text(
        "city,aqi,day\nPune,80,2026-10-09\nDelhi,310,2026-10-09\nAgra,250,2026-10-08\n", "utf-8"
    )
    flow = {
        "steps": {
            "readings": {"block": "read_csv", "settings": {"path": "air.csv"}},
            "bad": {
                "block": "filter",
                "settings": {"rows": "{{ readings }}", "where": "aqi > 200"},
            },
            "dated": {
                "block": "date_format",
                "settings": {"rows": "{{ bad }}", "field": "day", "format": "%d %b"},
            },
            "message": {
                "block": "text_template",
                "settings": {
                    "rows": "{{ dated }}",
                    "row": "{city} ({day}): {aqi}",
                    "header": "{count} unhealthy:",
                },
            },
            "report": {
                "block": "write_xlsx",
                "settings": {"path": "out/bad.xlsx", "rows": "{{ dated }}"},
            },
            "alert": {
                "block": "slack_message",
                "settings": {"webhook_url": "{{ env.SLACK_HOOK }}", "text": "{{ message.text }}"},
            },
        }
    }
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps(flow), encoding="utf-8")

    # A webhook URL written into the flow is refused: it must come from the environment.
    leaky = json.loads(json.dumps(flow))
    leaky["steps"]["alert"]["settings"]["webhook_url"] = f"{fake.url}/hook"
    (tmp_path / "leaky.json").write_text(json.dumps(leaky), encoding="utf-8")
    with pytest.raises(FlowFileError, match=r"secrets can't be written in the flow"):
        load_json_flow(tmp_path / "leaky.json")

    monkeypatch.setenv("SLACK_HOOK", f"{fake.url}/hook")
    loaded = load_json_flow(path)
    with StateStore(tmp_path / "s.db") as store:
        result = Scheduler(loaded.dag, store=store, flow="alerts").run()
    assert result.status == "SUCCESS", result.errors
    assert (
        json.loads(fake.requests[0]["body"])["text"]
        == "2 unhealthy:\nDelhi (09 Oct): 310\nAgra (08 Oct): 250"
    )
    assert call("read_xlsx", path=str(tmp_path / "out" / "bad.xlsx")) == [
        {"city": "Delhi", "aqi": 310, "day": "09 Oct"},
        {"city": "Agra", "aqi": 250, "day": "08 Oct"},
    ]


def test_editor_catalog_lists_the_new_blocks_with_secret_fields():
    from miniflow.ui.author import block_catalog

    catalog = {b["name"]: b for b in block_catalog()}
    expected = {
        *("slack_message", "teams_message", "webhook", "llm_prompt", "s3_get", "s3_put"),
        *(
            "read_xlsx",
            "write_xlsx",
            "write_docx",
            "date_format",
            "date_add",
            "date_diff",
            "text_template",
        ),
        *("postgres", "mysql"),
    }
    assert expected <= catalog.keys()
    secrets = {s["name"] for s in catalog["slack_message"]["settings"] if s["secret"]}
    assert secrets == {"webhook_url"}
    assert all("default" not in s for s in catalog["llm_prompt"]["settings"] if s["secret"])
