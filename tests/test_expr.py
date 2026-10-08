from __future__ import annotations

import pytest

from miniflow.blocks.expr import ConditionError, compile_condition

ROW = {"price": 120, "region": "North", "tags": ["a", "b"], "unit price": 3.5, "note": None}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("price > 100", True),
        ("price >= 120 and region == 'North'", True),
        ("price < 100 or region == 'North'", True),
        ("not price > 100", False),
        ("100 < price <= 200", True),
        ("lower(region) == 'north'", True),
        ("region in ['North', 'South']", True),
        ("'a' in tags", True),
        ("len(tags) == 2", True),
        ("row['unit price'] * 2 == 7", True),
        ("price - 20 == 100", True),
        ("note == None", True),
        ("price % 7 == 1", True),
        ("-price < 0", True),
        ("round(row['unit price']) == 4", True),
    ],
)
def test_conditions(text, expected):
    assert compile_condition(text).evaluate(ROW) is expected


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("__import__('os').system('rm -rf /')", "__import__"),
        ("price.__class__", "can't use"),
        ("open('x')", "open"),
        ("[x for x in tags]", "can't use"),
        ("lambda: 1", "can't use"),
        ("price >", "isn't a valid condition"),
        ("row[0] == 1", "row['field name']"),
    ],
)
def test_unsafe_or_broken_conditions_are_refused(text, message):
    with pytest.raises(ConditionError) as err:
        compile_condition(text)
    assert message in str(err.value)


def test_unknown_field_lists_the_fields():
    cond = compile_condition("prise > 100")
    with pytest.raises(ConditionError) as err:
        cond.evaluate(ROW)
    assert "no field 'prise'" in str(err.value)
    assert "Did you mean 'price'" in str(err.value)


def test_type_mismatch_is_explained():
    with pytest.raises(ConditionError, match="can't compare"):
        compile_condition("region > 5").evaluate(ROW)
