"""Block drafts: checked with ast only, never imported until a person approves them."""

from __future__ import annotations

import hashlib
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
