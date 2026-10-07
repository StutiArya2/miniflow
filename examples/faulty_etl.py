"""A wider ETL-style flow with injected faults, for demos and the fault-tolerance evaluation.

    miniflow run examples/faulty_etl.py               # some downloads retry, one region fails
    MINIFLOW_FAIL_RATE=0 miniflow resume examples/faulty_etl.py   # fix the fault, finish the run

Environment:
    MINIFLOW_FAIL_RATE  probability that a download attempt fails (default 0.4)
    MINIFLOW_SEED       seed for the fault injection (default: random)

Shape: 4 downloads (io) -> 4 parses (cpu) -> merge (cpu) -> report (async) + archive (io),
plus a "slow_source" with a timeout that always hangs on its first attempt.
"""

import asyncio
import os
import random
import time

from miniflow import task

FAIL_RATE = float(os.environ.get("MINIFLOW_FAIL_RATE", "0.4"))
_rng = random.Random(os.environ.get("MINIFLOW_SEED"))
_attempts: dict[str, int] = {}

REGIONS = ("north", "south", "east", "west")


def _download(region: str) -> list[int]:
    time.sleep(0.3 + _rng.random() * 0.3)  # network latency
    if _rng.random() < FAIL_RATE:
        raise ConnectionError(f"{region}: connection reset by peer")
    return [hash((region, i)) % 1000 for i in range(50_000)]


@task(kind="io", retries=3)
def download_north():
    return _download("north")


@task(kind="io", retries=3)
def download_south():
    return _download("south")


@task(kind="io", retries=3)
def download_east():
    return _download("east")


@task(kind="io", retries=0)  # no retry budget: fails whenever the fault hits
def download_west():
    return _download("west")


@task(kind="io", retries=1, timeout=1.0)
def slow_source():
    _attempts["slow"] = _attempts.get("slow", 0) + 1
    if _attempts["slow"] == 1:
        time.sleep(30)  # hangs on the first attempt; the timeout kicks in
    return list(range(1000))


def _parse(rows: list[int]) -> dict[str, float]:
    total = sum(x * x for x in rows)
    return {"n": len(rows), "mean_sq": total / max(len(rows), 1)}


@task(kind="cpu", after=[download_north])
def parse_north(rows):
    return _parse(rows)


@task(kind="cpu", after=[download_south])
def parse_south(rows):
    return _parse(rows)


@task(kind="cpu", after=[download_east])
def parse_east(rows):
    return _parse(rows)


@task(kind="cpu", after=[download_west])
def parse_west(rows):
    return _parse(rows)


@task(kind="cpu", after=[parse_north, parse_south, parse_east, slow_source])
def merge(north, south, east, extra):
    parts = [north, south, east]
    return {"rows": sum(p["n"] for p in parts) + len(extra), "regions": len(parts)}


@task(kind="async", after=[merge])
async def report(summary):
    await asyncio.sleep(0.2)  # e.g. post to a webhook
    return f"merged {summary['rows']} rows from {summary['regions']} regions"


@task(kind="io", after=[merge, parse_west])
def archive(summary, west):
    time.sleep(0.1)
    return "archived"
