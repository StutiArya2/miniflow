"""Validated JSON-flow files and block metadata for the local editor."""

from __future__ import annotations

import builtins
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

from miniflow.blocks import REQUIRED, all_blocks, load_plugins
from miniflow.flowfile import FlowFileError, load_json_flow
from miniflow.flowtest import run_flow_tests

__all__ = ["FlowWorkspace", "WorkspaceError", "block_catalog"]

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")


class WorkspaceError(ValueError):
    """An invalid flow name or document in the editor workspace."""


def block_catalog() -> list[dict[str, Any]]:
    """Block settings in a form-friendly shape, without secret default values."""
    result: list[dict[str, Any]] = []
    for block in all_blocks():
        settings = []
        for setting in block.settings:
            types = setting.type if isinstance(setting.type, tuple) else (setting.type,)
            item = {
                "name": setting.name,
                "types": [kind.__name__ for kind in types],
                "required": setting.required,
                "help": setting.help,
                "choices": list(setting.choices),
                "secret": setting.secret,
                "file": setting.file,
            }
            if not setting.required and not setting.secret and setting.default is not REQUIRED:
                item["default"] = setting.default
            settings.append(item)
        result.append(
            {
                "name": block.name,
                "kind": block.kind,
                "description": block.description,
                "settings": settings,
            }
        )
    return result


class FlowWorkspace:
    """Read, validate and atomically save direct child JSON flows of one folder."""

    def __init__(self, folder: str | Path) -> None:
        self.folder = Path(folder).resolve()
        if not self.folder.is_dir():
            raise NotADirectoryError(self.folder)
        self._lock = threading.Lock()

    def _path(self, name: str) -> Path:
        if not _NAME.fullmatch(name) or name.startswith("_"):
            raise WorkspaceError(
                "flow name must start with a letter and use letters, numbers, _ or -"
            )
        path = self.folder / f"{name}.json"
        if path.is_symlink() or path.resolve().parent != self.folder:
            raise WorkspaceError("flow must stay inside the configured flows folder")
        return path

    def list(self) -> list[str]:
        return sorted(
            p.stem
            for p in self.folder.glob("*.json")
            if p.is_file()
            and not p.is_symlink()
            and _NAME.fullmatch(p.stem)
            and not p.stem.startswith("_")
        )

    def catalog(self) -> builtins.list[dict[str, Any]]:
        plugins = self.folder / "blocks"
        if plugins.is_dir():
            load_plugins(plugins)
        return block_catalog()

    def read(self, name: str) -> Any:
        path = self._path(name)
        if not path.is_file():
            raise FileNotFoundError(path)
        return json.loads(path.read_text(encoding="utf-8"))

    def validate(self, name: str, document: Any) -> dict[str, Any]:
        path = self._path(name)
        try:
            flow = load_json_flow(path, _source=document, _nested=True)
        except FlowFileError as exc:
            return {"valid": False, "problems": exc.problems}
        return {
            "valid": True,
            "problems": [],
            "steps": [
                {"name": task.name, "kind": task.kind, "after": list(task.after)}
                for task in flow.dag.tasks.values()
            ],
        }

    def save(self, name: str, document: Any) -> dict[str, Any]:
        path = self._path(name)
        result = self.validate(name, document)
        if not result["valid"]:
            return result
        data = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            with tempfile.NamedTemporaryFile(
                dir=self.folder, prefix=".miniflow-", delete=False
            ) as f:
                temporary = Path(f.name)
                try:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
            try:
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return result

    def test(self, name: str, document: Any) -> builtins.list[dict[str, Any]]:
        return run_flow_tests(self._path(name), document)
