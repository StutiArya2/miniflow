"""Conservative identities for JSON-flow steps used by partial-run reuse."""

from __future__ import annotations

import hashlib
import inspect
import os
import pickle
import re
from pathlib import Path
from typing import Any

from miniflow.blocks import get_block
from miniflow.task import Task

_ENV_REF = re.compile(r"\{\{\s*env\.([A-Za-z_]\w*)\s*\}\}")
_PARAM_REF = re.compile(r"\{\{\s*params\.([A-Za-z_]\w*)")


def _environment_refs(value: Any) -> set[str]:
    return _references(value, _ENV_REF)


def _references(value: Any, pattern: re.Pattern[str]) -> set[str]:
    if isinstance(value, str):
        return set(pattern.findall(value))
    if isinstance(value, dict):
        return set().union(*(_references(v, pattern) for v in value.values()))
    if isinstance(value, list | tuple):
        return set().union(*(_references(v, pattern) for v in value))
    return set()


def _file_digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def step_signature(step: Task) -> str | None:
    """Hash code, configuration, referenced environment and local file inputs.

    A missing or unreadable input fails closed by changing the identity. Upstream
    results are checked separately: a step is reusable only if all parents reused.
    """
    kwargs = dict(step.kwargs)
    if "params" in kwargs:
        used = _references({k: v for k, v in kwargs.items() if k != "params"}, _PARAM_REF)
        kwargs["params"] = {
            key: kwargs["params"][key] for key in sorted(used) if key in kwargs["params"]
        }
    sources: list[tuple[str, str | None]] = []
    try:
        source = inspect.getsourcefile(step.fn)
        if source:
            sources.append((source, _file_digest(Path(source))))
    except (OSError, TypeError):
        pass
    if "block" in kwargs:
        spec = get_block(kwargs["block"])
        try:
            source = inspect.getsourcefile(spec.fn)
            if source:
                sources.append((source, _file_digest(Path(source))))
        except (OSError, TypeError):
            pass
        settings = kwargs.get("settings", {})
        base = Path(kwargs.get("base_dir", "."))
        for setting in spec.settings:
            raw = settings.get(setting.name)
            if setting.file and isinstance(raw, str) and "{{" in raw:
                return None  # resolved paths depend on live values; rerun the step
            if setting.file and isinstance(raw, str) and "{{" not in raw:
                path = Path(raw)
                path = path if path.is_absolute() else base / path
                sources.append((str(path), _file_digest(path)))
    env = sorted((key, os.environ.get(key)) for key in _environment_refs(kwargs))
    identity = (step.name, step.kind, step.after, step.args, kwargs, env, sorted(sources))
    return hashlib.sha256(pickle.dumps(identity, protocol=4)).hexdigest()
