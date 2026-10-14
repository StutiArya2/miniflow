"""Block drafts: checked with ast only, never imported until a person approves them."""

from __future__ import annotations

import hashlib
import sys
import uuid
from pathlib import Path
from textwrap import dedent

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from miniflow.agent.drafts import (
    DraftError,
    approve,
    check_source,
    discard,
    list_drafts,
    write_draft,
)
from miniflow.blocks import all_blocks, load_plugins
from miniflow.cli import main
from miniflow.ui import DashboardServer
from tests.test_ui import call, get


def uniq() -> str:
    # Plugin modules and blocks are global to the test process, so every test needs new names.
    return "blk_" + uuid.uuid4().hex[:10]


def good(name: str) -> str:
    return dedent(
        f'''
        """A test block."""
        from miniflow.blocks import Setting, block

        LIMIT = 3

        @block("{name}", settings=[Setting("text", str), Setting("times", int, default=2)])
        def run(text, times):
            """Repeat text."""
            return text * min(times, LIMIT)
        '''
    )


def failed(report: dict) -> set[str]:
    return {c["check"] for c in report["checks"] if not c["ok"]}


@pytest.fixture
def flows(tmp_path: Path) -> Path:
    return tmp_path


def test_a_good_block_passes_every_check(flows):
    report = check_source(flows, (name := uniq()), good(name))
    assert report["ok"], report
    assert report["imports"] == ["miniflow.blocks"]


BAD = {
    "parses": "def (:\n",
    "top-level code": 'print("hi")\n',
    "top-level code#class": "class A:\n    pass\n",
    "top-level code#decorator": "import functools\n@functools.cache\ndef f():\n    pass\n",
    "top-level code#default": "def f(x=open('x')):\n    pass\n",
    "top-level code#late import": "def f():\n    pass\nimport os\n",
    "top-level code#setting call": "import os\nfrom miniflow.blocks import Setting, block\n"
    "@block('NAME', settings=[Setting('x', str, default=os.getcwd())])\ndef f(x):\n    pass\n",
    "one block": "def f():\n    pass\n",
    "block name": "from miniflow.blocks import block\n@block('someone_else')\ndef f():\n    pass\n",
    "settings": "from miniflow.blocks import Setting, block\n"
    "@block('NAME', settings=[Setting('missing', str)])\ndef f(x):\n    pass\n",
    "kind": "from miniflow.blocks import block\n@block('NAME', kind='async')\ndef f():\n    pass\n",
    "size": "X = 1\n" * 30_000,
}


@pytest.mark.parametrize("check", sorted(BAD))
def test_each_check_refuses_its_case(flows, check):
    name = uniq()
    report = check_source(flows, name, BAD[check].replace("NAME", name))
    assert not report["ok"]
    assert check.split("#")[0] in failed(report)


@pytest.mark.parametrize("name", ["json", "os", "log", "Bad", "1x", "x" * 41, "has-dash"])
def test_names_that_clash_or_are_malformed_are_refused(flows, name):
    report = check_source(flows, name, good(name))
    assert failed(report) & {"name", "name is free", "doesn't shadow a module"}


def test_a_draft_is_never_imported(flows, tmp_path):
    marker = tmp_path / "ran.txt"
    source = f"open({str(marker)!r}, 'w').close()\n" + good(name := uniq())
    with pytest.raises(DraftError) as info:
        write_draft(flows, name, source)
    assert "top-level code" in failed(info.value.report)
    assert not marker.exists()
    assert not (flows / "blocks" / f"_draft_{name}.py").exists()


def test_a_written_draft_stays_out_of_the_catalog(flows):
    draft = write_draft(flows, (name := uniq()), good(name))
    assert draft["name"] == name
    assert draft["ok"]
    assert draft["sha256"] == hashlib.sha256(good(name).encode()).hexdigest()
    load_plugins(flows / "blocks")
    assert name not in {b.name for b in all_blocks()}
    assert [d["name"] for d in list_drafts(flows)] == [name]


def test_writing_over_a_draft_needs_replace(flows):
    write_draft(flows, (name := uniq()), good(name))
    with pytest.raises(DraftError, match="replace"):
        write_draft(flows, name, good(name))
    changed = good(name).replace("LIMIT = 3", "LIMIT = 4")
    assert write_draft(flows, name, changed, replace=True)["source"] == changed


def test_approve_loads_the_block(flows):
    draft = write_draft(flows, (name := uniq()), good(name))
    assert approve(flows, name, draft["sha256"]) == [name]
    assert (flows / "blocks" / f"{name}.py").exists()
    assert list_drafts(flows) == []
    assert name in {b.name for b in all_blocks()}


def test_approve_refuses_a_draft_that_changed_since_it_was_shown(flows):
    write_draft(flows, (name := uniq()), good(name))
    with pytest.raises(DraftError, match="changed"):
        approve(flows, name, "0" * 64)
    assert list_drafts(flows)[0]["name"] == name


def test_a_block_that_fails_to_load_goes_back_to_being_a_draft(flows):
    name = uniq()
    missing = "not_a_module_" + uuid.uuid4().hex[:8]
    source = good(name).replace("from miniflow.blocks", f"import {missing}\nfrom miniflow.blocks")
    draft = write_draft(flows, name, source)
    with pytest.raises(DraftError, match="failed to load"):
        approve(flows, name, draft["sha256"])
    assert not (flows / "blocks" / f"{name}.py").exists()
    assert [d["name"] for d in list_drafts(flows)] == [name]


def test_discard_removes_the_draft(flows):
    draft = write_draft(flows, (name := uniq()), good(name))
    with pytest.raises(DraftError, match="changed"):
        discard(flows, name, "0" * 64)
    discard(flows, name, draft["sha256"])
    assert list_drafts(flows) == []


def test_unknown_drafts_are_reported(flows):
    with pytest.raises(DraftError, match="no draft"):
        approve(flows, "nothing_here", "0" * 64)


@given(st.text(max_size=400))
@settings(
    max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_any_text_gives_a_report_never_an_exception(flows, source):
    report = check_source(flows, "anything", source)
    assert isinstance(report["ok"], bool)


def test_deeply_nested_source_is_refused_not_crashed(flows):
    report = check_source(flows, "deep", "X = " + "(" * 5000 + "1" + ")" * 5000 + "\n")
    assert not report["ok"]


BYPASSES = {
    "alias Setting": "from os import system as Setting\nfrom miniflow.blocks import block\n"
    "@block('NAME', settings=[Setting('echo PWNED')])\ndef run(**kw):\n    pass\n",
    "alias block": "from os import system as block\n@block('NAME', 'echo PWNED')\n"
    "def run():\n    pass\n",
    "walrus default": "import os\nfrom miniflow.blocks import block\n"
    "def g(x=(Setting := os.system)):\n    pass\n@block('NAME')\ndef run():\n    pass\n",
    "star import": "from os import *\n",
    "relative import": "from . import x\n",
    "draft import": "import _draft_zq_e\n",
    "huge default": "def run(x=9**9**9):\n    pass\n",
    "annotation": "import os\nX: os.environ['HOME'] = 1\n",
    "reserved target": "str = 1\n",
    "reserved function": "def int():\n    pass\n",
}


@pytest.mark.parametrize("case", sorted(BYPASSES))
def test_bypasses_are_refused(flows, case):
    name = uniq()
    report = check_source(flows, name, BYPASSES[case].replace("NAME", name))
    assert not report["ok"]
    assert "top-level code" in failed(report)


def test_plain_annotations_are_allowed(flows):
    name = uniq()
    source = good(name).replace(
        "def run(text, times):",
        "def run(text: str, times: int, rows: list[dict[str, int]] | None = None) -> dict:",
    )
    assert check_source(flows, name, source)["ok"]


def test_a_draft_changed_after_listing_is_not_installed(flows):
    draft = write_draft(flows, (name := uniq()), good(name))
    write_draft(flows, name, good(name).replace("LIMIT = 3", "LIMIT = 9"), replace=True)
    with pytest.raises(DraftError, match="changed"):
        approve(flows, name, draft["sha256"])
    assert not (flows / "blocks" / f"{name}.py").exists()


def test_the_installed_file_is_the_approved_bytes(flows, monkeypatch):
    import miniflow.agent.drafts as drafts

    draft = write_draft(flows, (name := uniq()), good(name))
    swapped = good(name).replace("LIMIT = 3", "LIMIT = 9")
    real = drafts.check_source

    def swap_after_check(*args):
        report = real(*args)
        (flows / "blocks" / f"_draft_{name}.py").write_text(swapped)
        return report

    monkeypatch.setattr(drafts, "check_source", swap_after_check)
    approve(flows, name, draft["sha256"])
    assert (flows / "blocks" / f"{name}.py").read_text() == good(name)
    assert [d["source"] for d in list_drafts(flows)] == [swapped]


def test_approve_rolls_back_when_the_import_exits(flows):
    name = uniq()
    source = good(name) + "\nraise SystemExit(3)\n"
    # the checks would refuse this, so install the draft file directly
    (flows / "blocks").mkdir()
    path = flows / "blocks" / f"_draft_{name}.py"
    path.write_text(source)
    sha = hashlib.sha256(source.encode()).hexdigest()
    import miniflow.agent.drafts as drafts

    original = drafts.check_source
    drafts.check_source = lambda *a: {"ok": True, "checks": [], "imports": []}  # type: ignore[assignment]
    try:
        with pytest.raises(SystemExit):
            approve(flows, name, sha)
    finally:
        drafts.check_source = original
    assert not (flows / "blocks" / f"{name}.py").exists()
    assert path.exists()
    assert name not in sys.modules
    assert name not in {b.name for b in all_blocks()}


def test_a_symlinked_blocks_folder_is_a_failed_check(flows, tmp_path_factory):
    real = tmp_path_factory.mktemp("elsewhere")
    try:
        (flows / "blocks").symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks aren't supported")
    report = check_source(flows, (name := uniq()), good(name))
    assert "blocks folder" in failed(report)


@pytest.mark.parametrize("annotation", ["trap[str]", "trap | int", "trap.attr"])
def test_annotations_cannot_execute_imported_objects(flows, annotation):
    name = uniq()
    source = good(name).replace(
        "from miniflow.blocks", "from somewhere import trap\nfrom miniflow.blocks"
    )
    source = source.replace("def run(text, times):", f"def run(text: {annotation}, times):")
    assert not check_source(flows, name, source)["ok"]


def test_postponed_custom_annotations_are_allowed(flows):
    name = uniq()
    source = good(name).replace(
        "from miniflow.blocks", "from __future__ import annotations\nfrom miniflow.blocks"
    )
    source = source.replace("def run(text, times):", "def run(text: Custom[str] | None, times):")
    assert check_source(flows, name, source)["ok"]


def test_module_getattr_is_refused(flows):
    name = uniq()
    source = good(name) + '\ndef __getattr__(name):\n    print("side effect")\n'
    assert not check_source(flows, name, source)["ok"]


def test_nested_draft_import_is_refused(flows):
    name = uniq()
    source = good(name).replace(
        "return text * min(times, LIMIT)", "import _draft_other\n    return text"
    )
    assert not check_source(flows, name, source)["ok"]


@pytest.mark.parametrize(
    "extra",
    [
        "import miniflow.blocks as m\n@m.block('NAME')\ndef other():\n    pass\n",
        "from miniflow.blocks import Setting as block\n",
        "def helper():\n    global Setting\n    Setting = 1\n",
        "try:\n    pass\nexcept Exception:\n    pass\n",
        "if True:\n    pass\n",
        "class Other:\n    pass\n",
        "match 1:\n    case 1:\n        pass\n",
        "type Alias = int\n",
        "del block\n",
        "from os import system as __getattr__\n",
    ],
)
def test_fresh_bypasses_are_refused(flows, extra):
    name = uniq()
    assert not check_source(flows, name, good(name) + extra.replace("NAME", name))["ok"]


@pytest.mark.parametrize("kind", ["cpu", "async"])
def test_typed_blocks_helpers_and_third_party_imports_pass(flows, kind):
    name = uniq()
    source = good(name).replace("from miniflow.blocks", "import requests\nfrom miniflow.blocks")
    source = source.replace("settings=[", f"kind={kind!r}, settings=[")
    source = source.replace("def run(text, times):", "def run(text: str, times: int):")
    if kind == "async":
        source = source.replace("def run(", "async def run(")
    source += "\ndef helper(value: int = 1) -> int:\n    return value\n"
    assert check_source(flows, name, source)["ok"]


@pytest.mark.parametrize(
    "setting", ["Setting(**{})", "Setting('x', type=block)", "Setting('x', type=Setting)"]
)
def test_settings_require_explicit_arguments_and_builtin_types(flows, setting):
    name = uniq()
    source = (
        "from miniflow.blocks import block, Setting\n"
        f"@block({name!r}, settings=[{setting}])\ndef run(**kw):\n    pass\n"
    )
    assert not check_source(flows, name, source)["ok"]


@pytest.fixture
def dashboard(tmp_path: Path):
    flows = tmp_path / "flows"
    flows.mkdir()
    with DashboardServer(tmp_path / "s.db", port=0, flows=flows, allow_run=True) as server:
        yield server, flows


def test_the_dashboard_lists_and_approves_drafts(dashboard):
    server, flows = dashboard
    draft = write_draft(flows, (name := uniq()), good(name))
    status, drafts = call(server, "GET", "/api/blocks/drafts")
    assert status == 200
    assert [d["sha256"] for d in drafts] == [draft["sha256"]]
    status, result = call(
        server, "POST", "/api/blocks/drafts/approve", {"name": name, "sha256": draft["sha256"]}
    )
    assert status == 200
    assert result["added"] == [name]
    assert {"name": name, "group": "Plugins"}.items() <= next(
        b for b in result["blocks"] if b["name"] == name
    ).items()
    assert call(server, "GET", "/api/blocks/drafts") == (200, [])


def test_the_dashboard_refuses_a_stale_approval(dashboard):
    server, flows = dashboard
    write_draft(flows, (name := uniq()), good(name))
    status, body = call(
        server, "POST", "/api/blocks/drafts/approve", {"name": name, "sha256": "0" * 64}
    )
    assert status == 409
    assert "changed" in body["error"]


def test_the_dashboard_discards_drafts(dashboard):
    server, flows = dashboard
    draft = write_draft(flows, (name := uniq()), good(name))
    assert call(
        server, "POST", "/api/blocks/drafts/discard", {"name": name, "sha256": draft["sha256"]}
    ) == (200, {"discarded": name})
    assert list_drafts(flows) == []


def test_approval_from_another_site_is_refused(dashboard):
    server, flows = dashboard
    draft = write_draft(flows, (name := uniq()), good(name))
    status, _ = call(
        server,
        "POST",
        "/api/blocks/drafts/approve",
        {"name": name, "sha256": draft["sha256"]},
        headers={"Origin": "http://evil.example"},
    )
    assert status == 403
    assert list_drafts(flows)


def test_a_read_only_dashboard_has_no_draft_endpoints(tmp_path):
    flows = tmp_path / "flows"
    flows.mkdir()
    with DashboardServer(tmp_path / "s.db", port=0, flows=flows, allow_run=False) as server:
        assert call(server, "GET", "/api/blocks/drafts")[0] == 404
        assert (
            call(server, "POST", "/api/blocks/drafts/approve", {"name": "x", "sha256": "0"})[0]
            == 404
        )


def test_dashboard_returns_failed_checks_on_approval(dashboard):
    server, flows = dashboard
    name = uniq()
    source = good(name) + '\nprint("unsafe")\n'
    (flows / "blocks").mkdir()
    (flows / "blocks" / f"_draft_{name}.py").write_text(source)
    status, result = call(
        server,
        "POST",
        "/api/blocks/drafts/approve",
        {"name": name, "sha256": hashlib.sha256(source.encode()).hexdigest()},
    )
    assert status == 409
    assert any(not check["ok"] for check in result["checks"])
    assert not (flows / "blocks" / f"{name}.py").exists()


def test_the_editor_ships_the_pending_blocks_panel(dashboard):
    server, _ = dashboard
    status, ctype, body = get(server, "/static/editor-drafts.js")
    assert status == 200
    assert ctype.startswith("text/javascript")
    assert b"function studioDraftsInit" in body
    page = get(server, "/")[2]
    for marker in (
        b'id="studio-drafts"',
        b'id="studio-drafts-dialog"',
        b"/static/editor-drafts.js",
    ):
        assert marker in page
    assert b'"studioDraftsInit"' in get(server, "/static/editor.js")[2]


def test_approve_block_asks_first(flows, monkeypatch, capsys):
    write_draft(flows, (name := uniq()), good(name))
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert main(["approve-block", name, "--flows", str(flows)]) == 1
    out = capsys.readouterr().out
    assert "Repeat text." in out
    assert "not approved" in out
    assert list_drafts(flows)


def test_approve_block_with_yes(flows, monkeypatch, capsys):
    write_draft(flows, (name := uniq()), good(name))
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert main(["approve-block", name, "--flows", str(flows)]) == 0
    assert (flows / "blocks" / f"{name}.py").exists()
    assert f"approved: {name}" in capsys.readouterr().out


def test_approve_block_with_no_such_draft(flows, capsys):
    assert main(["approve-block", "nothing", "--flows", str(flows)]) == 2
    assert "no draft" in capsys.readouterr().err


def test_approve_block_with_closed_stdin_keeps_the_draft(flows, monkeypatch, capsys):
    write_draft(flows, (name := uniq()), good(name))

    def closed(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    assert main(["approve-block", name, "--flows", str(flows)]) == 1
    assert list_drafts(flows)
