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
from miniflow.ui.templates import TEMPLATES, build_template

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

    def environments(self) -> builtins.list[str]:
        """Only names are sent to the browser; environment values stay on disk."""
        folder = self.folder / "environments"
        return sorted(
            p.stem
            for p in folder.glob("*.json")
            if p.is_file() and not p.is_symlink() and _NAME.fullmatch(p.stem)
        )

    def subflows(self) -> builtins.list[dict[str, Any]]:
        """Reusable direct-child flows with declared settings for the palette."""
        result: builtins.list[dict[str, Any]] = []
        for name in self.list():
            try:
                document = self.read(name)
            except (OSError, ValueError):
                continue
            if isinstance(document, dict):
                params = document.get("params", {})
                result.append(
                    {
                        "name": name,
                        "description": str(document.get("description", "")),
                        "params": params if isinstance(params, dict) else {},
                    }
                )
        return result

    def templates(self) -> builtins.list[dict[str, str]]:
        return [dict(item) for item in TEMPLATES]

    def create_template(self, template: str, name: str) -> dict[str, Any]:
        """Create a starter flow and its local sample data without overwriting files."""
        path = self._path(name)
        if path.exists():
            raise WorkspaceError(f"flow {name!r} already exists")
        try:
            document, assets = build_template(template, name)
        except KeyError:
            raise WorkspaceError(f"unknown template {template!r}") from None
        created: builtins.list[Path] = []
        try:
            for relative, content in assets.items():
                asset = self.folder / relative
                if not asset.resolve().is_relative_to(self.folder) or asset.is_symlink():
                    raise WorkspaceError("template data must stay inside the flows folder")
                asset.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with asset.open("x", encoding="utf-8") as file:
                        file.write(content)
                except FileExistsError:
                    raise WorkspaceError(f"sample data {relative!r} already exists") from None
                created.append(asset)
            result = self.save(name, document)
            if not result["valid"]:
                raise WorkspaceError("template failed validation: " + "; ".join(result["problems"]))
        except BaseException:
            for asset in created:
                asset.unlink(missing_ok=True)
            raise
        return document

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
