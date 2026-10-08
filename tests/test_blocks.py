from __future__ import annotations

import math
import sys
from textwrap import dedent

import pytest

from miniflow.blocks import (
    REQUIRED,
    BlockError,
    Setting,
    all_blocks,
    block,
    get_block,
    load_plugins,
)

BUILTIN = {
    "http_request", "read_csv", "write_csv", "read_json", "write_json",
    "filter", "select", "aggregate", "sort",
    "shell", "sql",
    "email", "log", "wait",
}  # fmt: skip


@pytest.mark.parametrize(
    ("setting", "value", "problem"),
    [
        (Setting("n", int), 3, None),
        (Setting("n", int), True, "should be a whole number, not a true/false value"),
        (Setting("n", int), "3", "should be a whole number, not a piece of text"),
        (Setting("x", float), 2, None),
        (Setting("x", float), math.nan, None),
        (Setting("on", bool), 1, "should be true or false, not a number"),
        (Setting("rows", list), {"a": 1}, "should be a list, not an object"),
        (Setting("h", dict), [], "should be an object, not a list"),
        (Setting("to", (str, list)), ["a@b.c"], None),
        (Setting("to", (str, list)), 5, "should be text or a list, not a number"),
        (
            Setting("m", str, choices=("GET", "POST")),
            "PUT",
            "should be one of GET, POST, not 'PUT'",
        ),
        (Setting("body", object, default=None), None, None),
        (Setting("path", str), None, "should be text, not an empty value"),
    ],
)
def test_setting_check(setting, value, problem):
    assert setting.check(value) == problem


def test_setting_required_by_default():
    assert Setting("url", str).default is REQUIRED
    assert Setting("url", str).required
    assert not Setting("x", int, default=0).required


def test_block_decorator_registers_and_describes(tmp_path):
    @block("double_it_test", settings=[Setting("n", int, help="a number")], kind="cpu")
    def double_it(n):
        """Double a number.

        More detail that isn't part of the summary."""
        return 2 * n

    spec = get_block("double_it_test")
    assert spec.fn is double_it
    assert double_it(n=4) == 8  # the function itself is unchanged
    assert spec.kind == "cpu"
    assert spec.description == "Double a number."
    assert spec.ref == f"{__name__}:test_block_decorator_registers_and_describes.<locals>.double_it"
    assert [s.name for s in spec.settings] == ["n"]


def test_block_names_must_be_unique():
    with pytest.raises(BlockError, match="already"):

        @block("log")
        def another_log():
            return None


def test_block_settings_must_match_the_function():
    with pytest.raises(BlockError, match="'b'"):

        @block("mismatch_test", settings=[Setting("a", int), Setting("b", int)])
        def f(a):
            return a


def test_builtin_blocks_are_all_there():
    assert {b.name for b in all_blocks()} >= BUILTIN
    assert get_block("http_request").kind == "io"


def test_unknown_block_suggests_a_name():
    with pytest.raises(BlockError, match="Did you mean 'read_csv'"):
        get_block("read_cvs")


def test_plugins_load_from_a_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    (tmp_path / "weather_plugin.py").write_text(
        dedent(
            """
            from miniflow.blocks import Setting, block

            @block("weather_test", settings=[Setting("city", str)])
            def weather(city):
                \"\"\"Pretend weather for a city.\"\"\"
                return {"city": city, "temp": 21.5}
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "_private.py").write_text("raise RuntimeError('not loaded')\n", encoding="utf-8")
    names = load_plugins(tmp_path)
    assert names == ["weather_test"]
    spec = get_block("weather_test")
    assert spec.fn(city="Vellore") == {"city": "Vellore", "temp": 21.5}
    assert spec.ref == "weather_plugin:weather"
    assert load_plugins(tmp_path) == []  # already loaded


def test_broken_plugin_names_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    (tmp_path / "broken_plugin_x.py").write_text("this is not python\n", encoding="utf-8")
    with pytest.raises(BlockError, match=r"broken_plugin_x\.py"):
        load_plugins(tmp_path)
