"""Task states (spec §3.5), shared by the scheduler and the state store."""

from __future__ import annotations

from enum import StrEnum

__all__ = ["TERMINAL", "TRANSITIONS", "State"]


class State(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


TERMINAL = frozenset({State.SUCCESS, State.FAILED, State.SKIPPED})

# Allowed transitions. The scheduler asserts against this table.
# READY -> SKIPPED and RETRYING -> SKIPPED happen only with ``Scheduler(prune=True)``:
# a waiting task whose results can no longer reach any sink is pruned (miniflow.priority).
TRANSITIONS: dict[State, frozenset[State]] = {
    State.PENDING: frozenset({State.READY, State.SKIPPED}),
    State.READY: frozenset({State.RUNNING, State.SKIPPED}),
    State.RUNNING: frozenset({State.SUCCESS, State.FAILED, State.RETRYING}),
    State.RETRYING: frozenset({State.READY, State.SKIPPED}),
    State.SUCCESS: frozenset(),
    State.FAILED: frozenset(),
    State.SKIPPED: frozenset(),
}
