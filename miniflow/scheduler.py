"""Event-driven scheduler (spec §3.4, §3.6 "Scheduling loop"). Implemented in Phase 3.

Planned API::

    Scheduler(dag, *, router=None, store=None, policy=None).run(run_id=None) -> RunResult
    run(dag_or_tasks, **options) -> RunResult        # convenience wrapper

The loop keeps an unfinished-upstream counter per task, a ``heapq`` ready queue
keyed by ``-critical_path_length``, waits with
``concurrent.futures.wait(..., return_when=FIRST_COMPLETED)``, applies the
:data:`miniflow.states.TRANSITIONS` table, and marks every descendant of a
permanently failed task SKIPPED via :meth:`miniflow.graph.DAG.descendants`.
"""

from __future__ import annotations
