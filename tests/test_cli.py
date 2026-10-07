from pathlib import Path

import pytest

from miniflow.cli import load_flow, main

EXAMPLE = Path(__file__).parent.parent / "examples" / "pipeline.py"


def test_load_flow():
    _, dag = load_flow(EXAMPLE)
    assert dag.topological_order() == ["fetch", "analyse", "publish"]


def test_validate(capsys):
    assert main(["validate", str(EXAMPLE)]) == 0
    assert "fetch -> analyse -> publish" in capsys.readouterr().out


def test_graph_to_file(tmp_path):
    out = tmp_path / "g.dot"
    assert main(["graph", str(EXAMPLE), "-o", str(out)]) == 0
    assert '"fetch" -> "analyse";' in out.read_text()


def test_graph_stdout(capsys):
    assert main(["graph", str(EXAMPLE)]) == 0
    assert "digraph" in capsys.readouterr().out


def test_cycle_exits_with_error(tmp_path, capsys):
    flow = tmp_path / "cyclic.py"
    flow.write_text(
        "from miniflow import task\n"
        "@task(after=['b'])\ndef a(): pass\n"
        "@task(after=['a'])\ndef b(): pass\n"
    )
    assert main(["validate", str(flow)]) == 2
    assert "cycle" in capsys.readouterr().err


def test_missing_file():
    with pytest.raises(FileNotFoundError):
        load_flow("nope.py")
