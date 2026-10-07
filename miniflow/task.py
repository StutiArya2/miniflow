"""Task definition: the ``@task`` decorator and the :class:`Task` dataclass.

A task wraps a plain function together with its worker kind, upstream
dependencies, retry budget and timeout::

    @task(kind="io", retries=3, args=("https://example.com",))
    def fetch(url): ...

    @task(kind="cpu", after=[fetch])
    def analyse(data): ...

    @task(kind="async", after=[analyse])
    async def publish(report): ...

Calling convention when the scheduler runs a task::

    fn(*upstream_results, *task.args, **task.kwargs)

where ``upstream_results`` are the results of the tasks in ``after``, in the
order they were listed.
"""

from __future__ import annotations

import importlib
import inspect
import pickle
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias, get_args

__all__ = ["KINDS", "Kind", "Task", "TaskDefinitionError", "task"]

Kind: TypeAlias = Literal["io", "cpu", "async"]
KINDS: tuple[Kind, ...] = get_args(Kind)

Dependency: TypeAlias = "Task | str"


class TaskDefinitionError(ValueError):
    """Raised when a task is declared with invalid options."""


@dataclass(frozen=True, eq=False)
class Task:
    """A unit of work in a MiniFlow DAG.

    Tasks compare and hash by identity; within one DAG they are addressed by
    :attr:`name`, which must be unique.
    """

    fn: Callable[..., Any]
    name: str
    kind: Kind = "io"
    after: tuple[str, ...] = ()
    retries: int = 0
    timeout: float | None = None
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise TaskDefinitionError(
                f"task {self.name!r}: kind must be one of {KINDS}, got {self.kind!r}"
            )
        if self.retries < 0:
            raise TaskDefinitionError(f"task {self.name!r}: retries must be >= 0")
        if self.timeout is not None and self.timeout <= 0:
            raise TaskDefinitionError(f"task {self.name!r}: timeout must be > 0")
        if self.name in self.after:
            raise TaskDefinitionError(f"task {self.name!r} depends on itself")
        if len(set(self.after)) != len(self.after):
            raise TaskDefinitionError(f"task {self.name!r} lists a dependency twice")
        is_coro = inspect.iscoroutinefunction(self.fn)
        if self.kind == "async" and not is_coro:
            raise TaskDefinitionError(
                f"task {self.name!r}: kind='async' requires an 'async def' function"
            )
        if self.kind != "async" and is_coro:
            raise TaskDefinitionError(
                f"task {self.name!r}: coroutine functions must use kind='async'"
            )
        if self.kind == "cpu":
            _check_importable(self)

    @property
    def max_attempts(self) -> int:
        """Total number of attempts allowed (first run plus retries)."""
        return self.retries + 1

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Call the wrapped function directly, bypassing the scheduler."""
        return self.fn(*args, **kwargs)

    def __repr__(self) -> str:
        return f"Task({self.name!r}, kind={self.kind!r}, after={list(self.after)!r})"

    def __reduce__(self) -> tuple[Any, ...]:
        # Process workers receive tasks *by reference* (module + qualname): the
        # function is re-imported in the child, never serialised.
        module, qualname = self.fn.__module__, self.fn.__qualname__
        try:
            target = _lookup(module, qualname)
        except (ImportError, AttributeError) as exc:
            raise pickle.PicklingError(
                f"task {self.name!r}: {module}.{qualname} can't be imported by a worker"
            ) from exc
        if target is self:  # the usual case: a @task-decorated module attribute
            return (_load_task, (module, qualname))
        # A task built from a module-level function under another name, e.g.
        # task(work, name="t1", kind="cpu"): rebuild it in the child around the
        # same imported function.
        fields = (
            self.name,
            self.kind,
            self.after,
            self.retries,
            self.timeout,
            self.args,
            dict(self.kwargs),
        )
        return (_rebuild_task, (module, qualname, fields))


def _lookup(module: str, qualname: str) -> Any:
    obj: Any = importlib.import_module(module)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


def _load_task(module: str, qualname: str) -> Task:
    obj = _lookup(module, qualname)
    if not isinstance(obj, Task):
        raise TypeError(f"{module}.{qualname} is not a MiniFlow task")
    return obj


def _rebuild_task(module: str, qualname: str, fields: tuple[Any, ...]) -> Task:
    obj = _lookup(module, qualname)
    fn = obj.fn if isinstance(obj, Task) else obj
    t = object.__new__(Task)
    names = ("name", "kind", "after", "retries", "timeout", "args", "kwargs")
    object.__setattr__(t, "fn", fn)
    for name, value in zip(names, fields, strict=True):
        object.__setattr__(t, name, value)
    return t


def _check_importable(t: Task) -> None:
    """cpu tasks run in another process (spec §3.3): check what can be checked up front.

    The function must be importable by the worker, and the task's own ``args`` and
    ``kwargs`` must be picklable. Upstream results are only known at run time; if
    one can't be pickled, that attempt fails with the pickling error.
    """
    qualname = getattr(t.fn, "__qualname__", "")
    if "<lambda>" in qualname or "<locals>" in qualname:
        raise TaskDefinitionError(
            f"task {t.name!r}: kind='cpu' needs a module-level function so it can be "
            f"pickled for a worker process (got {qualname!r})"
        )
    try:
        pickle.dumps((t.args, dict(t.kwargs)))
    except Exception as exc:
        raise TaskDefinitionError(
            f"task {t.name!r}: kind='cpu' arguments must be picklable to reach a worker "
            f"process ({type(exc).__name__}: {exc})"
        ) from exc


def _dep_name(dep: Dependency) -> str:
    if isinstance(dep, Task):
        return dep.name
    if isinstance(dep, str):
        return dep
    raise TaskDefinitionError(f"dependencies must be tasks or task names, got {dep!r}")


def task(
    fn: Callable[..., Any] | None = None,
    *,
    kind: Kind = "io",
    after: Iterable[Dependency] = (),
    retries: int = 0,
    timeout: float | None = None,
    name: str | None = None,
    args: Iterable[Any] = (),
    kwargs: Mapping[str, Any] | None = None,
) -> Any:
    """Declare a task. Usable bare (``@task``) or with options (``@task(kind="cpu")``)."""

    def wrap(f: Callable[..., Any]) -> Task:
        return Task(
            fn=f,
            name=name or f.__name__,
            kind=kind,
            after=tuple(_dep_name(d) for d in after),
            retries=retries,
            timeout=timeout,
            args=tuple(args),
            kwargs=dict(kwargs or {}),
        )

    return wrap(fn) if fn is not None else wrap
