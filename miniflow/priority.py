"""Failure-aware priority and doomed-task pruning (opt-in scheduler policy).

Problem
-------
The default ready queue is ordered by critical-path length, which assumes every task
succeeds. When a task fails for good, the scheduler skips its descendants, but it
still runs *sibling* work whose only consumers are those skipped descendants. In a
join ``A -> {risky, expensive} -> report``, if ``expensive`` runs first and ``risky``
then fails, the worker time spent on ``expensive`` produced a result nothing in the
run will use.

Mechanism
---------
Three parts that work together:

1. **Learned failure model.** :func:`learn_history` reads the attempt log of earlier
   runs of the same flow from the :class:`~miniflow.store.StateStore`. For each task
   it estimates the per-attempt failure probability ``p`` with a Beta prior
   (:class:`FailureModel`) and its mean successful duration. With ``k`` attempts left
   the task fails for good with probability ``q = p ** k`` and is expected to use
   ``(1 - p ** k) / (1 - p)`` attempts.

2. **Doomed-set analysis.** :func:`doomed` finds the tasks outside a set ``D`` of dead
   tasks whose every downstream path ends in ``D``: their results cannot reach any
   sink. It walks upstream from ``D`` with a live-children counter, so the cost is
   proportional to the part of the graph it touches, not to the whole DAG.

3. **Fail-fast promotion.** ``exposure(t)`` is the expected cost of the
   not-yet-started tasks that would become doomed if ``t`` failed (its doomed set,
   minus its own descendants, which are skipped whatever the order). Starting ``t``
   ahead of other ready work delays that work by at most ``c(t)``, the expected cost
   of ``t``, and only matters if ``t`` succeeds; it saves ``exposure(t)`` if ``t``
   fails. A pairwise exchange argument therefore promotes ``t`` when

   ``q(t) * exposure(t) > (1 - q(t)) * c(t)``

   and the task has at least one recorded failure. Promoted tasks run before all
   others, ordered by expected saving per second, ``q * exposure / c`` (the classic
   ratio rule for sequencing pass/fail tests). Every other task keeps the critical-path
   order, computed with expected costs (mean duration times expected attempts). A flow
   with no failure history is therefore scheduled exactly as before.

When the scheduler also has ``prune=True``, a permanent failure marks the doomed
tasks SKIPPED instead of running them. Ranking alone moves failure detection earlier;
together with pruning, running risky tasks first turns that early detection into
worker time that is never spent. Sinks are never pruned, and a pruned task reruns
normally on ``resume``.

Reuse-aware salvage (``salvage=True``)
--------------------------------------
Pruning defers work rather than deleting it when the failed run is later resumed: the
pruned steps run during the resume, on its critical path. Salvage decides per doomed
task between pruning and keeping it:

5. **Reuse model.** :func:`learn_reuse` pairs each failed run with the next run of the
   same flow and counts, per task, how often a result computed in the failed run was
   taken from the cache by the next one (:class:`ReuseModel`, Beta prior).
6. **Adaptive pruning.** A doomed task whose reuse probability reaches the threshold
   (``p * c > (1 - p) * c``, so 0.5) is kept instead of pruned, but sorts after every
   live ready task, so it only uses workers that live work doesn't need. Anything
   below the threshold is pruned as before.

So a flow whose failures are usually transient and resumed keeps computing its doomed
work, which the resume then reuses instead of redoing on its own critical path, while a
flow whose failures are abandoned learns low reuse and behaves like plain pruning.
Neither fixed choice is best in both situations; the learned decision picks per task.
"""

from __future__ import annotations

import itertools
import statistics
from collections import deque
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field

from miniflow.graph import DAG
from miniflow.states import State
from miniflow.store import StateStore
from miniflow.task import Task

__all__ = [
    "FailureAwareRank",
    "FailureModel",
    "ReuseHistory",
    "ReuseModel",
    "TaskHistory",
    "doomed",
    "expected_attempts",
    "learn_history",
    "learn_reuse",
    "static_keys",
]


@dataclass
class TaskHistory:
    """Attempt statistics of one task across earlier runs (and the current one)."""

    attempts: int = 0
    failures: int = 0
    successes: int = 0
    success_seconds: float = 0.0

    def record(self, failed: bool, seconds: float | None) -> None:
        self.attempts += 1
        if failed:
            self.failures += 1
        else:
            self.successes += 1
            if seconds is not None and seconds >= 0:
                self.success_seconds += seconds

    @property
    def mean_seconds(self) -> float | None:
        return self.success_seconds / self.successes if self.successes else None


def learn_history(store: StateStore, flow: str, *, last_runs: int = 20) -> dict[str, TaskHistory]:
    """Per-task attempt statistics from the newest ``last_runs`` runs of ``flow``.

    Attempts that timed out count as failures. Results reused from the cache have no
    attempt rows, so they don't count either way.
    """
    if last_runs < 1:
        return {}
    runs = [r.run_id for r in store.list_runs() if r.flow == flow][:last_runs]
    history: dict[str, TaskHistory] = {}
    for run_id in runs:
        for a in store.attempts(run_id):
            seconds = (
                a.finished_at - a.started_at
                if a.started_at is not None and a.finished_at is not None
                else None
            )
            history.setdefault(a.task, TaskHistory()).record(a.state is not State.SUCCESS, seconds)
    return history


def expected_attempts(p: float, attempts_left: int) -> float:
    """Expected attempts a task uses when each fails with ``p`` and ``attempts_left`` remain."""
    if attempts_left <= 0:
        return 0.0
    if p >= 1.0:
        return float(attempts_left)
    return (1.0 - p**attempts_left) / (1.0 - p)


@dataclass
class FailureModel:
    """Beta-Bernoulli estimate of each task's per-attempt failure probability.

    The prior is ``Beta(prior_failures, prior_attempts - prior_failures)``: a mean of
    1 % with the weight of two attempts by default, so a task with no history is
    assumed reliable and a few observed attempts quickly dominate.

    :param history: attempt statistics per task name (see :func:`learn_history`).
    :param default_seconds: cost of a task with no successful attempt on record; by
        default the median of the known mean durations, or 1 s if nothing is known.
    """

    history: dict[str, TaskHistory] = field(default_factory=dict)
    prior_failures: float = 0.02
    prior_attempts: float = 2.0
    default_seconds: float | None = None

    def __post_init__(self) -> None:
        if not 0 < self.prior_failures < self.prior_attempts:
            raise ValueError("need 0 < prior_failures < prior_attempts")
        if self.default_seconds is None:
            known = [m for h in self.history.values() if (m := h.mean_seconds) is not None]
            self.default_seconds = statistics.median(known) if known else 1.0

    def attempt_failure(self, name: str) -> float:
        """Posterior mean of the per-attempt failure probability of ``name``."""
        h = self.history.get(name)
        fails, tries = (h.failures, h.attempts) if h else (0, 0)
        return (fails + self.prior_failures) / (tries + self.prior_attempts)

    def seconds(self, name: str) -> float:
        h = self.history.get(name)
        mean = h.mean_seconds if h else None
        assert self.default_seconds is not None
        return mean if mean is not None else self.default_seconds

    def permanent_failure(self, task: Task, attempts_used: int = 0) -> float:
        """Probability that ``task`` fails all of its remaining attempts."""
        left = task.max_attempts - attempts_used
        if left <= 0:
            return 1.0
        return self.attempt_failure(task.name) ** left

    def expected_cost(self, task: Task, attempts_used: int = 0) -> float:
        """Expected worker-seconds ``task`` still needs, counting retries."""
        p = self.attempt_failure(task.name)
        return self.seconds(task.name) * expected_attempts(p, task.max_attempts - attempts_used)

    def record(self, name: str, failed: bool, seconds: float | None) -> None:
        """Add an attempt of the current run, so later decisions use it."""
        self.history.setdefault(name, TaskHistory()).record(failed, seconds)


def doomed(dag: DAG, dead: Collection[str]) -> set[str]:
    """Tasks outside ``dead`` whose every downstream path ends in ``dead``.

    Their results can't reach any sink, so running them is wasted work. Sinks are
    never doomed. Works upstream from ``dead`` with a live-children counter.
    """
    dead_set = set(dead)
    live: dict[str, int] = {}
    out: set[str] = set()
    frontier = deque(dead_set)
    while frontier:
        node = frontier.popleft()
        # upstream and downstream list a repeated dependency the same number of times,
        # so decrementing once per upstream entry keeps the counter consistent.
        for parent in dag.upstream[node]:
            if parent in dead_set or parent in out:
                continue
            if parent not in live:
                live[parent] = len(dag.downstream[parent])
            live[parent] -= 1
            if live[parent] == 0:
                out.add(parent)
                frontier.append(parent)
    return out


Key = tuple[float, float]
"""``(promoted, value)``: promoted tasks (1.0) first by saving per second, then the rest
(0.0) by expected critical-path length. Higher keys run first."""


class FailureAwareRank:
    """Ranks ready tasks for the scheduler's ``failure_aware`` priority.

    :param dag: the flow.
    :param model: failure and cost estimates.
    """

    def __init__(self, dag: DAG, model: FailureModel) -> None:
        self.dag = dag
        self.model = model
        self.ecp = dag.critical_path_lengths(model.expected_cost)

    def exposure(
        self,
        name: str,
        open_tasks: Callable[[str], bool],
        dead: Iterable[str] = (),
    ) -> float:
        """Expected cost of the open tasks that ``name`` failing would newly doom.

        :param open_tasks: whether a task hasn't started yet (so its cost can be saved).
        :param dead: tasks already skipped or failed; not counted again.
        """
        already = set(dead)
        seed = {name} | self.dag.descendants(name) | already
        newly = (
            doomed(self.dag, seed) - doomed(self.dag, already)
            if already
            else doomed(self.dag, seed)
        )
        return sum(self.model.expected_cost(self.dag.tasks[u]) for u in newly if open_tasks(u))

    def key(
        self,
        name: str,
        *,
        attempts_used: int,
        open_tasks: Callable[[str], bool],
        dead: Iterable[str] = (),
    ) -> Key:
        """Priority key of a ready task; see the module docstring."""
        task = self.dag.tasks[name]
        history = self.model.history.get(name)
        if task.optional or history is None or history.failures == 0:
            return (0.0, self.ecp[name])
        q = self.model.permanent_failure(task, attempts_used)
        cost = max(self.model.expected_cost(task, attempts_used), 1e-9)
        saving = q * self.exposure(name, open_tasks, dead)
        if saving > (1.0 - q) * cost:
            return (1.0, saving / cost)
        return (0.0, self.ecp[name])


def static_keys(dag: DAG, model: FailureModel) -> Mapping[str, Key]:
    """Keys of every task before anything has run (for inspection and tests)."""
    rank = FailureAwareRank(dag, model)
    return {n: rank.key(n, attempts_used=0, open_tasks=lambda _u: True) for n in dag.tasks}


# ---------------------------------------------------------------------- salvage
@dataclass
class ReuseHistory:
    """How often a task's result from a failed run was reused by the next run."""

    chances: int = 0
    reused: int = 0


def learn_reuse(store: StateStore, flow: str, *, last_runs: int = 20) -> dict[str, ReuseHistory]:
    """Per-task reuse statistics from consecutive runs of ``flow``.

    For each FAILED run among the newest ``last_runs`` runs (except the newest), every
    task that ran and succeeded in it is a *chance*; it counts as *reused* when the next
    run of the flow took that task's result from the cache (a resume, or ``--reuse``)
    instead of computing it again. A failed run that was never followed up contributes
    chances but no reuse, so a flow whose failures are abandoned learns not to salvage.
    """
    if last_runs < 2:
        return {}
    runs = [r for r in store.list_runs() if r.flow == flow][:last_runs]
    stats: dict[str, ReuseHistory] = {}
    # list_runs is newest first: runs[i + 1] is the run before runs[i].
    for later, earlier in itertools.pairwise(runs):
        if earlier.status != "FAILED":
            continue
        cached = {r.task for r in store.changes_since(later.run_id, 0.0) if r.from_cache}
        for rec in store.changes_since(earlier.run_id, 0.0):
            if rec.state is State.SUCCESS and not rec.from_cache:
                h = stats.setdefault(rec.task, ReuseHistory())
                h.chances += 1
                h.reused += rec.task in cached
    return stats


@dataclass
class ReuseModel:
    """Beta-Bernoulli estimate of the chance that a task's result is reused after a failure.

    The default prior has a mean of 30 % with the weight of two observations, so a task is
    salvaged (``threshold`` 0.5) only once history shows its results tend to be reused.
    """

    history: dict[str, ReuseHistory] = field(default_factory=dict)
    prior_reused: float = 0.6
    prior_chances: float = 2.0
    threshold: float = 0.5

    def __post_init__(self) -> None:
        if not 0 < self.prior_reused < self.prior_chances:
            raise ValueError("need 0 < prior_reused < prior_chances")

    def p_reuse(self, name: str) -> float:
        h = self.history.get(name)
        reused, chances = (h.reused, h.chances) if h else (0, 0)
        return (reused + self.prior_reused) / (chances + self.prior_chances)

    def worth_salvaging(self, name: str) -> bool:
        return self.p_reuse(name) >= self.threshold
