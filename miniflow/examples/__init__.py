"""Real-world example pipelines that ship with MiniFlow.

Each one uses live public data (set ``MINIFLOW_OFFLINE=1`` to use saved snapshots):

* ``earthquakes``  - live earthquake monitor (US Geological Survey)
* ``air_quality``  - air-quality and outdoor-exercise advisor for eight Indian cities (Open-Meteo)
* ``news_digest``  - morning digest of the Hacker News front page
* ``shop_report``  - online shop's sales report with real exchange rates (European Central Bank)

Run one with ``miniflow example <name> --ui``, or from the dashboard's Examples gallery.
"""

from __future__ import annotations

import importlib
import types
from collections import Counter
from typing import Any

from miniflow.graph import DAG

__all__ = ["NAMES", "catalog", "load"]

NAMES = ("earthquakes", "air_quality", "news_digest", "shop_report")


def load(name: str) -> tuple[types.ModuleType, DAG]:
    """Import a bundled example by name and build its DAG."""
    if name not in NAMES:
        raise KeyError(f"no example called {name!r}; choose from {', '.join(NAMES)}")
    module = importlib.import_module(f"miniflow.examples.{name}")
    return module, DAG.from_module(module)


def catalog() -> list[dict[str, Any]]:
    """Title, story and shape of every bundled example (no network access)."""
    out = []
    for name in NAMES:
        _, dag = load(name)
        kinds = Counter(t.kind for t in dag.tasks.values())
        out.append(
            {
                "name": name,
                **dag.about,
                "tasks": len(dag),
                "kinds": {k: kinds.get(k, 0) for k in ("io", "cpu", "async")},
            }
        )
    return out
