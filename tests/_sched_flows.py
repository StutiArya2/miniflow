"""Module-level task functions for scheduler tests.

cpu tasks must be importable by spawned worker processes, so they live here.
Tests build tasks around these functions with ``task(fn, name=..., ...)``.
"""

from __future__ import annotations

import asyncio
import collections
import os
import threading
import time
from pathlib import Path

from miniflow import task

# Per-test call counters for io/async functions (same process). Reset by a fixture.
calls: collections.Counter[str] = collections.Counter()
_lock = threading.Lock()


def count(key: str) -> int:
    with _lock:
        calls[key] += 1
        return calls[key]


def const(value: object) -> object:
    return value


def ignore(*_upstream: object, value: object = None) -> object:
    """Like ``const`` but for tasks with upstream results it doesn't need."""
    return value


def add(*xs: int) -> int:
    return sum(xs)


def sleep_then(seconds: float, value: object = None) -> object:
    time.sleep(seconds)
    return value


def counted(*_upstream: object, key: str, value: object = None) -> object:
    count(key)
    return value


def flaky(key: str, failures: int, value: object = "ok") -> object:
    """Fail the first ``failures`` calls for ``key``, then return ``value``."""
    if count(key) <= failures:
        raise ConnectionError(f"{key}: transient failure")
    return value


def always_fail(*_upstream: object, message: str = "boom") -> None:
    raise ValueError(message)


def fail_unless_file(*_upstream: object, path: str) -> str:
    if not Path(path).exists():
        raise FileNotFoundError(path)
    return "fixed"


def interrupt(*_upstream: object) -> None:
    raise KeyboardInterrupt


def unpicklable() -> threading.Lock:
    return threading.Lock()


def holds_lock(lock: threading.Lock) -> bool:
    return isinstance(lock, type(threading.Lock()))


async def async_sleep_then(seconds: float, value: object = None) -> object:
    await asyncio.sleep(seconds)
    return value


async def async_add(*xs: int) -> int:
    await asyncio.sleep(0)
    return sum(xs)


# --------------------------------------------------------------- cpu (picklable)
@task(kind="cpu")
def cpu_square(n: int) -> int:
    return n * n


@task(kind="cpu", after=["number"])
def cpu_square_of(n: int) -> int:
    return n * n


@task(kind="cpu", retries=1, after=["marker"])
def cpu_crash_once(marker: str) -> str:
    """Kill the worker process the first time (marker file absent), succeed after."""
    if not os.path.exists(marker):
        Path(marker).write_text("crashed")
        os._exit(1)
    return "survived"


@task(kind="cpu")
def cpu_pid() -> int:
    return os.getpid()
