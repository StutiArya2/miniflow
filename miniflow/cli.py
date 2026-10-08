"""MiniFlow command line: run, resume, status, graph and validate a flow file."""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import shutil
import subprocess
import sys
import time
import types
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

from miniflow.executors import ExecutorRouter
from miniflow.graph import DAG, GraphError
from miniflow.retry import RetryPolicy
from miniflow.scheduler import Event, RunResult, Scheduler
from miniflow.states import State
from miniflow.store import StateStore
from miniflow.ui.server import DashboardServer

__all__ = ["entry", "load_flow", "main"]

DEFAULT_DB = "miniflow.db"

# Set when a run abandoned timed-out threads/processes. Python would wait for them
# at exit, so the console entry point exits hard once everything is saved.
_abandoned_work = False

_SYMBOLS = {
    State.PENDING: "·",
    State.READY: "○",
    State.RUNNING: "▶",
    State.RETRYING: "↻",
    State.SUCCESS: "✓",
    State.FAILED: "✗",
    State.SKIPPED: "-",
}


def load_flow(path: str | Path) -> tuple[types.ModuleType, DAG]:
    """Import a flow file and build its DAG from the module-level tasks."""
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(p)
    name = p.stem
    spec = importlib.util.spec_from_file_location(name, p)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import {p}")
    module = importlib.util.module_from_spec(spec)
    # Registered (and its folder put on sys.path, which spawned children inherit)
    # so process workers can re-import cpu tasks by module name.
    sys.modules[name] = module
    if str(p.parent) not in sys.path:
        sys.path.insert(0, str(p.parent))
    spec.loader.exec_module(module)
    return module, DAG.from_module(module)


# --------------------------------------------------------------------- output
def _symbol(state: State, out: TextIO) -> str:
    symbol = _SYMBOLS[state]
    try:
        symbol.encode(out.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return state.value[0]
    return symbol


class PlainReporter:
    """One line per state change. Used when ``rich`` is missing or output isn't a terminal."""

    def __init__(self, dag: DAG, out: TextIO | None = None, verbose: bool = False) -> None:
        self.out = out or sys.stdout
        self.verbose = verbose
        self.width = max((len(n) for n in dag.tasks), default=4)
        self.t0 = time.time()

    def __call__(self, e: Event) -> None:
        if not self.verbose and e.state in (State.PENDING, State.READY):
            return
        parts = [
            f"[{e.time - self.t0:7.2f}s]",
            _symbol(e.state, self.out),
            f"{e.task:<{self.width}}",
        ]
        parts.append(f"{e.state.value:<8}")
        if e.from_cache:
            parts.append("(reused from previous run)")
        elif e.state is State.RUNNING:
            parts.append(f"attempt {e.attempt}")
        elif e.worker:
            parts.append(f"on {e.worker}")
        if e.error:
            parts.append(f"- {e.error}")
        if e.delay is not None:
            parts.append(f"(retry in {e.delay:.2f}s)")
        print(" ".join(parts), file=self.out, flush=True)

    def __enter__(self) -> PlainReporter:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class RichReporter:
    """Live-updating table (needs the optional ``rich`` extra)."""

    def __init__(self, dag: DAG) -> None:
        from rich.console import Console
        from rich.live import Live

        self.dag = dag
        self.rows: dict[str, dict[str, Any]] = {
            n: {"state": State.PENDING, "attempt": 0, "worker": "", "note": "", "start": None}
            for n in dag.topological_order()
        }
        self.t0 = time.time()
        self.live = Live(self._table(), console=Console(), refresh_per_second=8, transient=False)

    def _table(self) -> Any:
        from rich.table import Table

        colours = {
            State.PENDING: "dim",
            State.READY: "yellow",
            State.RUNNING: "cyan",
            State.RETRYING: "dark_orange",
            State.SUCCESS: "green",
            State.FAILED: "red",
            State.SKIPPED: "grey50",
        }
        done = sum(
            r["state"] in (State.SUCCESS, State.FAILED, State.SKIPPED) for r in self.rows.values()
        )
        table = Table(
            title=f"MiniFlow · {done}/{len(self.rows)} done · {time.time() - self.t0:.1f}s"
        )
        for col in ("task", "kind", "state", "attempt", "worker", "note"):
            table.add_column(col)
        for name, r in self.rows.items():
            c = colours[r["state"]]
            table.add_row(
                name,
                self.dag.tasks[name].kind,
                f"[{c}]{_SYMBOLS[r['state']]} {r['state'].value}[/]",
                str(r["attempt"] or ""),
                r["worker"],
                r["note"],
            )
        return table

    def __call__(self, e: Event) -> None:
        row = self.rows[e.task]
        row["state"] = e.state
        row["attempt"] = e.attempt or row["attempt"]
        if e.worker:
            row["worker"] = e.worker
        if e.from_cache:
            row["note"] = "reused from previous run"
        elif e.error:
            row["note"] = e.error + (f" (retry in {e.delay:.2f}s)" if e.delay is not None else "")
        elif e.state is State.SUCCESS:
            row["note"] = ""
        self.live.update(self._table())

    def __enter__(self) -> RichReporter:
        self.live.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.live.update(self._table())
        self.live.stop()


def _reporter(dag: DAG, args: argparse.Namespace) -> PlainReporter | RichReporter | None:
    if args.quiet:
        return None
    if not args.plain and sys.stdout.isatty():
        try:
            return RichReporter(dag)
        except ImportError:
            pass
    return PlainReporter(dag, verbose=args.verbose)


def _summary(result: RunResult, out: TextIO | None = None) -> None:
    out = out or sys.stdout
    counts: dict[State, int] = {}
    for s in result.states.values():
        counts[s] = counts.get(s, 0) + 1
    parts = [f"{n} {s.value.lower()}" for s, n in sorted(counts.items())]
    if result.from_cache:
        parts.append(f"{len(result.from_cache)} reused")
    print(
        f"\nrun {result.run_id}: {result.status} in {result.duration:.2f}s ({', '.join(parts)})",
        file=out,
    )
    for name, err in result.errors.items():
        if result.states[name] is State.FAILED:
            print(f"  {name}: {err}", file=out)
    if result.status != "SUCCESS":
        print(f"resume with: miniflow resume <flow> --run {result.run_id}", file=out)


# ------------------------------------------------------------------- commands
def _start_dashboard(args: argparse.Namespace, allow_run: bool = False) -> DashboardServer:
    server = DashboardServer(
        args.db, port=args.port, allow_run=allow_run, offline=getattr(args, "offline", False)
    ).start()
    print(f"dashboard: {server.url}", file=sys.stderr, flush=True)
    if not args.no_browser:
        webbrowser.open(server.url)
    return server


def _serve_until_interrupted(server: DashboardServer) -> None:
    print(f"dashboard running at {server.url} - press Ctrl+C to stop", file=sys.stderr, flush=True)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


def _load(args: argparse.Namespace) -> tuple[DAG, str]:
    """The DAG and flow name for a flow file, or for a bundled example (``miniflow example``)."""
    if getattr(args, "example", False):
        from miniflow.examples import load

        return load(args.flow)[1], args.flow
    return load_flow(args.flow)[1], Path(args.flow).stem


def _execute(args: argparse.Namespace, resume: bool) -> int:
    if getattr(args, "offline", False):
        os.environ["MINIFLOW_OFFLINE"] = "1"  # read by the examples; inherited by workers
    dag, flow = _load(args)
    server = _start_dashboard(args) if args.ui else None
    try:
        return _execute_with(args, resume, dag, flow, server)
    finally:
        if server is not None:
            server.stop()


def _execute_with(
    args: argparse.Namespace, resume: bool, dag: DAG, flow: str, server: DashboardServer | None
) -> int:
    with StateStore(args.db) as store:
        previous = None
        if resume:
            previous = args.run or store.latest_run(flow)
            if previous is None:
                print(f"error: no earlier run of {flow!r} in {args.db}", file=sys.stderr)
                return 2
        router = ExecutorRouter(io_workers=args.io_workers, cpu_workers=args.cpu_workers)
        reporter = _reporter(dag, args)
        scheduler = Scheduler(
            dag,
            router=router,
            store=store,
            policy=RetryPolicy(base=args.retry_base, cap=args.retry_cap),
            flow=flow,
            listeners=[reporter] if reporter else [],
        )
        try:
            if reporter:
                with reporter:
                    result = scheduler.run(resume_from=previous)
            else:
                result = scheduler.run(resume_from=previous)
        finally:
            # Abandoned (timed-out) threads or processes would block a waiting shutdown.
            router.shutdown(wait=not scheduler.abandoned_work, cancel_futures=True)
    if scheduler.abandoned_work:
        global _abandoned_work
        _abandoned_work = True
        if not args.quiet:
            print("note: timed-out tasks were abandoned while still running", file=sys.stderr)
    if not args.quiet:
        _summary(result)
    if server is not None and args.ui_wait:
        _serve_until_interrupted(server)
    return {"SUCCESS": 0, "FAILED": 1}.get(result.status, 130)


def _cmd_run(args: argparse.Namespace) -> int:
    return _execute(args, resume=False)


def _cmd_resume(args: argparse.Namespace) -> int:
    return _execute(args, resume=True)


def _last_line(text: str | None) -> str:
    lines = (text or "").strip().splitlines()
    return lines[-1] if lines else ""


def _fmt_time(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"


def _cmd_status(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        print(f"error: no database at {args.db}", file=sys.stderr)
        return 2
    with StateStore(args.db) as store:
        if args.all:
            runs = store.list_runs()
            if not runs:
                print("no runs yet")
            print(f"{'run':<14}{'flow':<20}{'status':<13}{'started':<21}duration")
            for r in runs:
                dur = f"{r.finished_at - r.started_at:.2f}s" if r.finished_at else "-"
                print(f"{r.run_id:<14}{r.flow:<20}{r.status:<13}{_fmt_time(r.started_at):<21}{dur}")
            return 0
        run_id = args.run or store.latest_run(args.flow)
        if run_id is None:
            print("no runs yet")
            return 0
        meta = next(r for r in store.list_runs() if r.run_id == run_id)
        graph = store.get_graph(run_id)["tasks"]
        records = {r.task: r for r in store.changes_since(run_id, 0.0)}
        print(
            f"run {run_id} · flow {meta.flow} · {meta.status}"
            f" · started {_fmt_time(meta.started_at)}\n"
        )
        width = max((len(n) for n in graph), default=4)
        print(f"{'task':<{width + 2}}{'kind':<7}{'state':<10}{'tries':<7}{'time':<9}note")
        for name, info in graph.items():
            rec = records[name]
            took = (
                f"{rec.finished_at - rec.started_at:.2f}s"
                if rec.started_at and rec.finished_at
                else "-"
            )
            note = "reused" if rec.from_cache else _last_line(rec.error)
            sym = _symbol(rec.state, sys.stdout)
            print(
                f"{name:<{width + 2}}{info['kind']:<7}{sym} {rec.state.value:<8}"
                f"{rec.attempts:<7}{took:<9}{note}"
            )
    return 0


def _cmd_graph(args: argparse.Namespace) -> int:
    _, dag = load_flow(args.flow)
    states: dict[str, str] | None = None
    if args.db and Path(args.db).exists():
        with StateStore(args.db) as store:
            run_id = args.run or store.latest_run(Path(args.flow).stem)
            if run_id:
                states = {k: v.value for k, v in store.get_states(run_id).items()}
    dot = dag.to_dot(states)
    if args.render:
        return _render(dot, args.render, args.output)
    if args.output:
        Path(args.output).write_text(dot, encoding="utf-8")
    else:
        sys.stdout.write(dot)
    return 0


def _render(dot: str, fmt: str, output: str | None) -> int:
    target = Path(output or f"flow.{fmt}")
    try:
        import graphviz  # optional extra: miniflow[viz]

        graphviz.Source(dot).render(outfile=str(target), format=fmt, cleanup=True)
    except ImportError:
        exe = shutil.which("dot")
        if exe is None:
            print(
                "error: rendering needs Graphviz (`pip install miniflow[viz]` and the dot binary)",
                file=sys.stderr,
            )
            return 2
        subprocess.run([exe, f"-T{fmt}", "-o", str(target)], input=dot, text=True, check=True)
    print(f"wrote {target}")
    return 0


def _cmd_example(args: argparse.Namespace) -> int:
    return _execute(args, resume=args.resume)


def _cmd_examples(args: argparse.Namespace) -> int:
    from miniflow.examples import catalog

    for ex in catalog():
        k = ex["kinds"]
        print(f"{ex['name']:<13} {ex['title']}")
        print(f"{'':<13} {ex['tasks']} steps ({k['io']} io, {k['cpu']} cpu, {k['async']} async)")
        print(f"{'':<13} miniflow example {ex['name']} --ui\n")
    return 0


def _cmd_ui(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        print(
            f"note: {args.db} doesn't exist yet; runs will appear once you start one",
            file=sys.stderr,
        )
    server = _start_dashboard(args, allow_run=not args.read_only)
    try:
        _serve_until_interrupted(server)
    finally:
        server.stop()
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    _, dag = load_flow(args.flow)
    order = dag.topological_order()
    print(f"OK: {len(dag)} tasks, order: {' -> '.join(order)}")
    return 0


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="miniflow", description=__doc__)
    parser.add_argument("--log-level", default="WARNING", help="Python logging level")
    sub = parser.add_subparsers(dest="command", required=True)

    def run_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("flow", help="path to a flow file (for `example`: the example's name)")
        p.add_argument("--db", default=DEFAULT_DB, help=f"state database (default {DEFAULT_DB})")
        p.add_argument("--io-workers", type=int, default=8, help="thread pool size")
        p.add_argument(
            "--cpu-workers", type=int, default=None, help="process pool size (default: CPU count)"
        )
        p.add_argument(
            "--retry-base", type=float, default=0.1, help="first backoff delay in seconds"
        )
        p.add_argument("--retry-cap", type=float, default=30.0, help="maximum backoff delay")
        p.add_argument(
            "--plain", action="store_true", help="line-by-line output instead of a live table"
        )
        p.add_argument(
            "-v", "--verbose", action="store_true", help="also show PENDING/READY changes"
        )
        p.add_argument("-q", "--quiet", action="store_true", help="no progress output")
        p.add_argument(
            "--offline", action="store_true", help="examples: use saved real data, no internet"
        )
        p.add_argument("--ui", action="store_true", help="serve the live dashboard during the run")
        p.add_argument(
            "--ui-wait",
            action=argparse.BooleanOptionalAction,
            default=True,
            help="with --ui, keep the dashboard up after the run until Ctrl+C",
        )
        dashboard_options(p)

    def dashboard_options(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--port", type=int, default=8765, help="dashboard port (next free one if taken)"
        )
        p.add_argument("--no-browser", action="store_true", help="don't open a browser tab")

    r = sub.add_parser("run", help="run a flow")
    run_options(r)
    r.set_defaults(func=_cmd_run)

    rs = sub.add_parser("resume", help="rerun a flow, reusing results that already succeeded")
    run_options(rs)
    rs.add_argument("--run", help="run id to resume (default: latest run of this flow)")
    rs.set_defaults(func=_cmd_resume)

    st = sub.add_parser("status", help="show the tasks of a run, or all runs")
    st.add_argument("flow", nargs="?", help="limit to this flow name")
    st.add_argument("--db", default=DEFAULT_DB)
    st.add_argument("--run", help="run id (default: latest)")
    st.add_argument("--all", action="store_true", help="list all runs")
    st.set_defaults(func=_cmd_status)

    g = sub.add_parser("graph", help="export the DAG in Graphviz DOT, coloured by the latest run")
    g.add_argument("flow", help="path to a Python file defining tasks")
    g.add_argument("-o", "--output", help="write to this file instead of stdout")
    g.add_argument("--db", default=DEFAULT_DB, help="colour nodes by states from this database")
    g.add_argument("--run", help="run id for colours (default: latest run of this flow)")
    g.add_argument("--render", choices=["png", "svg", "pdf"], help="render an image with Graphviz")
    g.set_defaults(func=_cmd_graph)

    ex = sub.add_parser("example", help="run a bundled real-world example pipeline")
    run_options(ex)
    ex.add_argument("--resume", action="store_true", help="reuse results of the latest run")
    ex.set_defaults(func=_cmd_example, example=True, run=None)

    exs = sub.add_parser("examples", help="list the bundled example pipelines")
    exs.set_defaults(func=_cmd_examples)

    u = sub.add_parser("ui", help="serve the dashboard for a state database")
    u.add_argument("--db", default=DEFAULT_DB, help=f"state database (default {DEFAULT_DB})")
    u.add_argument(
        "--read-only", action="store_true", help="hide the Run buttons of the Examples gallery"
    )
    u.add_argument(
        "--offline", action="store_true", help="examples started from the page use saved data"
    )
    dashboard_options(u)
    u.set_defaults(func=_cmd_ui)

    v = sub.add_parser("validate", help="check the DAG and print a topological order")
    v.add_argument("flow")
    v.set_defaults(func=_cmd_validate)
    return parser


def entry() -> None:
    """Console-script entry point (``miniflow`` / ``python -m miniflow``)."""
    code = main()
    if _abandoned_work:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    sys.exit(code)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")
    func: Callable[[argparse.Namespace], int] = args.func
    try:
        return int(func(args))
    except GraphError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
