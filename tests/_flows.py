"""Module-level tasks for executor tests.

cpu tasks must be importable by a spawned child process, so they live here and
not inside test functions.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

from miniflow import task


class Unpicklable(Exception):
    """An exception carrying a lock, which cannot be pickled."""

    def __init__(self) -> None:
        super().__init__("cannot cross processes")
        self.lock = threading.Lock()


@task(kind="cpu")
def square(n: int) -> int:
    return n * n


@task(kind="cpu")
def add(a: int, b: int, *, scale: int = 1) -> int:
    return (a + b) * scale


@task(kind="cpu")
def getpid() -> int:
    return os.getpid()


@task(kind="cpu")
def cpu_fail(message: str) -> None:
    raise ValueError(message)


@task(kind="cpu")
def cpu_fail_unpicklable() -> None:
    raise Unpicklable()


@task(kind="cpu")
def cpu_sleep(seconds: float) -> float:
    time.sleep(seconds)
    return seconds


@task(kind="io")
def io_double(x: int) -> int:
    return 2 * x


@task(kind="io")
def io_sleep(seconds: float, value: object = None) -> object:
    time.sleep(seconds)
    return value


@task(kind="io")
def io_thread_name() -> str:
    return threading.current_thread().name


@task(kind="io")
def io_fail(message: str) -> None:
    raise KeyError(message)


@task(kind="async")
async def async_echo(value: object, delay: float = 0.0) -> object:
    await asyncio.sleep(delay)
    return value


@task(kind="async")
async def async_fail(message: str) -> None:
    await asyncio.sleep(0)
    raise RuntimeError(message)


@task(kind="async")
async def async_forever() -> None:
    await asyncio.sleep(3600)


@task(kind="cpu")
def cpu_crash() -> None:
    os._exit(1)
