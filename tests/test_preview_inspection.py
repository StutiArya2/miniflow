from miniflow.preview import inspect_value, summarize


def test_structured_sample_limits_rows_and_redacts_secrets(monkeypatch):
    monkeypatch.setenv("MINIFLOW_API_KEY", "sample-secret-value")
    value = [
        {"name": str(i), "password": "hidden", "note": "sample-secret-value"} for i in range(8)
    ]
    sample, count = inspect_value(value)
    assert count == 8
    assert len(sample) == 6
    assert sample[0] == {"name": "0", "password": "••••", "note": "••••"}
    assert sample[-1] == "… 3 more items"
    assert "sample-secret-value" not in (summarize(value) or "")


def test_nonfinite_numbers_are_json_safe():
    sample, count = inspect_value({"measurements": [float("nan"), float("inf")]})
    assert count == 1
    assert sample == {"measurements": ["nan", "inf"]}


def test_summary_hides_secret_named_fields():
    assert "hidden" not in (summarize({"password": "hidden", "value": 2}) or "")
