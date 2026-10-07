from __future__ import annotations

import pytest
from hypothesis import strategies as st

from miniflow import Task, task


def noop() -> None:
    return None


def make_tasks(edges: dict[str, list[str]]) -> list[Task]:
    """Build io tasks from ``{name: [upstream names]}``."""
    return [task(noop, name=name, after=ups) for name, ups in edges.items()]


@pytest.fixture
def diamond() -> list[Task]:
    return make_tasks({"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]})


@st.composite
def random_dags(draw: st.DrawFn, max_nodes: int = 25) -> dict[str, list[str]]:
    """Random acyclic graphs: edges only go from lower to higher index."""
    n = draw(st.integers(min_value=1, max_value=max_nodes))
    names = [f"t{i}" for i in range(n)]
    edges: dict[str, list[str]] = {}
    for i, name in enumerate(names):
        ups = draw(st.lists(st.sampled_from(names[:i]), unique=True)) if i else []
        edges[name] = ups
    # Shuffle definition order so tests don't rely on it.
    order = draw(st.permutations(names))
    return {k: edges[k] for k in order}
