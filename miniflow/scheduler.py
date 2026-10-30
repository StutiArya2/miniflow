"""Event-driven scheduler (spec §3.4, §3.6 "Scheduling loop").

The loop keeps, for every task, a counter of upstream tasks that have not yet
succeeded. A task whose counter reaches zero becomes READY and joins a
per-kind ``heapq`` ready queue keyed by its critical-path length (HEFT-style
rank, :meth:`miniflow.graph.DAG.critical_path_lengths`), so tasks that gate
the most remaining work start first. Tasks are dispatched while their worker
kind has free capacity, and the loop blocks in
``concurrent.futures.wait(..., return_when=FIRST_COMPLETED)``; there is no
polling. The wait is bounded only by the nearest retry or timeout deadline, and
on Windows by a short limit, since Ctrl+C can't interrupt a blocked wait there.

Failures go through :class:`miniflow.retry.RetryPolicy`. A task declared with
``optional=True`` that fails for good does not skip anything: the tasks after it run
and receive ``None`` in its place (like a trigger rule in larger workflow engines). A failed attempt
with budget left becomes RETRYING and re-enters the ready queue after its
backoff. A task whose attempts are exhausted becomes FAILED and every
descendant is marked SKIPPED (breadth-first, :meth:`DAG.descendants`), so a
run always terminates.

Every transition is checked against :data:`miniflow.states.TRANSITIONS` and
written to the :class:`miniflow.store.StateStore`. ``run(resume_from=...)``
starts a new run that reuses the stored results of tasks that succeeded in an
earlier run (marked ``from_cache``) and executes only the rest.

Opt-in failure-aware policy (:mod:`miniflow.priority`): ``priority="failure_aware"``
starts first the ready tasks that are likely to fail and whose failure would make
other waiting work useless, using failure rates from earlier runs of the same flow;
other tasks keep the critical-path order.
``prune=True`` marks SKIPPED the waiting tasks whose results can no longer reach any
sink once a task has failed for good. ``salvage=True`` (with ``prune``) keeps the doomed
tasks whose results are usually reused after a failure, learned from earlier runs, and
runs them after the live work. All default to off.

Timeouts: a task that exceeds ``timeout`` is failed with
:class:`miniflow.retry.TaskTimeoutError` and its future is cancelled.
Coroutines are cancelled for real. Python cannot stop a running thread or a
busy pool process, so those are abandoned: their eventual result is ignored.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import os
import pickle
import random
import sys
import time
import traceback
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from typing import Any, Literal

from miniflow.executors import ExecutorRouter, Outcome, TaskError
from miniflow.graph import DAG
from miniflow.preview import summarize
from miniflow.priority import (
    FailureAwareRank,
    FailureModel,
    ReuseHistory,
    ReuseModel,
    TaskHistory,
    doomed,
    learn_history,
    learn_reuse,
)
from miniflow.retry import RetryPolicy, TaskTimeoutError
from miniflow.states import TERMINAL, TRANSITIONS, State
from miniflow.store import StateStore
from miniflow.task import KINDS, Kind, Task

__all__ = ["Event", "RunResult", "Scheduler", "run"]

log = logging.getLogger("miniflow")

Priority = Literal["critical_path", "failure_aware"]
PRIORITIES: tuple[Priority, ...] = ("critical_path", "failure_aware")

# On Windows a blocked wait can't be interrupted by Ctrl+C (or the dashboard's
# Ctrl+Break), so the loop wakes up at least this often there to notice it.
_WAKE_LIMIT: float | None = 0.25 if sys.platform == "win32" else None

Listener = Callable[["Event"], None]


@dataclass(frozen=True)
class Event:
    """One state change, delivered to listeners (CLI progress, logging, tests)."""

    run_id: str
    task: str
    state: State
    attempt: int
    time: float
    worker: str | None = None
    error: str | None = None
    delay: float | None = None  # RETRYING only: seconds until the next attempt
    from_cache: bool = False


@dataclass(frozen=True)
class RunResult:
    """Final state of a run. ``status`` is ``SUCCESS``, ``FAILED`` or ``INTERRUPTED``."""

    run_id: str
    status: str
    states: Mapping[str, State]
    results: Mapping[str, Any]
    errors: Mapping[str, str]
    attempts: Mapping[str, int]
    from_cache: frozenset[str]
    started_at: float
    finished_at: float

    @property
    def ok(self) -> bool:
        return self.status == "SUCCESS"

    @property
    def duration(self) -> float:
        return self.finished_at - self.started_at

    def __getitem__(self, task: str) -> Any:
        return self.results[task]


@dataclass
class _Attempt:
    task: Task
    attempt: int
    started_wall: float
    deadline: float | None  # time.monotonic() value
    pool_generation: int


def _short(exc: BaseException) -> str:
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _describe(exc: BaseException) -> str:
    """Full traceback when the exception still has one (threads, coroutines)."""
    if exc.__traceback__ is not None:
        return "".join(traceback.format_exception(exc)).rstrip()
    return _short(exc)


class Scheduler:
    """Runs one DAG. Create a new :class:`Scheduler` (or call :meth:`run` again) per run.

    :param router: worker pools to use. When omitted the scheduler creates one
        and shuts it down at the end of each run.
    :param store: where states are persisted. Defaults to an in-memory store.
    :param policy: backoff between attempts. Each task's ``retries`` sets how many.
    :param flow: name recorded for the run (the CLI uses the flow file name).
    :param async_limit: maximum coroutines in flight at once.
    :param weight: estimated cost of a task for critical-path ranking (default 1).
    :param listeners: callables receiving every :class:`Event`.
    :param rng: random source for backoff jitter (seed it for reproducible tests).
    :param priority: ``"critical_path"`` (default) or ``"failure_aware"``, which uses
        failure rates and durations learned from earlier runs (see :mod:`miniflow.priority`).
    :param prune: skip waiting tasks whose results can no longer reach any sink after
        a task fails for good.
    :param history: attempt statistics per task for ``failure_aware``; by default they
        are read from the newest ``history_runs`` runs of ``flow`` in ``store``.
    :param salvage: with ``prune``, keep doomed tasks that are likely to be reused after
        the failure and run them after the live work (see :mod:`miniflow.priority`).
    :param reuse: reuse statistics per task for ``salvage``; by default learned from
        ``store`` like ``history``.
    """

    def __init__(
        self,
        dag: DAG,
        *,
        router: ExecutorRouter | None = None,
        store: StateStore | None = None,
        policy: RetryPolicy | None = None,
        flow: str = "flow",
        async_limit: int = 1000,
        weight: Callable[[Task], float] | None = None,
        listeners: Iterable[Listener] = (),
        rng: random.Random | None = None,
        signatures: Mapping[str, str | None] | None = None,
        config_signatures: Mapping[str, str] | None = None,
        priority: Priority = "critical_path",
        prune: bool = False,
        history: Mapping[str, TaskHistory] | None = None,
        history_runs: int = 20,
        salvage: bool = False,
        reuse: Mapping[str, ReuseHistory] | None = None,
    ) -> None:
        if async_limit < 1:
            raise ValueError("async_limit must be >= 1")
        if priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}, got {priority!r}")
        if salvage and not prune:
            raise ValueError("salvage needs prune=True")
        self.dag = dag
        self.store = store if store is not None else StateStore()
        self.policy = policy or RetryPolicy()
        self.flow = flow
        self.listeners = list(listeners)
        self._router_arg = router
        self._async_limit = async_limit
        self._rank = dag.critical_path_lengths(weight)
        self.priority: Priority = priority
        self.prune = prune
        self._history_arg = history
        self._history_runs = history_runs
        self._scorer: FailureAwareRank | None = None
        self._model: FailureModel | None = None
        self.salvage = salvage
        self._reuse_arg = reuse
        self._reuse: ReuseModel | None = None
        self._rng = rng
        self.signatures = dict(signatures) if signatures is not None else None
        # Settings-only identities, recorded for the editor's "changed since this run".
        self.config_signatures = dict(config_signatures) if config_signatures is not None else None

    # ------------------------------------------------------------------ public
    def run(self, resume_from: str | None = None) -> RunResult:
        """Execute the DAG and return its :class:`RunResult`.

        :param resume_from: id of an earlier run of the same flow; its successful
            results are reused and only the remaining tasks are executed.
        """
        owned = self._router_arg is None
        self.router = self._router_arg or ExecutorRouter()
        self._reset()
        if self.priority == "failure_aware" or self.salvage:
            # Learn before this run's own row exists, then keep learning during it.
            if self._history_arg is not None:
                learned = {
                    n: TaskHistory(h.attempts, h.failures, h.successes, h.success_seconds)
                    for n, h in self._history_arg.items()
                }
            else:
                learned = learn_history(self.store, self.flow, last_runs=self._history_runs)
            self._model = FailureModel(learned)
            if self.priority == "failure_aware":
                self._scorer = FailureAwareRank(self.dag, self._model)
        if self.salvage:
            if self._reuse_arg is not None:
                reuse = {n: ReuseHistory(h.chances, h.reused) for n, h in self._reuse_arg.items()}
            else:
                reuse = learn_reuse(self.store, self.flow, last_runs=self._history_runs)
            self._reuse = ReuseModel(reuse)
        self.run_id = self.store.create_run(
            self.flow,
            self.dag,
            signatures=self.signatures,
            config_signatures=self.config_signatures,
        )
        status = "SUCCESS"
        try:
            if resume_from is not None:
                self._load_cache(resume_from)
            for name in self.dag.topological_order():
                if self.state[name] is State.PENDING and self.waiting[name] == 0:
                    self._make_ready(name)
            self._loop()
        except KeyboardInterrupt:
            status = "INTERRUPTED"
            for fut in self.running:
                fut.cancel()
            self._abandoned = True
            log.warning("run %s interrupted; resume with --run %s", self.run_id, self.run_id)
        finally:
            if owned:
                if self._abandoned:
                    self.router.shutdown(wait=False, cancel_futures=True)
                else:
                    self.router.shutdown()

        if status == "SUCCESS":
            assert all(s in TERMINAL for s in self.state.values()), self.state
            # A run fails if any task was skipped or a required task failed;
            # a failed optional task alone leaves the run successful.
            if any(
                s is State.SKIPPED or (s is State.FAILED and not self.dag.tasks[n].optional)
                for n, s in self.state.items()
            ):
                status = "FAILED"
        self.store.finish_run(self.run_id, status)
        return RunResult(
            run_id=self.run_id,
            status=status,
            states=dict(self.state),
            results=dict(self.results),
            errors=dict(self.errors),
            attempts=dict(self.attempts),
            from_cache=frozenset(self.cached),
            started_at=self._started_at,
            finished_at=time.time(),
        )

    @property
    def abandoned_work(self) -> bool:
        """True if the last run left timed-out threads or processes still running."""
        return getattr(self, "_abandoned", False)

    # ------------------------------------------------------------------- setup
    def _reset(self) -> None:
        dag = self.dag
        self.state: dict[str, State] = dict.fromkeys(dag.tasks, State.PENDING)
        self.waiting: dict[str, int] = {n: len(ups) for n, ups in dag.upstream.items()}
        self.results: dict[str, Any] = {}
        self.errors: dict[str, str] = {}
        self.attempts: dict[str, int] = dict.fromkeys(dag.tasks, 0)
        self.cached: set[str] = set()
        # Entries are (-promoted, -rank, seq, name); promoted is always 0 without failure_aware.
        self.ready: dict[Kind, list[tuple[float, float, int, str]]] = {k: [] for k in KINDS}
        self.delayed: list[tuple[float, int, str]] = []
        self.running: dict[Future[Outcome], _Attempt] = {}
        self.in_use: dict[Kind, int] = dict.fromkeys(KINDS, 0)
        self.capacity: dict[Kind, int] = {
            "io": self.router.io_workers,
            "cpu": self.router.cpu_workers or os.cpu_count() or 1,
            "async": self._async_limit,
        }
        # Tasks whose result will never exist in this run (SKIPPED, or FAILED and required).
        self._dead: set[str] = set()
        # Doomed tasks kept to run in idle time because their results are likely reused.
        self._salvage: set[str] = set()
        self._seq = itertools.count()
        self._pool_generation = 0
        self._abandoned = False
        self._started_at = time.time()

    def _load_cache(self, previous_run: str) -> None:
        prior = self.store.load_completed(previous_run)
        prior_graph = self.store.get_graph(previous_run)
        if self.signatures is not None:
            runs = {record.run_id: record for record in self.store.list_runs()}
            if runs[previous_run].flow != self.flow:
                raise ValueError("cannot reuse results from a different flow")
        for name in self.dag.topological_order():
            # Reuse a result only if everything it was computed from was reused too.
            same = self.signatures is None or (
                self.signatures.get(name) is not None
                and self.signatures[name]
                == prior_graph.get("tasks", {}).get(name, {}).get("signature")
            )
            if same and name in prior and all(u in self.cached for u in self.dag.upstream[name]):
                self.cached.add(name)
                self.results[name] = prior[name]
                self.store.save_result(
                    self.run_id, name, prior[name], from_cache=True, preview=summarize(prior[name])
                )
                # PENDING -> SUCCESS without running is only legal here, so bypass _set.
                self.state[name] = State.SUCCESS
                self._emit(name, State.SUCCESS, from_cache=True)
                for child in self.dag.downstream[name]:
                    self.waiting[child] -= 1

    # -------------------------------------------------------------------- loop
    def _loop(self) -> None:
        while True:
            now = time.monotonic()
            while self.delayed and self.delayed[0][0] <= now:
                _, _, name = heapq.heappop(self.delayed)
                if self.state[name] is State.RETRYING:  # not pruned during its backoff
                    self._make_ready(name)
            self._dispatch()
            if not self.running and not self.delayed:
                return
            timeout = self._next_wakeup()
            if _WAKE_LIMIT is not None:
                timeout = _WAKE_LIMIT if timeout is None else min(timeout, _WAKE_LIMIT)
            if self.running:
                done, _ = wait(list(self.running), timeout=timeout, return_when=FIRST_COMPLETED)
                for fut in done:
                    self._complete(fut)
            elif timeout:
                time.sleep(timeout)
            self._expire_timeouts()

    def _next_wakeup(self) -> float | None:
        times = [d for d, _, _ in self.delayed[:1]]
        times += [a.deadline for a in self.running.values() if a.deadline is not None]
        if not times:
            return None
        return max(0.0, min(times) - time.monotonic())

    def _dispatch(self) -> None:
        for kind in KINDS:
            heap = self.ready[kind]
            while heap and self.in_use[kind] < self.capacity[kind]:
                entry = heapq.heappop(heap)
                name = entry[-1]
                if self.state[name] is not State.READY:  # pruned while queued
                    continue
                if name in self._salvage and entry[0] <= 0:
                    continue  # its entry from before it was doomed; a salvage entry follows
                self._start(self.dag.tasks[name])

    def _start(self, task: Task) -> None:
        name = task.name
        self.attempts[name] += 1
        attempt = self.attempts[name]
        args = tuple(self.results[u] for u in task.after) + task.args
        self._set(name, State.RUNNING, attempt=attempt)
        try:
            fut = self.router.submit(task, args, dict(task.kwargs))
        except BrokenProcessPool:
            self._restart_pool(self._pool_generation)
            fut = self.router.submit(task, args, dict(task.kwargs))
        self.in_use[task.kind] += 1
        deadline = time.monotonic() + task.timeout if task.timeout is not None else None
        self.running[fut] = _Attempt(task, attempt, time.time(), deadline, self._pool_generation)

    def _complete(self, fut: Future[Outcome]) -> None:
        att = self.running.pop(fut, None)
        if att is None:  # already timed out and abandoned
            return
        self.in_use[att.task.kind] -= 1
        try:
            outcome = fut.result()
        except TaskError as err:
            self._failed(att, err.original, err.worker, err.started_at, err.finished_at)
        except BrokenProcessPool as err:
            self._restart_pool(att.pool_generation)
            self._failed(att, err, None, att.started_wall, time.time())
        except Exception as err:  # e.g. arguments that could not be pickled
            self._failed(att, err, None, att.started_wall, time.time())
        else:
            self._succeeded(att, outcome)

    def _expire_timeouts(self) -> None:
        now = time.monotonic()
        for fut, att in list(self.running.items()):
            if att.deadline is None or now < att.deadline or fut.done():
                continue
            del self.running[fut]
            self.in_use[att.task.kind] -= 1
            fut.cancel()
            if att.task.kind != "async":
                self._abandoned = True  # threads and processes can't be interrupted
            err = TaskTimeoutError(
                f"task {att.task.name!r} exceeded its timeout of {att.task.timeout}s"
            )
            self._failed(att, err, None, att.started_wall, time.time())

    def _restart_pool(self, generation: int) -> None:
        # Every in-flight cpu future of a broken pool fails; restart only once per pool.
        if generation == self._pool_generation:
            log.warning("a worker process died; restarting the process pool")
            self.router.restart_cpu_pool()
            self._pool_generation += 1

    # ---------------------------------------------------------------- outcomes
    def _succeeded(self, att: _Attempt, outcome: Outcome) -> None:
        name = att.task.name
        self.results[name] = outcome.value
        self.errors.pop(name, None)
        if self._model is not None:
            self._model.record(name, False, outcome.finished_at - outcome.started_at)
        self.store.record_attempt(
            self.run_id,
            name,
            att.attempt,
            State.SUCCESS,
            None,
            outcome.started_at,
            outcome.finished_at,
            outcome.worker,
        )
        preview = summarize(outcome.value)
        try:
            # Also marks the task SUCCESS in the store.
            self.store.save_result(self.run_id, name, outcome.value, preview=preview)
            write_state = False
        except (pickle.PicklingError, TypeError, AttributeError) as exc:
            log.warning("result of %r can't be pickled (%s); it will rerun on resume", name, exc)
            write_state = True
        self._set(
            name, State.SUCCESS, attempt=att.attempt, worker=outcome.worker, persist=write_state
        )
        if write_state and preview:
            self.store.set_preview(self.run_id, name, preview)
        self._release(name)

    def _release(self, name: str) -> None:
        """Count ``name`` as done for its children and start any that are now unblocked."""
        for child in self.dag.downstream[name]:
            self.waiting[child] -= 1
            if self.waiting[child] == 0 and self.state[child] is State.PENDING:
                self._make_ready(child)

    def _failed(
        self,
        att: _Attempt,
        exc: BaseException,
        worker: str | None,
        started_at: float,
        finished_at: float,
    ) -> None:
        task, name = att.task, att.task.name
        short, full = _short(exc), _describe(exc)
        if self._model is not None:
            self._model.record(name, True, None)
        self.errors[name] = short
        self.store.record_attempt(
            self.run_id, name, att.attempt, State.FAILED, full, started_at, finished_at, worker
        )
        if self.policy.should_retry(task, att.attempt):
            delay = self.policy.delay(att.attempt, self._rng)
            self._set(
                name,
                State.RETRYING,
                attempt=att.attempt,
                worker=worker,
                error=short,
                stored_error=full,
                delay=delay,
            )
            heapq.heappush(self.delayed, (time.monotonic() + delay, next(self._seq), name))
            return
        self._set(
            name, State.FAILED, attempt=att.attempt, worker=worker, error=short, stored_error=full
        )
        if task.optional:
            # Optional: the tasks after it still run and receive None for its result.
            self.results[name] = None
            self._release(name)
            return
        reason = f"upstream task {name!r} failed"
        for desc in self.dag.descendants(name):
            if self.state[desc] is State.PENDING:
                self.errors[desc] = reason
                self._set(desc, State.SKIPPED, attempt=0, error=reason)
        if self.prune:
            self._prune_doomed(name)

    def _prune_doomed(self, failed: str) -> None:
        """Skip waiting tasks whose results could only have been used by dead tasks.

        With ``salvage``, a doomed task that is likely to be reused after this failure is
        kept instead and runs after all live work of its kind.
        """
        reason = f"pruned: only tasks skipped after {failed!r} failed needed its result"
        waiting = (State.PENDING, State.READY, State.RETRYING)
        for name in doomed(self.dag, self._dead):
            state = self.state[name]
            if state not in waiting or name in self._salvage:
                continue
            if (
                self._reuse is not None
                and state is not State.RETRYING
                and self._reuse.worth_salvaging(name)
            ):
                self._salvage.add(name)
                if state is State.READY:
                    self._push_salvage(name)
                continue
            self.errors[name] = reason
            self._set(name, State.SKIPPED, attempt=self.attempts[name], error=reason)
        # Drop pruned retries now, so the loop doesn't sleep through their backoff.
        self.delayed = [e for e in self.delayed if self.state[e[2]] is State.RETRYING]
        heapq.heapify(self.delayed)

    # ----------------------------------------------------------------- helpers
    def _push_salvage(self, name: str) -> None:
        # A positive first field sorts after every live entry (theirs are 0 or -1).
        kind = self.dag.tasks[name].kind
        heapq.heappush(self.ready[kind], (1.0, 0.0, next(self._seq), name))

    def _make_ready(self, name: str) -> None:
        self._set(name, State.READY, attempt=self.attempts[name])
        task = self.dag.tasks[name]
        if name in self._salvage:
            self._push_salvage(name)
            return
        if self._scorer is not None:
            promoted, rank = self._scorer.key(
                name,
                attempts_used=self.attempts[name],
                open_tasks=lambda u: self.state[u] in (State.PENDING, State.READY),
                dead=self._dead,
            )
        else:
            promoted, rank = 0.0, self._rank[name]
        heapq.heappush(self.ready[task.kind], (-promoted, -rank, next(self._seq), name))

    def _set(
        self,
        name: str,
        new: State,
        *,
        attempt: int,
        worker: str | None = None,
        error: str | None = None,
        stored_error: str | None = None,
        delay: float | None = None,
        persist: bool = True,
    ) -> None:
        old = self.state[name]
        if new not in TRANSITIONS[old]:
            raise AssertionError(f"illegal transition for {name!r}: {old} -> {new}")
        self.state[name] = new
        if new is State.SKIPPED or (new is State.FAILED and not self.dag.tasks[name].optional):
            self._dead.add(name)
        if persist:
            self.store.set_state(self.run_id, name, new, error=stored_error or error)
        log.debug("%s: %s -> %s (attempt %d)", name, old, new, attempt)
        self._emit(name, new, attempt=attempt, worker=worker, error=error, delay=delay)

    def _emit(
        self,
        name: str,
        state: State,
        *,
        attempt: int = 0,
        worker: str | None = None,
        error: str | None = None,
        delay: float | None = None,
        from_cache: bool = False,
    ) -> None:
        if not self.listeners:
            return
        event = Event(
            self.run_id, name, state, attempt, time.time(), worker, error, delay, from_cache
        )
        for listener in self.listeners:
            try:
                listener(event)
            except Exception:
                log.exception("event listener %r failed", listener)


def run(
    tasks: DAG | Iterable[Task],
    *,
    resume_from: str | None = None,
    **options: Any,
) -> RunResult:
    """Validate ``tasks`` into a DAG and run it. ``options`` go to :class:`Scheduler`."""
    dag = tasks if isinstance(tasks, DAG) else DAG.from_tasks(tasks)
    if options.get("store") is not None:
        return Scheduler(dag, **options).run(resume_from=resume_from)
    # Nobody else can reach a store created here, so close it when the run ends.
    with StateStore() as store:
        return Scheduler(dag, store=store, **options).run(resume_from=resume_from)
