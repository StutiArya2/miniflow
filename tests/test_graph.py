import graphlib
import itertools
import sys
import types

import networkx as nx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from miniflow import DAG, CycleError, DuplicateTaskError, UnknownDependencyError, task
from tests.conftest import make_tasks, noop, random_dags


def assert_valid_order(dag: DAG, order: list[str]) -> None:
    assert sorted(order) == sorted(dag.tasks)
    pos = {n: i for i, n in enumerate(order)}
    for n, ups in dag.upstream.items():
        for u in ups:
            assert pos[u] < pos[n], f"{u} must come before {n}"


def to_nx(edges: dict[str, list[str]]) -> nx.DiGraph:
    g = nx.DiGraph()
    g.add_nodes_from(edges)
    g.add_edges_from((u, n) for n, ups in edges.items() for u in ups)
    return g


# ------------------------------------------------------------------ validation
def test_diamond(diamond):
    dag = DAG.from_tasks(diamond)
    order = dag.topological_order()
    assert order[0] == "a"
    assert order[-1] == "d"
    assert_valid_order(dag, order)
    assert dag.roots() == ["a"]
    assert dag.sinks() == ["d"]
    assert set(dag.downstream["a"]) == {"b", "c"}


def test_empty_graph():
    dag = DAG.from_tasks([])
    assert len(dag) == 0
    assert dag.topological_order() == []
    assert dag.find_cycle() is None


def test_unknown_dependency():
    with pytest.raises(UnknownDependencyError) as exc:
        DAG.from_tasks(make_tasks({"a": ["ghost"]}))
    assert exc.value.task == "a"
    assert exc.value.missing == "ghost"


def test_duplicate_names():
    with pytest.raises(DuplicateTaskError):
        DAG.from_tasks(make_tasks({"a": []}) + make_tasks({"a": []}))


def test_same_task_twice_is_fine():
    (a,) = make_tasks({"a": []})
    assert len(DAG.from_tasks([a, a])) == 1


@pytest.mark.parametrize(
    ("edges", "cycle_nodes"),
    [
        ({"a": ["b"], "b": ["a"]}, {"a", "b"}),
        ({"a": ["c"], "b": ["a"], "c": ["b"]}, {"a", "b", "c"}),
        ({"root": [], "x": ["root", "z"], "y": ["x"], "z": ["y"], "leaf": ["z"]}, {"x", "y", "z"}),
    ],
)
def test_cycle_is_reported(edges, cycle_nodes):
    with pytest.raises(CycleError) as exc:
        DAG.from_tasks(make_tasks(edges))
    cycle = exc.value.cycle
    assert cycle[0] == cycle[-1]
    assert set(cycle) == cycle_nodes
    # Each consecutive pair must be a real edge u -> v (v lists u upstream).
    for u, v in itertools.pairwise(cycle):
        assert u in edges[v]
    assert "->" in str(exc.value)


def test_deep_chain_does_not_recurse():
    n = sys.getrecursionlimit() * 2
    edges = {f"t{i}": ([f"t{i - 1}"] if i else []) for i in range(n)}
    dag = DAG.from_tasks(make_tasks(edges))
    assert dag.topological_order()[-1] == f"t{n - 1}"
    assert dag.critical_path_lengths()["t0"] == n


def test_from_module():
    mod = types.ModuleType("flow")
    mod.a = task(noop, name="a")
    mod.b = task(noop, name="b", after=["a"])
    mod.unrelated = 42
    dag = DAG.from_module(mod)
    assert list(dag.tasks) == ["a", "b"]


# ------------------------------------------------------------------ algorithms
def test_critical_path(diamond):
    ranks = DAG.from_tasks(diamond).critical_path_lengths()
    assert ranks == {"a": 3, "b": 2, "c": 2, "d": 1}


def test_weighted_critical_path(diamond):
    weights = {"a": 1.0, "b": 5.0, "c": 1.0, "d": 2.0}
    ranks = DAG.from_tasks(diamond).critical_path_lengths(lambda t: weights[t.name])
    assert ranks == {"a": 8.0, "b": 7.0, "c": 3.0, "d": 2.0}


def test_descendants_and_ancestors(diamond):
    dag = DAG.from_tasks(diamond)
    assert dag.descendants("a") == {"b", "c", "d"}
    assert dag.descendants("b") == {"d"}
    assert dag.descendants("d") == set()
    assert dag.ancestors("d") == {"a", "b", "c"}


def test_to_dot(diamond):
    dot = DAG.from_tasks(diamond).to_dot({"a": "SUCCESS", "b": "FAILED"})
    assert dot.startswith("digraph miniflow {")
    assert '"a" -> "b";' in dot
    assert "palegreen" in dot
    assert "salmon" in dot


# ---------------------------------------------------------- property-based
@given(random_dags())
def test_order_matches_oracles(edges):
    dag = DAG.from_tasks(make_tasks(edges))
    order = dag.topological_order()
    assert_valid_order(dag, order)

    # graphlib (spec Â§3.7 cross-check) and networkx agree it is acyclic and valid.
    ts = graphlib.TopologicalSorter({n: ups for n, ups in edges.items()})
    assert_valid_order(dag, list(ts.static_order()))
    assert nx.is_directed_acyclic_graph(to_nx(edges))


@given(random_dags())
def test_critical_path_matches_networkx(edges):
    dag = DAG.from_tasks(make_tasks(edges))
    g = to_nx(edges)
    ranks = dag.critical_path_lengths()
    for n in edges:
        sub = g.subgraph(nx.descendants(g, n) | {n})
        assert ranks[n] == nx.dag_longest_path_length(sub) + 1


@given(random_dags(), st.data())
def test_descendants_match_networkx(edges, data):
    dag = DAG.from_tasks(make_tasks(edges))
    n = data.draw(st.sampled_from(sorted(edges)))
    assert dag.descendants(n) == nx.descendants(to_nx(edges), n)


@given(random_dags(max_nodes=15), st.data())
def test_added_back_edge_creates_detected_cycle(edges, data):
    g = to_nx(edges)
    pairs = [(u, v) for u in edges for v in nx.descendants(g, u)]
    if not pairs:
        return
    u, v = data.draw(st.sampled_from(sorted(pairs)))
    edges = {k: list(ups) for k, ups in edges.items()}
    edges[u].append(v)  # v -> u closes a cycle
    with pytest.raises(CycleError) as exc:
        DAG.from_tasks(make_tasks(edges))
    cycle = exc.value.cycle
    assert cycle[0] == cycle[-1]
    for a, b in itertools.pairwise(cycle):
        assert a in edges[b]
