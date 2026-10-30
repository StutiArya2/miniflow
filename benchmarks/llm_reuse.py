"""Experiment 8: reusing LLM answers across daily runs of a ticket-triage flow.

Every day a :class:`~benchmarks.tickets.TicketStream` changes: new tickets arrive,
some are edited in ways that don't matter and some in ways that do (the actual
problem changes). Each day the flow summarises every ticket and labels the summary,
both with ``llm_map`` on the offline simulated model, under one reuse policy:

* *no reuse*: every row asks the model;
* *exact*: only identical inputs reuse an answer;
* *fixed threshold* (0.8 or 0.95): any near match at least that similar reuses the
  earlier answer, the usual semantic-cache rule (after one check per band);
* *learned, self-verified*: near matches reuse only in similarity bands where checks
  showed the step's own answer doesn't change more than re-asking does (how
  verified semantic caches judge reuse);
* *learned, downstream-verified*: the same, but a free-text step is judged by
  whether the next step's answer (the label) changes.

Two workloads: *small edits* (typos, greetings; some problems change by only a few
words) and *note edits* (staff rewrite a note line, a bigger harmless edit; problem
changes are blatant). Measured per day after the first: model calls and tokens, and
the share of tickets whose label is wrong (the simulated model alone gets about
4 % wrong).
"""

from __future__ import annotations

import statistics
from typing import Any

from benchmarks.tickets import CATEGORIES, TicketStream
from miniflow.blocks.builtin.llm import ReuseCache, map_rows, simulated_reply

POLICIES: dict[str, dict[str, Any]] = {
    "no reuse": {"reuse": "off"},
    "exact": {"reuse": "exact"},
    "fixed threshold 0.8": {
        "reuse": "tolerant",
        "min_similarity": 0.8,
        "max_change_rate": 1.0,
        "verify_rate": 0.0,
    },
    "fixed threshold 0.95": {
        "reuse": "tolerant",
        "min_similarity": 0.95,
        "max_change_rate": 1.0,
        "verify_rate": 0.0,
    },
    "learned, self-verified": {"reuse": "tolerant", "verify_rate": 0.05, "judge": "self"},
    "learned, downstream-verified": {"reuse": "tolerant", "verify_rate": 0.05},
}

WORKLOADS: dict[str, dict[str, Any]] = {
    "small edits": {"trivial_style": "small", "subtle_share": 0.4},
    "note edits": {"trivial_style": "notes", "subtle_share": 0.0},
}


def _ask(prompt: str, rng: Any) -> dict[str, Any]:
    return simulated_reply(prompt, rng=rng)


def run_days(
    policy: dict[str, Any],
    *,
    days: int,
    size: int,
    seed: int,
    workload: dict[str, Any] | None = None,
) -> list[dict[str, float]]:
    """One stream of ``days`` days under ``policy``; a row of measurements per day."""
    stream = TicketStream(size=size, seed=seed, **(workload or {}))
    out = []
    with ReuseCache(":memory:") as cache:
        cache_arg = cache if policy["reuse"] != "off" else None
        for day in range(days):
            if day:
                stream.advance()
            summed, s1 = map_rows(
                stream.today(),
                "Summarise this support ticket in one sentence:\n{text}",
                "summary",
                _ask,
                key="id",
                cache=cache_arg,
                seed=seed * 1000 + day,
                **policy,
            )
            labelled, s2 = map_rows(
                summed,
                "Which kind of request is this support ticket?\n{summary}",
                "label",
                _ask,
                choices=list(CATEGORIES),
                key="id",
                cache=cache_arg,
                seed=seed * 1000 + day,
                **policy,
            )
            wrong = sum(r["label"] != r["category"] for r in labelled)
            out.append(
                {
                    "day": day,
                    "calls": s1.calls + s2.calls,
                    "tokens": s1.input_tokens
                    + s1.output_tokens
                    + s2.input_tokens
                    + s2.output_tokens,
                    "tolerant": s1.tolerant + s2.tolerant,
                    "checks": s1.checks + s2.checks,
                    "error": wrong / len(labelled),
                }
            )
    return out


def _row(
    workload: str, policy: str, per_day: list[dict[str, float]], days: int, size: int, seeds: int
) -> dict[str, Any]:
    warm = [d for d in per_day if d["day"] > 0]
    late = [d for d in warm if d["day"] >= days // 2]
    return {
        "workload": workload,
        "policy": policy,
        "days": days,
        "tickets": size,
        "seeds": seeds,
        "calls_per_day": statistics.mean(d["calls"] for d in warm),
        "tokens_per_day": statistics.mean(d["tokens"] for d in warm),
        "tolerant_per_day": statistics.mean(d["tolerant"] for d in warm),
        "checks_per_day": statistics.mean(d["checks"] for d in warm),
        "label_error": statistics.mean(d["error"] for d in warm),
        "label_error_late": statistics.mean(d["error"] for d in late),
    }


def tolerant_reuse(cfg: Any, _checker: Any) -> list[dict[str, Any]]:
    """Experiment 8 rows: workload x policy, averaged over seeds and days 1.. (day 0 is cold)."""
    quick = cfg.repeats == 1 and cfg.workers <= 2
    days, size, seeds = (6, 40, 2) if quick else (15, 200, 3)
    rows: list[dict[str, Any]] = []
    for name, workload in WORKLOADS.items():
        for label, policy in POLICIES.items():
            per_day = [
                d
                for s in range(seeds)
                for d in run_days(policy, days=days, size=size, seed=s, workload=workload)
            ]
            rows.append(_row(name, label, per_day, days, size, seeds))
    for r in rows:
        base = next(b for b in rows if b["workload"] == r["workload"] and b["policy"] == "no reuse")
        r["calls_vs_no_reuse"] = r["calls_per_day"] / base["calls_per_day"]
    return rows
