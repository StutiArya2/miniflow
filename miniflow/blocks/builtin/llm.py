"""AI over rows: ``llm_map``, an offline simulated model, and downstream-verified reuse.

``llm_map`` asks a model the same question about every row (``"Summarise:\\n{text}"``)
and adds the answer to each row as a new column. Providers are ``anthropic``,
``openai`` (or any compatible server) and ``simulated``, a small offline stand-in
used by the examples, tests and benchmarks: it needs no API key and, like a real
model, words its answers a little differently each time.

Reuse (setting ``reuse``)
-------------------------
``off``
    Every row asks the model.
``exact``
    A row whose filled-in prompt was answered before (in this flow folder's cache)
    gets the stored answer.
``tolerant``
    *Downstream-verified tolerant reuse.* Besides exact matches, a row whose input
    is only slightly different from one answered before (same ``key``, text
    similarity at least ``min_similarity``) may get the earlier answer. Whether
    that is allowed is learned, per step and per band of similarity, from whether
    such differences change what the flow does with the answer:

    * A step with ``choices`` (a classifier) judges itself: on a *check*, it asks
      the model anyway and compares the new choice with the reused one.
    * A free-text step (a summary) is judged by the next ``llm_map`` step that reads
      its column. On a check, the step outputs the fresh answer and records the one
      it would have reused; when the later step processes that row, it also looks up
      its own stored answer for the reused text and records whether the two answers
      differ. Rewording that doesn't change the downstream decision counts as no
      change, however different the text.

    Each band keeps a count of checks and changes; the estimated change rate is
    ``(changes + 1) / (checks + 2)``. The same is measured for identical inputs: a
    share of exact matches is asked again (``verify_rate`` for the first 30, then a
    tenth of that), which gives the *noise floor*, how often the outcome changes when
    nothing changed at all. Reuse in a band is allowed once it has been checked at
    least once and its change rate is at most the noise floor plus
    ``max_change_rate``: a near match may be reused when it is about as reliable as
    asking the model again. Until then every near match is checked (which costs the
    same as not reusing); after that a share ``verify_rate`` is still checked, so the
    estimate keeps up with drift. A free-text step
    with no later ``llm_map`` reading it is never judged, so it never reuses
    tolerantly.

The cache is a SQLite file in the flow folder (``cache_file``). Results reused or
computed are stored under their exact input, so a reused answer is also an exact
match next time.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import string
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from miniflow.blocks import Setting, block

__all__ = [
    "BANDS",
    "MapStats",
    "ReuseCache",
    "band_of",
    "llm_map",
    "map_rows",
    "similarity",
    "simulated_reply",
]

log = logging.getLogger("miniflow.llm")
Row = dict[str, Any]

# Similarity bands (lower edges); a near match falls in the highest band it reaches.
BANDS: tuple[float, ...] = (0.98, 0.95, 0.9, 0.8)
# Pseudo-band for re-asked identical inputs: the model's own run-to-run variation.
FLOOR = len(BANDS)
FLOOR_WARMUP = 30
_CHOICE_LINE = "Answer with exactly one of: "


# ------------------------------------------------------------- simulated model
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "billing": ("refund", "charged", "invoice", "payment", "billed", "price", "subscription"),
    "bug": ("crash", "error", "broken", "fails", "freezes", "bug", "doesn't load"),
    "feature": ("add", "would like", "feature", "support for", "option to", "wish"),
}
_ALL_KEYWORDS = tuple(k for words in _KEYWORDS.values() for k in words)
_OPENERS = ("Customer reports", "User says", "The customer writes that", "Ticket:", "Reported:")
_FILLER = {"really", "very", "just", "please", "again", "today", "now", "also", "still"}


def _words(text: str) -> int:
    return len(text.split())


def _hits(text: str, keywords: tuple[str, ...]) -> int:
    """Keyword occurrences; single words match whole words, ignoring swapped letters."""
    lowered = text.lower()
    words = re.findall(r"[a-z']+", lowered)
    count = 0
    for k in keywords:
        if " " in k:
            count += lowered.count(k)
        else:
            target = sorted(k)
            count += sum(w.startswith(k) or sorted(w) == target for w in words)
    return count


def simulated_reply(
    prompt: str, system: str = "", rng: random.Random | None = None
) -> dict[str, Any]:
    """An offline stand-in for a model call, with the same reply shape as ``llm_prompt``.

    A prompt that ends with ``Answer with exactly one of: a, b, c`` gets one of those
    choices, picked by keywords in the prompt (2 % of answers are a random other
    choice). Any other prompt gets a one-sentence summary of its most telling
    sentence, worded slightly differently each time (3 % of summaries lose the
    keyword that tells what the text is about).
    """
    rng = rng or random.Random()
    text = prompt
    if _CHOICE_LINE in prompt:
        body, _, line = prompt.rpartition(_CHOICE_LINE)
        options = [c.strip() for c in line.strip().rstrip(".").split(",") if c.strip()]

        def score(choice: str) -> int:
            return _hits(body, _KEYWORDS.get(choice.lower(), (choice.lower(),)))

        answer = max(options, key=score) if options else ""
        if len(options) > 1 and rng.random() < 0.02:
            answer = rng.choice([o for o in options if o != answer])
        text = answer
    else:
        body = prompt.split("\n", 1)[-1] if "\n" in prompt else prompt
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if s.strip()] or [body]
        best = max(sentences, key=lambda s: _hits(s, _ALL_KEYWORDS))
        head, sep, rest = best.partition(", ")
        if sep and len(head.split()) <= 3:  # drop a greeting such as "Hi team,"
            best = rest
        words = best.rstrip(".!?").split()[:16]
        words = [w for w in words if w.lower() not in _FILLER or rng.random() < 0.5]
        if rng.random() < 0.03:
            words = [w for w in words if not _hits(w, _ALL_KEYWORDS)]
        if words:
            words[0] = words[0].lower() if words[0][:1].isupper() and words[0] != "I" else words[0]
        text = f"{rng.choice(_OPENERS)} {' '.join(words)}."
    usage = {"input_tokens": int(1.3 * (_words(prompt) + _words(system))) + 1}
    usage["output_tokens"] = int(1.3 * _words(text)) + 1
    return {"text": text, "model": "simulated", "usage": usage}


# ---------------------------------------------------------------- similarity
def _normal(text: str) -> str:
    return " ".join(text.lower().split())


def similarity(a: str, b: str) -> float:
    """How alike two texts are, from 0 to 1 (``difflib`` ratio of normalised text)."""
    a, b = _normal(a), _normal(b)
    if a == b:
        return 1.0
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if matcher.real_quick_ratio() < 0.5:
        return matcher.real_quick_ratio()
    return matcher.ratio()


def band_of(score: float, floor: float) -> int | None:
    """Index into :data:`BANDS` of a near match, or ``None`` below ``floor``."""
    if score < floor:
        return None
    for i, edge in enumerate(BANDS):
        if score >= max(edge, floor):
            return i
    return len(BANDS) - 1


def _same(a: Any, b: Any) -> bool:
    return _normal(str(a)) == _normal(str(b))


# --------------------------------------------------------------------- cache
_SCHEMA = """
CREATE TABLE IF NOT EXISTS answers (
    step TEXT NOT NULL, digest TEXT NOT NULL, key TEXT, content TEXT NOT NULL,
    answer TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (step, digest)
);
CREATE INDEX IF NOT EXISTS answers_by_key ON answers (step, key, at);
CREATE TABLE IF NOT EXISTS checks (
    step TEXT NOT NULL, band INTEGER NOT NULL, checks INTEGER NOT NULL DEFAULT 0,
    changes INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (step, band)
);
CREATE TABLE IF NOT EXISTS pending (
    step TEXT NOT NULL, band INTEGER NOT NULL, fresh TEXT NOT NULL, reused TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS pending_by_fresh ON pending (fresh);
"""


class ReuseCache:
    """Stored answers, per-band check counts and checks waiting for a later step."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> ReuseCache:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _q(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
            self._db.commit()
            return rows

    def exact(self, step: str, digest: str) -> str | None:
        rows = self._q("SELECT answer FROM answers WHERE step = ? AND digest = ?", (step, digest))
        return rows[0][0] if rows else None

    def previous(self, step: str, key: str | None) -> list[tuple[str, str]]:
        """Earlier ``(content, answer)`` pairs to compare with: the same key, or recent ones."""
        if key is not None:
            return [
                (r[0], r[1])
                for r in self._q(
                    "SELECT content, answer FROM answers WHERE step = ? AND key = ?"
                    " ORDER BY at DESC LIMIT 3",
                    (step, key),
                )
            ]
        return [
            (r[0], r[1])
            for r in self._q(
                "SELECT content, answer FROM answers WHERE step = ? ORDER BY at DESC LIMIT 300",
                (step,),
            )
        ]

    def store(self, step: str, digest: str, key: str | None, content: str, answer: str) -> None:
        self._q(
            "INSERT OR REPLACE INTO answers VALUES (?, ?, ?, ?, ?, ?)",
            (step, digest, key, content, answer, time.time()),
        )

    def band_stats(self, step: str, band: int) -> tuple[int, int]:
        rows = self._q(
            "SELECT checks, changes FROM checks WHERE step = ? AND band = ?", (step, band)
        )
        return (rows[0][0], rows[0][1]) if rows else (0, 0)

    def record(self, step: str, band: int, changed: bool) -> None:
        self._q(
            "INSERT INTO checks VALUES (?, ?, 1, ?) ON CONFLICT (step, band)"
            " DO UPDATE SET checks = checks + 1, changes = changes + excluded.changes",
            (step, band, int(changed)),
        )

    def add_pending(self, step: str, band: int, fresh: str, reused: str) -> None:
        self._q(
            "INSERT INTO pending VALUES (?, ?, ?, ?, ?)", (step, band, fresh, reused, time.time())
        )

    def take_pending(self, fresh: str) -> list[tuple[str, int, str]]:
        """Checks waiting for a step that reads ``fresh``: ``(step, band, reused)``."""
        rows = self._q("SELECT rowid, step, band, reused FROM pending WHERE fresh = ?", (fresh,))
        if rows:
            marks = ",".join("?" * len(rows))
            self._q(f"DELETE FROM pending WHERE rowid IN ({marks})", tuple(r[0] for r in rows))
        return [(r[1], r[2], r[3]) for r in rows]


def change_rate(checks: int, changes: int) -> float:
    """Estimated share of near matches in a band whose difference matters downstream."""
    return (changes + 1) / (checks + 2)


def change_bound(checks: int, changes: int, z: float = 1.645) -> float:
    """One-sided 95 % upper bound (Wilson) on a band's change rate: 1 before any check."""
    if checks <= 0:
        return 1.0
    p = changes / checks
    denom = 1 + z * z / checks
    centre = p + z * z / (2 * checks)
    spread = z * ((p * (1 - p) + z * z / (4 * checks)) / checks) ** 0.5
    return float(min(1.0, (centre + spread) / denom))


def noise_floor(checks: int, changes: int) -> float:
    """Estimated share of re-asked identical inputs whose outcome changed (0 before any)."""
    return change_rate(checks, changes) if checks else 0.0


# ----------------------------------------------------------------------- map
@dataclass
class MapStats:
    """What one ``llm_map`` call did with its rows."""

    step: str = ""  # identity of the step in the cache
    rows: int = 0
    calls: int = 0
    exact: int = 0
    tolerant: int = 0
    checks: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    bands: dict[int, tuple[int, int]] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"{self.rows} rows: {self.calls} model calls, {self.exact} exact reuses, "
            f"{self.tolerant} tolerant reuses ({self.checks} checks)"
        )


def _fields(template: str) -> list[str]:
    names = []
    for _, name, _, _ in string.Formatter().parse(template):
        if name and name not in names:
            names.append(name)
    return names


class _RowFormat(dict[str, Any]):
    def __missing__(self, key: str) -> Any:
        raise KeyError(f"the prompt uses {{{key}}}, but a row has no column {key!r}")


def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def map_rows(
    rows: Sequence[Mapping[str, Any]],
    prompt: str,
    field: str,
    ask: Callable[[str, random.Random], dict[str, Any]],
    *,
    system: str = "",
    model_id: str = "",
    choices: Sequence[str] | None = None,
    reuse: str = "off",
    key: str = "",
    cache: ReuseCache | None = None,
    min_similarity: float = 0.8,
    max_change_rate: float = 0.05,
    verify_rate: float = 0.1,
    workers: int = 4,
    seed: int | None = None,
    judge: str = "downstream",
) -> tuple[list[Row], MapStats]:
    """The work of ``llm_map``: answer every row, reusing as ``reuse`` allows.

    ``judge="self"`` makes a free-text step judge checks by its own answer (did the
    text change?) instead of by the next step's; it exists to measure what the
    downstream judgement adds.
    """
    if reuse not in ("off", "exact", "tolerant"):
        raise ValueError(f"reuse must be off, exact or tolerant, not {reuse!r}")
    if reuse != "off" and cache is None:
        raise ValueError("reuse needs a cache")
    fields = _fields(prompt)
    full = prompt + (f"\n\n{_CHOICE_LINE}{', '.join(choices)}." if choices else "")
    step = _digest("llm_map", model_id, system, full, field)[:16]
    rng = random.Random(seed)
    stats = MapStats(step=step, rows=len(rows))
    out: list[Row] = [dict(r) for r in rows]
    # Per row: the filled prompt, the text compared for near matches, and its key.
    filled = [full.format_map(_RowFormat(r)) for r in out]
    contents = ["\n".join(str(r[f]) for f in fields) for r in out]
    keys = [str(r[key]) if key and key in r else None for r in out]
    digests = [_digest(step, p) for p in filled]

    def normalise(answer: str) -> str:
        answer = answer.strip()
        if choices:
            lowered = answer.lower()
            for c in choices:
                if c.lower() in lowered:
                    return c
        return answer

    # Downstream judgement: rows this step reads that an earlier step is waiting on.
    waiting: list[list[tuple[str, int, str, str]]] = [[] for _ in out]
    if cache is not None and reuse != "off":
        for i, r in enumerate(out):
            for f in fields:
                for earlier, band, reused in cache.take_pending(str(r[f])):
                    waiting[i].append((earlier, band, f, reused))

    answers: list[str | None] = [None] * len(out)
    to_ask: list[int] = []
    # Rows whose tolerant reuse is being checked: (band, reused answer).
    checking: dict[int, tuple[int, str]] = {}
    floor, floor_checks = 0.0, 0
    if cache is not None and reuse == "tolerant":
        floor_checks, floor_changes = cache.band_stats(step, FLOOR)
        floor = noise_floor(floor_checks, floor_changes)
    # Learn the floor quickly at first, then keep an eye on it.
    floor_check_rate = verify_rate if floor_checks < FLOOR_WARMUP else verify_rate / 10
    for i in range(len(out)):
        if reuse == "off" or cache is None:
            to_ask.append(i)
            continue
        hit = cache.exact(step, digests[i])
        if hit is not None:
            if reuse == "tolerant" and rng.random() < floor_check_rate:
                # Re-ask an identical input now and then: how often does the outcome
                # change with no input change at all? That is the noise floor.
                checking[i] = (FLOOR, hit)
                stats.checks += 1
                to_ask.append(i)
                continue
            answers[i] = hit
            stats.exact += 1
            continue
        if reuse == "tolerant":
            near = max(
                ((similarity(contents[i], c), a) for c, a in cache.previous(step, keys[i])),
                default=None,
                key=lambda pair: pair[0],
            )
            near_band = band_of(near[0], min_similarity) if near else None
            if near is not None and near_band is not None:
                checks, changes = cache.band_stats(step, near_band)
                trusted = change_bound(checks, changes) <= floor + max_change_rate
                if trusted and rng.random() >= verify_rate:
                    answers[i] = near[1]
                    stats.tolerant += 1
                    continue
                checking[i] = (near_band, near[1])
                stats.checks += 1
        to_ask.append(i)

    def call(i: int) -> dict[str, Any]:
        return ask(filled[i], random.Random(None if seed is None else f"{seed}:{filled[i]}"))

    if to_ask:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(to_ask)))) as pool:
            replies = list(pool.map(call, to_ask))
        for i, reply in zip(to_ask, replies, strict=True):
            answers[i] = normalise(str(reply.get("text", "")))
            usage = reply.get("usage") or {}
            stats.input_tokens += int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            stats.output_tokens += int(
                usage.get("output_tokens") or usage.get("completion_tokens") or 0
            )
        stats.calls = len(to_ask)

    for i, r in enumerate(out):
        answer = answers[i]
        assert answer is not None
        r[field] = answer
        if cache is None or reuse == "off":
            continue
        cache.store(step, digests[i], keys[i], contents[i], answer)
        if i in checking:
            band, reused = checking[i]
            if choices or judge == "self":
                cache.record(step, band, not _same(answer, reused))
            elif not _same(answer, reused):
                cache.add_pending(step, band, answer, reused)
            else:
                cache.record(step, band, False)
        for earlier, band, f, reused in waiting[i]:
            # What would this step have answered for the text the earlier step reused?
            alt = dict(r)
            alt[f] = reused
            stored = cache.exact(step, _digest(step, full.format_map(_RowFormat(alt))))
            if stored is not None:
                cache.record(earlier, band, not _same(stored, answer))
    if cache is not None:
        stats.bands = {b: cache.band_stats(step, b) for b in range(len(BANDS) + 1)}
    return out, stats


def _asker(
    provider: str,
    api_key: str,
    model: str,
    system: str,
    max_tokens: int,
    base_url: str,
    timeout: float,
) -> Callable[[str, random.Random], dict[str, Any]]:
    if provider == "simulated":
        return lambda prompt, rng: simulated_reply(prompt, system, rng)
    if not api_key:
        raise ValueError(f"provider {provider!r} needs an api_key ({{{{ env.NAME }}}})")
    from miniflow.blocks.builtin.web import llm_prompt

    def ask(prompt: str, _rng: random.Random) -> dict[str, Any]:
        reply: dict[str, Any] = llm_prompt(
            prompt=prompt,
            api_key=api_key,
            provider=provider,
            model=model,
            system=system,
            max_tokens=max_tokens,
            base_url=base_url,
            timeout=timeout,
        )
        return reply

    return ask


@block(
    "llm_map",
    settings=[
        Setting("rows", list, help="Rows to process, e.g. {{ tickets }}"),
        Setting(
            "prompt",
            str,
            help="Question asked for every row; {column} is replaced by the row's value",
        ),
        Setting("field", str, help="Column to put each answer in"),
        Setting(
            "provider",
            str,
            default="anthropic",
            choices=("anthropic", "openai", "simulated"),
            help="simulated: offline stand-in, no key needed",
        ),
        Setting(
            "api_key",
            str,
            default="",
            secret=True,
            help="{{ env.ANTHROPIC_API_KEY }} (not needed for simulated)",
        ),
        Setting("model", str, default="", help="Model name (default: the provider's)"),
        Setting("system", str, default="", help="Standing instructions for the model"),
        Setting(
            "choices",
            list,
            default=None,
            help='Allowed answers, e.g. ["billing", "bug"]; the answer is one of them',
        ),
        Setting("max_tokens", int, default=256),
        Setting("base_url", str, default="", help="Another server with the same API"),
        Setting("timeout", float, default=120.0),
        Setting("workers", int, default=4, help="Rows asked at the same time"),
        Setting(
            "reuse",
            str,
            default="off",
            choices=("off", "exact", "tolerant"),
            help="Reuse earlier answers: exact matches, or also near matches (tolerant)",
        ),
        Setting(
            "key", str, default="", help="Column identifying a row across runs, e.g. ticket id"
        ),
        Setting(
            "cache_file",
            str,
            default=".miniflow/llm_cache.sqlite",
            file=True,
            help="Where answers and checks are kept",
        ),
        Setting("min_similarity", float, default=0.8, help="tolerant: closest match to consider"),
        Setting(
            "max_change_rate",
            float,
            default=0.05,
            help="tolerant: allowed change rate above the model's own run-to-run variation",
        ),
        Setting(
            "verify_rate", float, default=0.1, help="tolerant: share of near matches still checked"
        ),
        Setting("seed", int, default=None, help="Fixes random choices (tests and benchmarks)"),
    ],
)
def llm_map(
    rows: list[Row],
    prompt: str,
    field: str,
    provider: str = "anthropic",
    api_key: str = "",
    model: str = "",
    system: str = "",
    choices: list[str] | None = None,
    max_tokens: int = 256,
    base_url: str = "",
    timeout: float = 120.0,
    workers: int = 4,
    reuse: str = "off",
    key: str = "",
    cache_file: str = ".miniflow/llm_cache.sqlite",
    min_similarity: float = 0.8,
    max_change_rate: float = 0.05,
    verify_rate: float = 0.1,
    seed: int | None = None,
) -> list[Row]:
    """Ask an AI model about every row and add the answer as a new column."""
    ask = _asker(provider, api_key, model, system, max_tokens, base_url, timeout)
    cache = ReuseCache(cache_file) if reuse != "off" else None
    try:
        out, stats = map_rows(
            rows,
            prompt,
            field,
            ask,
            system=system,
            model_id=f"{provider}:{model}",
            choices=choices,
            reuse=reuse,
            key=key,
            cache=cache,
            min_similarity=min_similarity,
            max_change_rate=max_change_rate,
            verify_rate=verify_rate,
            workers=workers,
            seed=seed,
        )
    finally:
        if cache is not None:
            cache.close()
    log.info("llm_map %s: %s", field, stats.summary())
    return out
