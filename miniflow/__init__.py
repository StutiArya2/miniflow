"""MiniFlow: a parallel task scheduler built on the Python standard library."""

from miniflow.graph import DAG, CycleError, DuplicateTaskError, GraphError, UnknownDependencyError
from miniflow.states import State
from miniflow.task import Task, TaskDefinitionError, task

__all__ = [
    "DAG",
    "CycleError",
    "DuplicateTaskError",
    "GraphError",
    "State",
    "Task",
    "TaskDefinitionError",
    "UnknownDependencyError",
    "task",
]
__version__ = "0.1.0"
