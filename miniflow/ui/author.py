"""Validated JSON-flow files and block metadata for the local editor."""

from __future__ import annotations

import builtins
import contextlib
import csv
import getpass
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from miniflow.blocks import REQUIRED, all_blocks, load_plugins
from miniflow.flowfile import FlowFileError, load_json_flow
from miniflow.flowtest import run_flow_tests
from miniflow.signature import config_signature
from miniflow.ui.templates import TEMPLATES, build_template

__all__ = ["FlowWorkspace", "WorkspaceError", "block_catalog", "block_group", "flow_diff"]

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")
_VERSION = re.compile(r"\d{8}T\d{6}\d{6}(-\d+)?\Z")
HISTORY = ".history"


def _changes(old: Any, new: Any, prefix: str) -> list[dict[str, Any]]:
    """Field-level differences between two step (or flow) objects."""
    out: list[dict[str, Any]] = []
    old = old if isinstance(old, dict) else {}
    new = new if isinstance(new, dict) else {}
    for key in sorted(set(old) | set(new)):
        field = f"{prefix}{key}"
        a, b = old.get(key), new.get(key)
        if a == b:
            continue
        if key == "settings" and not prefix:
            out += _changes(a, b, "settings.")
        else:
            out.append({"field": field, "old": a, "new": b})
    return out


def flow_diff(old: Any, new: Any) -> dict[str, Any]:
    """What changed from flow document ``old`` to ``new``, step by step.

    ``{"added": [...], "removed": [...], "changed": {step: [{field, old, new}]},
    "flow": [{field, old, new}]}``; editor-only step fields (``position``) are ignored.
    """
    old_steps = old.get("steps", {}) if isinstance(old, dict) else {}
    new_steps = new.get("steps", {}) if isinstance(new, dict) else {}
    changed = {}
    for name in old_steps:
        if name in new_steps:
            fields = [
                c
                for c in _changes(old_steps[name], new_steps[name], "")
                if c["field"] != "position"
            ]
            if fields:
                changed[name] = fields
    rest_old = {k: v for k, v in (old if isinstance(old, dict) else {}).items() if k != "steps"}
    rest_new = {k: v for k, v in (new if isinstance(new, dict) else {}).items() if k != "steps"}
    return {
        "added": [n for n in new_steps if n not in old_steps],
        "removed": [n for n in old_steps if n not in new_steps],
        "changed": changed,
        "flow": _changes(rest_old, rest_new, ""),
    }


def _user() -> str:
    """The account saving the flow (from the OS, not the possibly-wrong USER variable)."""
    if sys.platform != "win32":  # pwd is POSIX-only; the check also lets mypy skip it
        import pwd

        with contextlib.suppress(KeyError):
            return pwd.getpwuid(os.getuid()).pw_name
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return "unknown"


class WorkspaceError(ValueError):
    """An invalid flow name or document in the editor workspace."""


_GROUPS = {
    "data": "Read & write",
    "transform": "Transform",
    "text": "Text & dates",
    "office": "Excel",
    "web": "Messages, AI & cloud",
    "notify": "Email & utilities",
    "databases": "Databases",
    "systems": "System",
}
_BUILTIN = "miniflow.blocks.builtin."


def block_group(ref: str) -> str:
    """Palette heading for a block, from the module it is defined in (``module:qualname``)."""
    module = ref.partition(":")[0]
    if module.startswith(_BUILTIN):
        return _GROUPS.get(module.removeprefix(_BUILTIN), "Other")
    return "Plugins"


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
                "group": block_group(block.ref),
                "description": block.description,
                "settings": settings,
            }
        )
    return result


class FlowWorkspace:
    """Read, validate and atomically save direct child JSON flows of one folder."""

    def __init__(self, folder: str | Path, *, history_limit: int = 100) -> None:
        self.folder = Path(folder).resolve()
        if not self.folder.is_dir():
            raise NotADirectoryError(self.folder)
        self.history_limit = history_limit
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

    def upload_csv(self, filename: str, content: bytes) -> str:
        """Store an uploaded CSV beside flows and return its flow-relative path."""
        if (
            not filename
            or len(filename) > 255
            or "/" in filename
            or "\\" in filename
            or "\x00" in filename
            or not filename.lower().endswith(".csv")
            or not filename[:-4].strip()
        ):
            raise WorkspaceError("choose a CSV file")
        try:
            decoded = content.decode("utf-8-sig")
            header = next(csv.reader(io.StringIO(decoded)), None)
        except (UnicodeDecodeError, csv.Error):
            raise WorkspaceError("CSV file must be UTF-8 text") from None
        if not header or not any(column.strip() for column in header) or "\x00" in decoded:
            raise WorkspaceError("CSV file needs a header row")
        uploads = self.folder / "uploads"
        if uploads.is_symlink():
            raise WorkspaceError("uploads folder cannot be a symlink")
        uploads.mkdir(exist_ok=True)
        if not uploads.is_dir():
            raise WorkspaceError("uploads path is not a folder")
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", filename[:-4]).strip("._-")[:60]
        relative = f"uploads/{uuid.uuid4().hex}_{safe_stem or 'file'}.csv"
        with (self.folder / relative).open("xb") as target:
            target.write(content)
        return relative

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
            "signatures": {n: config_signature(t) for n, t in flow.dag.tasks.items()},
        }

    # --------------------------------------------------------------- history
    def _history(self, name: str) -> Path:
        return self.folder / HISTORY / self._path(name).stem

    def _keep(self, name: str, previous: bytes, modified: float) -> None:
        """Copy the version being replaced into the history folder (lock held)."""
        folder = self._history(name)
        folder.mkdir(parents=True, exist_ok=True)
        meta_file = folder / "current.meta"
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {"saved_at": modified}  # saved before history existed, or by hand
        stamp = time.strftime("%Y%m%dT%H%M%S") + f"{time.time_ns() // 1000 % 1_000_000:06d}"
        vid, n = stamp, 1
        while (folder / f"{vid}.json").exists():
            vid, n = f"{stamp}-{n}", n + 1
        entry = {
            "saved_at": meta.get("saved_at"),
            "user": meta.get("user", ""),
            "message": meta.get("message", ""),
            "flow": json.loads(previous),
        }
        (folder / f"{vid}.json").write_text(json.dumps(entry, ensure_ascii=False), "utf-8")
        files = sorted(folder.glob("*.json"))
        for old in files[: max(0, len(files) - self.history_limit)]:
            old.unlink(missing_ok=True)

    def _entry(self, name: str, vid: str) -> dict[str, Any]:
        if not _VERSION.fullmatch(vid):
            raise WorkspaceError(f"there is no version {vid!r}")
        path = self._history(name) / f"{vid}.json"
        if not path.is_file():
            raise FileNotFoundError(path)
        entry: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return entry

    def versions(self, name: str) -> builtins.list[dict[str, Any]]:
        """Earlier saved versions of a flow, newest first (the current file isn't listed)."""
        folder = self._history(name)
        out = []
        for path in sorted(folder.glob("*.json"), reverse=True) if folder.is_dir() else []:
            if not _VERSION.fullmatch(path.stem):
                continue
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            steps = entry.get("flow", {}).get("steps", {})
            out.append(
                {
                    "id": path.stem,
                    "saved_at": entry.get("saved_at"),
                    "user": entry.get("user", ""),
                    "message": entry.get("message", ""),
                    "steps": len(steps) if isinstance(steps, dict) else 0,
                }
            )
        return out

    def version(self, name: str, vid: str) -> Any:
        return self._entry(name, vid)["flow"]

    def diff(self, name: str, vid: str) -> dict[str, Any]:
        """Changes from version ``vid`` to the current file."""
        return flow_diff(self.version(name, vid), self.read(name))

    def restore(self, name: str, vid: str) -> dict[str, Any]:
        """Make version ``vid`` current again; the replaced version is kept in history."""
        return self.save(name, self.version(name, vid), message=f"restored {vid}")

    def save(self, name: str, document: Any, message: str = "") -> dict[str, Any]:
        path = self._path(name)
        result = self.validate(name, document)
        if not result["valid"]:
            return result
        data = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            previous = path.read_bytes() if path.is_file() else None
            if previous == data:
                return result
            if previous is not None:
                # An old file that isn't valid JSON has nothing useful to keep.
                with contextlib.suppress(ValueError):
                    self._keep(name, previous, path.stat().st_mtime)
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
            folder = self._history(name)
            folder.mkdir(parents=True, exist_ok=True)
            meta = {"saved_at": time.time(), "user": _user(), "message": message}
            (folder / "current.meta").write_text(json.dumps(meta), encoding="utf-8")
        return result

    def test(self, name: str, document: Any) -> builtins.list[dict[str, Any]]:
        return run_flow_tests(self._path(name), document)
