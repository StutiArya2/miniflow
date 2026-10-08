from __future__ import annotations

import pytest

from miniflow.blocks.template import TemplateError, references, render

CTX = {
    "get": {"json": {"items": [{"name": "pen", "price": 120}, {"name": "cup", "price": 40}]}},
    "rows": [{"a": 1}, {"a": 2}, {"a": 3}],
    "params": {"min": 100, "who": "ops@example.com"},
    "env": {"TOKEN": "s3cret"},
}


def test_whole_placeholder_keeps_the_value():
    assert render("{{ get.json.items }}", CTX) == CTX["get"]["json"]["items"]
    assert render("{{params.min}}", CTX) == 100


def test_mixed_text_inserts_the_value_as_text():
    assert render("over {{ params.min }} for {{ params.who }}", CTX) == (
        "over 100 for ops@example.com"
    )


def test_list_positions_and_nested_keys():
    assert render("{{ get.json.items.1.name }}", CTX) == "cup"


def test_count_first_and_last_on_lists():
    assert render("{{ rows.count }}", CTX) == 3
    assert render("{{ rows.first.a }}", CTX) == 1
    assert render("{{ rows.last.a }}", CTX) == 3


def test_renders_inside_lists_and_dicts():
    value = {"auth": "Bearer {{ env.TOKEN }}", "ids": ["{{ rows.count }}", 7]}
    assert render(value, CTX) == {"auth": "Bearer s3cret", "ids": [3, 7]}


def test_text_without_placeholders_is_unchanged():
    assert render("plain {text}", CTX) == "plain {text}"
    assert render(5, CTX) == 5


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{{ nope }}", "'nope'"),
        ("{{ get.json.itemz }}", "no 'itemz' in get.json (it has: items)"),
        ("{{ get.json.items.9 }}", "get.json.items has 2 items, so there is no item 9"),
        ("{{ params.min.x }}", "params.min is a number, so it has no 'x'"),
    ],
)
def test_missing_values_are_explained(text, message):
    with pytest.raises(TemplateError) as err:
        render(text, CTX)
    assert message in str(err.value)


def test_references_lists_the_roots_and_paths():
    value = {"a": "{{ get.json.items }} and {{params.min}}", "b": ["{{ rows.count }}", "x"]}
    assert references(value) == [("get", "json.items"), ("params", "min"), ("rows", "count")]


def test_malformed_placeholders_are_reported():
    with pytest.raises(TemplateError, match="placeholder"):
        references("{{ get..json }}")
    with pytest.raises(TemplateError, match="placeholder"):
        references("{{ }}")
