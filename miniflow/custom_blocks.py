"""User-wide, revisioned custom blocks for JSON workflows.

Definitions are data files in the user's MiniFlow data directory. Python block
revisions are immutable importable modules so a running task keeps its code even
when the block is edited in the dashboard.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import hashlib
import importlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from miniflow.blocks import Setting, all_blocks

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_TYPES: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
}


def custom_blocks_root() -> Path:
    """The one library shared by this user's MiniFlow workspaces."""
    override = os.environ.get("MINIFLOW_CUSTOM_BLOCKS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/MiniFlow/custom_blocks"
    if sys.platform == "win32":
        return (
            Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
            / "MiniFlow/custom_blocks"
        )
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    return data / "miniflow/custom_blocks"


class CustomBlockError(ValueError):
    """Invalid definition or missing custom block."""


@contextlib.contextmanager
def _library_lock(root: Path) -> Iterator[None]:
    """Serialize manifest updates across dashboard processes on this computer."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".library.lock").open("a+b") as stream:
        if sys.platform == "win32":
            import msvcrt

            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform == "win32":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class CustomBlockLibrary:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root else custom_blocks_root()
        self.modules = self.root / "modules"
        if str(self.modules) not in sys.path:
            sys.path.insert(0, str(self.modules))

    def _folder(self, name: str) -> Path:
        if not _NAME.fullmatch(name):
            raise CustomBlockError(
                "block name must start with a letter and use letters, numbers or _"
            )
        folder = self.root / name
        if folder.is_symlink():
            raise CustomBlockError("custom block folder cannot be a symlink")
        return folder

    def list(self) -> list[dict[str, Any]]:
        result = []
        for path in sorted(self.root.glob("*/current.json")):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if item.get("name") == path.parent.name:
                    result.append(item)
            except (OSError, ValueError):
                continue
        return result

    def get(self, name: str) -> dict[str, Any]:
        folder = self._folder(name)
        try:
            meta = json.loads((folder / "current.json").read_text(encoding="utf-8"))
            revision = meta["revision"]
            if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{16}", revision):
                raise ValueError("invalid revision")
            definition = json.loads((folder / f"{revision}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError) as exc:
            raise CustomBlockError(f"no custom block called {name!r}") from exc
        if definition.get("name") != name:
            raise CustomBlockError(f"invalid custom block {name!r}")
        return {**definition, "revision": revision}

    def catalog(self) -> builtins.list[dict[str, Any]]:
        result = []
        for meta in self.list():
            try:
                definition = self.get(meta["name"])
            except CustomBlockError:
                continue
            result.append(
                {
                    key: definition[key]
                    for key in ("name", "kind", "description", "inputs", "outputs", "revision")
                }
            )
        return result

    def validate(self, definition: Any) -> dict[str, Any]:
        if not isinstance(definition, dict):
            raise CustomBlockError("custom block must be an object")
        name = definition.get("name")
        if not isinstance(name, str):
            raise CustomBlockError("block name is required")
        self._folder(name)
        kind = definition.get("kind")
        if kind not in ("visual", "python"):
            raise CustomBlockError("kind must be visual or python")
        description = definition.get("description", "")
        if not isinstance(description, str):
            raise CustomBlockError("description must be text")
        inputs = definition.get("inputs", {})
        if not isinstance(inputs, dict):
            raise CustomBlockError("inputs must be an object")
        for key, spec in inputs.items():
            if (
                not isinstance(key, str)
                or not _NAME.fullmatch(key)
                or key in ("params", "env", "failure")
            ):
                raise CustomBlockError(f"invalid input name {key!r}")
            if not isinstance(spec, dict) or spec.get("type", "str") not in _TYPES:
                raise CustomBlockError(f"input {key!r} needs a supported type")
            if set(spec) - {"type", "default", "help", "secret", "file"}:
                raise CustomBlockError(f"input {key!r} has unknown fields")
            if not isinstance(spec.get("help", ""), str):
                raise CustomBlockError(f"input {key!r} help must be text")
            if not isinstance(spec.get("secret", False), bool) or not isinstance(
                spec.get("file", False), bool
            ):
                raise CustomBlockError(f"input {key!r} secret and file must be true or false")
            if spec.get("secret") and "default" in spec:
                raise CustomBlockError(f"secret input {key!r} cannot have a default")
            if "default" in spec:
                problem = Setting(
                    key, _TYPES[spec.get("type", "str")], default=spec["default"]
                ).check(spec["default"])
                if problem:
                    raise CustomBlockError(f"input {key!r}: {problem}")
        outputs = definition.get("outputs")
        if kind == "visual":
            if not isinstance(outputs, dict) or not outputs:
                raise CustomBlockError("a visual block needs at least one named output")
            if not all(isinstance(k, str) and _NAME.fullmatch(k) for k in outputs):
                raise CustomBlockError("output names must use letters, numbers or _")
            if any(value in ("", None) for value in outputs.values()):
                raise CustomBlockError("each visual output needs a value from a step or input")
            steps = definition.get("steps")
            if not isinstance(steps, dict) or not steps:
                raise CustomBlockError("a visual block needs at least one step")
            builtins = {
                b.name for b in all_blocks() if b.ref.startswith("miniflow.blocks.builtin.")
            }
            for step_name, step in steps.items():
                if not isinstance(step, dict) or step.get("block") not in builtins:
                    raise CustomBlockError(f"step {step_name!r} must use a built-in block")
            from miniflow.flowfile import FlowFileError, load_json_flow

            flow = {
                "description": description,
                "params": inputs,
                "steps": steps,
                "outputs": outputs,
            }
            try:
                load_json_flow(self.root / "_custom_validation.json", _source=flow, _nested=True)
            except FlowFileError as exc:
                raise CustomBlockError("; ".join(exc.problems)) from exc
            return {
                "name": name,
                "kind": kind,
                "description": description,
                "inputs": inputs,
                "steps": steps,
                "outputs": outputs,
            }
        if (
            not isinstance(outputs, list)
            or not outputs
            or not all(isinstance(k, str) and _NAME.fullmatch(k) for k in outputs)
            or len(set(outputs)) != len(outputs)
        ):
            raise CustomBlockError("a Python block needs a list of unique output names")
        code = definition.get("code")
        if not isinstance(code, str) or not code.strip():
            raise CustomBlockError("Python code is required")
        execution = definition.get("execution", "io")
        if execution not in ("io", "cpu"):
            raise CustomBlockError("execution must be io or cpu")
        source = self._python_source(name, "validation", inputs, outputs, code, execution)
        try:
            ast.parse(source)
            compile(source, f"<custom block {name}>", "exec")
        except (SyntaxError, ValueError) as exc:
            raise CustomBlockError(f"Python syntax error: {exc}") from exc
        return {
            "name": name,
            "kind": kind,
            "description": description,
            "inputs": inputs,
            "outputs": outputs,
            "code": code,
            "execution": execution,
        }

    @staticmethod
    def _python_source(
        name: str,
        revision: str,
        inputs: dict[str, Any],
        outputs: builtins.list[str],
        code: str,
        execution: str,
    ) -> str:
        args = ", ".join(inputs)
        body = "\n".join("    " + line if line else "" for line in code.splitlines())
        settings = []
        for key, spec in inputs.items():
            typ = spec.get("type", "str")
            extras = ", ".join(
                f"{field}={spec[field]!r}"
                for field in ("default", "help", "secret", "file")
                if field in spec
            )
            settings.append(f"Setting({key!r}, {typ}{', ' + extras if extras else ''})")
        internal = f"miniflow_custom_{name}_{revision}"
        return (
            "from miniflow.blocks import Setting, block\n\n"
            f"def _implementation({args}):\n{body}\n\n"
            f"@block({internal!r}, settings=[{', '.join(settings)}], kind={execution!r})\n"
            "def execute(**kwargs):\n"
            "    result = _implementation(**kwargs)\n"
            "    if not isinstance(result, dict):\n"
            "        raise ValueError('custom block must return an object of named outputs')\n"
            f"    missing = set({outputs!r}) - result.keys()\n"
            "    if missing:\n"
            "        raise ValueError('custom block is missing outputs: ' + "
            "', '.join(sorted(missing)))\n"
            "    return result\n"
        )

    def save(self, definition: Any, expected_revision: str | None = None) -> dict[str, Any]:
        clean = self.validate(definition)
        with _library_lock(self.root):
            return self._save_validated(clean, expected_revision)

    def _save_validated(
        self, clean: dict[str, Any], expected_revision: str | None
    ) -> dict[str, Any]:
        name = clean["name"]
        folder = self._folder(name)
        previous = None
        if (folder / "current.json").exists():
            previous = self.get(name)["revision"]
        if expected_revision != previous:
            raise CustomBlockError("this block changed since you opened it; reload before saving")
        raw = json.dumps(clean, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        revision = hashlib.sha256(raw).hexdigest()[:16]
        folder.mkdir(parents=True, exist_ok=True)
        if clean["kind"] == "python":
            self.modules.mkdir(parents=True, exist_ok=True)
            module = f"miniflow_custom_{name}_{revision}"
            module_path = self.modules / f"{module}.py"
            if not module_path.exists():
                module_path.write_text(
                    self._python_source(
                        name,
                        revision,
                        clean["inputs"],
                        clean["outputs"],
                        clean["code"],
                        clean["execution"],
                    ),
                    encoding="utf-8",
                )
            try:
                importlib.invalidate_caches()
                importlib.import_module(module)
            except Exception as exc:
                raise CustomBlockError(
                    f"could not load Python block: {type(exc).__name__}: {exc}"
                ) from exc
        revision_path = folder / f"{revision}.json"
        if not revision_path.exists():
            revision_path.write_bytes(raw)
        meta = {
            "name": name,
            "kind": clean["kind"],
            "description": clean["description"],
            "revision": revision,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", dir=folder, prefix=".current-", suffix=".json", delete=False, encoding="utf-8"
        ) as stream:
            temporary = Path(stream.name)
            json.dump(meta, stream)
        temporary.replace(folder / "current.json")
        return {**clean, "revision": revision}

    def python_spec(self, definition: dict[str, Any]) -> tuple[str, str]:
        if definition["kind"] != "python":
            raise CustomBlockError("not a Python block")
        internal = f"miniflow_custom_{definition['name']}_{definition['revision']}"
        try:
            importlib.import_module(internal)
        except Exception as exc:
            raise CustomBlockError(
                f"could not load Python block {definition['name']!r}: {type(exc).__name__}: {exc}"
            ) from exc
        return internal, f"{internal}:execute"
