import pytest

from miniflow.ui.templates import build_template


def test_starters_have_distinct_steps_and_local_sample_file():
    hello, no_files = build_template("hello_world", "hello")
    sales, files = build_template("sales_report", "shop")
    assert no_files == {}
    assert len(hello["steps"]) == 2
    assert len(sales["steps"]) == 6
    assert "data/shop_sales.csv" in files
    assert sales["steps"]["save"]["settings"]["path"] == "out/shop_report.csv"


def test_unknown_starter_is_rejected():
    with pytest.raises(KeyError):
        build_template("missing", "flow")
