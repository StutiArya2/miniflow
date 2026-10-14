"""Exercise the browser formatter and its injection boundary with Node's test runner."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_chat_markdown_rendering():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for browser JavaScript checks")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", str(root / "tests/js/test_chat_markdown.cjs")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
