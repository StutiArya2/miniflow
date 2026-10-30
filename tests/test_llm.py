"""llm_map, the simulated model and downstream-verified tolerant reuse."""

from __future__ import annotations

import csv
import random
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from miniflow.blocks import get_block
from miniflow.blocks.builtin import llm as L
from miniflow.cli import main

EXAMPLES = Path(__file__).parent.parent / "examples"
CHOICES = ["billing", "bug", "feature"]


# ------------------------------------------------------------ simulated model
def test_simulated_classifier_follows_keywords():
    rng = random.Random(0)
    prompt = (
        "Classify:\nThe app crashes with an error.\n\n"
        "Answer with exactly one of: billing, bug, feature."
    )
    answers = [L.simulated_reply(prompt, rng=rng)["text"] for _ in range(200)]
    assert set(answers) <= set(CHOICES)
    assert answers.count("bug") > 180  # 2 % noise


def test_simulated_summary_varies_but_keeps_the_point():
    prompt = "Summarise:\nHi team, I was charged twice for my plan. Thanks."
    replies = [L.simulated_reply(prompt, rng=random.Random(i)) for i in range(50)]
    texts = [r["text"] for r in replies]
    assert len(set(texts)) > 1
    assert sum("charged" in t for t in texts) >= 45
    assert all("hi team" not in t.lower() for t in texts)
    assert replies[0]["usage"]["input_tokens"] > 0
    again = L.simulated_reply(prompt, rng=random.Random(0))
    assert again["text"] == texts[0]


def test_similarity_and_bands():
    assert L.similarity("Hello  World", "hello world") == 1.0
    assert L.similarity("abc", "xyz") < 0.5
    assert L.band_of(0.99, 0.8) == 0
    assert L.band_of(0.96, 0.8) == 1
    assert L.band_of(0.85, 0.8) == 3
    assert L.band_of(0.79, 0.8) is None
    assert L.band_of(0.86, 0.85) == 3
    assert L.change_rate(0, 0) == 0.5
    assert L.change_rate(18, 0) < 0.06


# ------------------------------------------------------------------- llm_map
def counting(fn: Any) -> Any:
    def ask(prompt: str, rng: random.Random) -> dict[str, Any]:
        ask.calls += 1  # type: ignore[attr-defined]
        return {"text": fn(prompt, rng), "usage": {"input_tokens": 3, "output_tokens": 2}}

    ask.calls = 0  # type: ignore[attr-defined]
    return ask


def label_of(prompt: str, _rng: random.Random) -> str:
    text = prompt.lower()
    return "bug" if "crash" in text else "billing" if "refund" in text else "feature"


def summary_of(prompt: str, rng: random.Random) -> str:
    """Reworded each call; keeps 'crash' or 'refund' when the text has it."""
    point = "crash" if "crash" in prompt else "refund" if "refund" in prompt else "idea"
    return (
        f"{rng.choice(['User reports', 'Customer says', 'Ticket:'])} {point} ({rng.random():.3f})"
    )


def test_off_asks_every_row_and_adds_the_field():
    ask = counting(label_of)
    rows = [{"text": "it crashes"}, {"text": "refund please"}]
    out, stats = L.map_rows(rows, "Q: {text}", "label", ask)
    assert [r["label"] for r in out] == ["bug", "billing"]
    assert rows[0] == {"text": "it crashes"}  # input rows untouched
    assert (stats.calls, stats.input_tokens, stats.output_tokens) == (2, 6, 4)
    assert "2 model calls" in stats.summary()


def test_missing_column_is_explained():
    with pytest.raises(KeyError, match="no column 'body'"):
        L.map_rows([{"text": "x"}], "Q: {body}", "a", counting(label_of))


def test_bad_reuse_settings():
    with pytest.raises(ValueError, match="reuse must be"):
        L.map_rows([], "{text}", "a", counting(label_of), reuse="always")
    with pytest.raises(ValueError, match="needs a cache"):
        L.map_rows([], "{text}", "a", counting(label_of), reuse="exact")


def test_choices_normalise_answers():
    ask = counting(lambda _p, _r: "I think this is a Bug report.")
    out, _ = L.map_rows([{"t": "x"}], "{t}", "label", ask, choices=CHOICES)
    assert out[0]["label"] == "bug"


def test_exact_reuse():
    with L.ReuseCache(":memory:") as cache:
        ask = counting(label_of)
        rows = [{"text": "it crashes"}, {"text": "refund please"}]
        L.map_rows(rows, "{text}", "label", ask, reuse="exact", cache=cache)
        out, stats = L.map_rows(
            [*rows, {"text": "new idea"}], "{text}", "label", ask, reuse="exact", cache=cache
        )
    assert (stats.exact, stats.calls, ask.calls) == (2, 1, 3)
    assert out[2]["label"] == "feature"


def edit(text: str, day: int) -> str:
    """A trivial edit: only the trailing account number changes."""
    return f"{text} Account number {1000 + day}."


def run_pipeline(
    cache: L.ReuseCache, rows: list[dict[str, Any]], seed: int, **options: Any
) -> tuple[list[dict[str, Any]], L.MapStats, L.MapStats, Any, Any]:
    summarise, classify = counting(summary_of), counting(label_of)
    summed, s1 = L.map_rows(
        rows, "Summarise: {text}", "summary", summarise, key="id", cache=cache, seed=seed, **options
    )
    out, s2 = L.map_rows(
        summed,
        "Classify: {summary}",
        "label",
        classify,
        choices=CHOICES,
        key="id",
        cache=cache,
        seed=seed,
        **options,
    )
    return out, s1, s2, summarise, classify


BASE = "My order page keeps showing the same message about my account settings."


def tickets(
    day: int, *, meaningful: bool = False, first: str | None = None
) -> list[dict[str, Any]]:
    """Ten tickets; ``meaningful`` swaps the point of the first five, ``first`` replaces it."""
    rows = []
    for i in range(10):
        point = "crash" if i % 2 else "refund"
        if meaningful and i < 5:
            point = "refund" if point == "crash" else "crash"
        if first is not None and i < 5:
            point = first
        rows.append({"id": f"T{i}", "text": edit(f"{BASE} {point} {i}.", day)})
    return rows


def test_tolerant_reuse_is_learned_from_the_downstream_step():
    options = {"reuse": "tolerant", "max_change_rate": 0.3, "verify_rate": 0.0}
    with L.ReuseCache(":memory:") as cache:
        run_pipeline(cache, tickets(0), 0, **options)
        # Day 1: near matches are checked: summaries are asked again, classify judges them.
        out, s1, s2, summarise, _ = run_pipeline(cache, tickets(1), 1, **options)
        assert (s1.checks, s1.tolerant, summarise.calls) == (10, 0, 10)
        bands = [cache.band_stats(s1.step, b) for b in range(len(L.BANDS))]
        assert sum(checks for checks, _ in bands) == 10
        assert sum(changes for _, changes in bands) == 0
        # Day 2: the band is trusted, so summaries are reused and classify hits exactly.
        out, s1, s2, summarise, classify = run_pipeline(cache, tickets(2), 2, **options)
        assert (s1.tolerant, summarise.calls) == (10, 0)
        assert (s2.exact, classify.calls) == (10, 0)
        assert [r["label"] for r in out] == ["billing", "bug"] * 5


def test_meaningful_edits_count_as_changes_in_their_band():
    options = {"reuse": "tolerant", "max_change_rate": 0.4, "verify_rate": 0.0}
    with L.ReuseCache(":memory:") as cache:
        run_pipeline(cache, tickets(0), 0, **options)
        _, s1, *_ = run_pipeline(cache, tickets(1, meaningful=True), 1, **options)
        bands = [cache.band_stats(s1.step, b) for b in range(len(L.BANDS))]
        # Account-number edits: no change downstream. Five flipped tickets: the label changed.
        assert sorted(bands, reverse=True)[:2] == [(5, 5), (5, 0)]
        # Day 2: the first five change point again, a near match in the untrusted band,
        # so they are asked; the other five are reused from the trusted band.
        out, s1, _, summarise, _ = run_pipeline(cache, tickets(2, first="idea"), 2, **options)
    assert (s1.tolerant, summarise.calls) == (5, 5)
    assert [r["label"] for r in out] == ["feature"] * 5 + ["bug", "billing"] * 2 + ["bug"]


def test_classifier_judges_itself():
    options = {"reuse": "tolerant", "max_change_rate": 0.5, "verify_rate": 0.0}
    rows = [{"id": "a", "text": "it crashes on start, version 1"}]
    with L.ReuseCache(":memory:") as cache:
        ask = counting(label_of)
        L.map_rows(rows, "{text}", "label", ask, choices=CHOICES, key="id", cache=cache, **options)
        for version in range(2, 6):
            rows = [{"id": "a", "text": f"it crashes on start, version {version}"}]
            _, stats = L.map_rows(
                rows, "{text}", "label", ask, choices=CHOICES, key="id", cache=cache, **options
            )
        assert stats.tolerant == 1
        assert ask.calls == 4  # first answer + three checks


def test_free_text_without_a_downstream_judge_never_reuses():
    options = {"reuse": "tolerant", "max_change_rate": 0.5, "verify_rate": 0.0}
    with L.ReuseCache(":memory:") as cache:
        ask = counting(summary_of)
        for day in range(4):
            _, stats = L.map_rows(
                tickets(day), "Summarise: {text}", "summary", ask, key="id", cache=cache, **options
            )
    assert stats.tolerant == 0
    assert ask.calls == 40


def test_verify_rate_keeps_checking_a_trusted_band():
    options = {"reuse": "tolerant", "max_change_rate": 0.2, "verify_rate": 1.0}
    with L.ReuseCache(":memory:") as cache:
        for day in range(3):
            _, s1, *_ = run_pipeline(cache, tickets(day), day, **options)
    assert s1.tolerant == 0
    assert s1.checks == 10


def test_without_key_recent_answers_are_compared(tmp_path):
    options = {"reuse": "tolerant", "max_change_rate": 0.5, "verify_rate": 0.0}
    with L.ReuseCache(tmp_path / "c" / "cache.sqlite") as cache:
        ask = counting(label_of)
        for v in range(1, 6):
            _, stats = L.map_rows(
                [{"text": f"it crashes on start, version {v}"}],
                "{text}",
                "label",
                ask,
                choices=CHOICES,
                cache=cache,
                **options,
            )
    assert stats.tolerant == 1


# ------------------------------------------------------------------- block
def test_block_with_simulated_provider(tmp_path):
    spec = get_block("llm_map")
    rows = [{"id": "1", "text": "The app crashes on start."}]
    cache = str(tmp_path / "cache.sqlite")
    kwargs = {"provider": "simulated", "reuse": "exact", "cache_file": cache, "seed": 1}
    out = spec.fn(rows=rows, prompt="Summarise:\n{text}", field="s", **kwargs)
    again = spec.fn(rows=rows, prompt="Summarise:\n{text}", field="s", **kwargs)
    assert out == again
    assert "crashes" in out[0]["s"]
    assert Path(cache).exists()


def test_block_needs_a_key_for_real_providers():
    with pytest.raises(ValueError, match="needs an api_key"):
        get_block("llm_map").fn(rows=[], prompt="{x}", field="y", provider="anthropic")


def test_block_calls_llm_prompt_for_real_providers(monkeypatch):
    seen: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return {"text": "feature", "usage": {"prompt_tokens": 5, "completion_tokens": 1}}

    monkeypatch.setattr("miniflow.blocks.builtin.web.llm_prompt", fake)
    out = get_block("llm_map").fn(
        rows=[{"t": "add dark mode"}],
        prompt="{t}",
        field="label",
        provider="openai",
        api_key="k",
        choices=CHOICES,
    )
    assert out[0]["label"] == "feature"
    assert seen[0]["provider"] == "openai"
    assert "Answer with exactly one of: billing, bug, feature." in seen[0]["prompt"]


# ----------------------------------------------------------------- example
@pytest.fixture
def triage(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    shutil.copytree(EXAMPLES / "data", tmp_path / "data")
    shutil.copy(EXAMPLES / "ticket_triage.json", tmp_path)
    return tmp_path


def test_ticket_triage_example(triage, caplog):
    flow, db = str(triage / "ticket_triage.json"), str(triage / "t.db")
    caplog.set_level("INFO", logger="miniflow.llm")
    assert main(["run", flow, "--db", db, "-q"]) == 0
    with (triage / "out" / "triaged_tickets.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 30
    assert {r["label"] for r in rows} <= set(CHOICES)
    assert all(r["summary"] for r in rows)
    caplog.clear()
    assert main(["run", flow, "--db", db, "-q"]) == 0
    # Unchanged tickets: everything is reused, apart from a few re-asks that measure
    # how much the model varies on its own (the noise floor).
    calls = [int(n) for n in re.findall(r"30 rows: (\d+) model calls", caplog.text)]
    assert len(calls) == 2
    assert all(n <= 8 for n in calls)
    assert (triage / ".miniflow" / "llm_cache.sqlite").exists()
