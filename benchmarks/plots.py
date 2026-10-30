"""Report figures (PNG) from the experiment rows. Needs the ``bench`` dependency group.

Colours are the validated default categorical palette (slots 1-3: blue, orange,
aqua) on a light surface, with text in neutral inks rather than series colours.
Every chart has a single y axis; series are named in a legend and labelled directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.axes import Axes

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]  # categorical slots 1-3, fixed order
BOUND = "#52514e"

plt.rcParams.update(
    {
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "axes.edgecolor": GRID,
        "axes.labelcolor": INK_2,
        "axes.titlecolor": INK,
        "axes.titleweight": "bold",
        "axes.titlesize": 12,
        "axes.titlelocation": "left",
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "axes.axisbelow": True,
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "xtick.color": INK_2,
        "ytick.color": INK_2,
        "font.size": 10,
        "legend.frameon": False,
        "legend.labelcolor": INK_2,
        "lines.linewidth": 2,
        "lines.markersize": 7,
    }
)


def _save(fig: Any, path: Path) -> Path:
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _label_line_end(ax: Axes, xs: Sequence[float], ys: Sequence[float], text: str) -> None:
    ax.annotate(
        text,
        (xs[-1], ys[-1]),
        xytext=(6, 0),
        textcoords="offset points",
        va="center",
        color=INK_2,
        fontsize=9,
    )


def speedup(rows: list[dict[str, Any]], out: Path) -> Path:
    kinds = [k for k in ("io", "cpu") if any(r["kind"] == k for r in rows)]
    has_dask = any("dask_speedup" in r for r in rows)
    fig, axes = plt.subplots(
        1, len(kinds), figsize=(5.2 * len(kinds), 4), sharey=True, squeeze=False
    )
    for ax, kind in zip(axes[0], kinds, strict=True):
        sub = [r for r in rows if r["kind"] == kind]
        xs = range(len(sub))
        n_series = 2 if has_dask else 1
        width = 0.8 / n_series
        bars = [("MiniFlow", "miniflow_speedup", SERIES[0])]
        if has_dask:
            bars.append(("Dask", "dask_speedup", SERIES[1]))
        for i, (label, key, colour) in enumerate(bars):
            offs = [x - 0.4 + width * (i + 0.5) for x in xs]
            vals = [r[key] for r in sub]
            ax.bar(offs, vals, width, color=colour, label=label, edgecolor=SURFACE, linewidth=2)
            for x, v in zip(offs, vals, strict=True):
                # Inside the bar top, so labels never collide with the bound line.
                inside = v > 0.6
                ax.annotate(
                    f"×{v:.1f}",
                    (x, v),
                    xytext=(0, -11 if inside else 3),
                    textcoords="offset points",
                    ha="center",
                    fontsize=8,
                    fontweight="bold" if inside else "normal",
                    color="white" if inside else INK_2,
                )
        for x, r in zip(xs, sub, strict=True):
            ax.hlines(
                r["level_speedup"],
                x - 0.45,
                x + 0.45,
                colors=BOUND,
                linewidth=2,
                linestyles=(0, (4, 2)),
                label="best possible schedule" if x == 0 else None,
            )
        ax.set_xticks(list(xs), [f"{r['shape']}\n{r['tasks']} tasks" for r in sub])
        workers, usable = sub[0]["workers"], sub[0].get("usable_workers", sub[0]["workers"])
        cores = f", {usable} physical cores" if usable != workers else ""
        ax.set_title(f"{kind}-bound tasks ({workers} workers{cores})")
        ax.grid(axis="x", visible=False)
    axes[0][0].set_ylabel("speedup over sequential (×)")
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=len(labels), bbox_to_anchor=(0.5, 1.0))
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(out / "e1_speedup.png", dpi=160)
    plt.close(fig)
    return out / "e1_speedup.png"


def worker_choice(rows: list[dict[str, Any]], out: Path) -> Path:
    fig, ax = plt.subplots(figsize=(7, 2.8))
    labels = [r["config"] for r in rows]
    vals = [r["wall_s"] for r in rows]
    ax.barh(labels, vals, color=SERIES[0], height=0.55, edgecolor=SURFACE, linewidth=2)
    for y, r in enumerate(rows):
        ax.annotate(
            f"{r['wall_s']:.2f} s  (×{r['speedup']:.1f})",
            (r["wall_s"], y),
            xytext=(5, 0),
            textcoords="offset points",
            va="center",
            color=INK_2,
            fontsize=9,
        )
    ax.invert_yaxis()
    ax.set_xlabel("wall time (s) - lower is better")
    r0 = rows[0]
    ax.set_title(f"Mixed workload: {r0['io_tasks']} io + {r0['cpu_tasks']} cpu tasks")
    ax.grid(axis="y", visible=False)
    ax.set_xlim(0, max(vals) * 1.35)
    return _save(fig, out / "e2_worker_choice.png")


def overhead(rows: list[dict[str, Any]], out: Path) -> Path:
    fig, ax = plt.subplots(figsize=(7, 4))
    xs = [r["tasks"] for r in rows]
    # Fixed slot per entity: MiniFlow blue, Dask orange, MiniFlow-with-file aqua.
    series = [
        ("MiniFlow (in-memory store)", "memory", "miniflow_memory_us_per_task", SERIES[0], "o"),
        ("MiniFlow (SQLite file)", "file", "miniflow_file_us_per_task", SERIES[2], "s"),
    ]
    if all("dask_us_per_task" in r for r in rows):
        series.append(("Dask threaded", "Dask", "dask_us_per_task", SERIES[1], "^"))
    for label, short, key, colour, marker in series:
        ys = [r[key] for r in rows]
        ax.plot(xs, ys, color=colour, marker=marker, label=label)
        _label_line_end(ax, xs, ys, short)
    ax.set_xscale("log")
    ax.set_yscale("log")
    plain = matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}")
    for axis in (ax.xaxis, ax.yaxis):
        axis.set_major_formatter(plain)
        axis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.set_xticks(xs)
    ax.set_xlabel("tasks in graph (empty tasks)")
    ax.set_ylabel("time per task (µs)")
    ax.set_title("Scheduling overhead per task")
    ax.legend(loc="upper right")
    ax.margins(x=0.15)
    return _save(fig, out / "e3_overhead.png")


def _retries(k: int) -> str:
    return f"{k} retry" if k == 1 else f"{k} retries"


def fault_tolerance(rows: list[dict[str, Any]], out: Path) -> list[Path]:
    retries = sorted({r["retries"] for r in rows})
    paths = []
    fig, ax = plt.subplots(figsize=(7, 4.4))
    for colour, k in zip(SERIES, retries, strict=False):
        sub = sorted((r for r in rows if r["retries"] == k), key=lambda r: r["failure_rate"])
        xs = [r["failure_rate"] for r in sub]
        expected = [r["expected_run_success"] for r in sub]
        ax.plot(xs, expected, color=colour, linewidth=1, linestyle=(0, (4, 2)))
        ys = [r["run_success_rate"] for r in sub]
        ax.plot(xs, ys, color=colour, marker="o", linewidth=0, label=f"{_retries(k)}")
        # Direct label where the expected curve crosses 50%: the series are far apart there.
        for (x0, y0), (x1, y1) in zip(
            zip(xs, expected, strict=True), zip(xs[1:], expected[1:], strict=True), strict=False
        ):
            if y0 >= 0.5 > y1:
                xm = x0 + (y0 - 0.5) / (y0 - y1) * (x1 - x0)
                ax.annotate(
                    _retries(k),
                    (xm, 0.5),
                    xytext=(6, 4),
                    textcoords="offset points",
                    color=INK_2,
                    fontsize=9,
                )
                break
    ax.plot(
        [], [], color=INK_2, linewidth=1, linestyle=(0, (4, 2)), label="expected: (1 - p^(r+1))^30"
    )
    ax.set_xlabel("probability that an attempt fails (p)")
    ax.set_ylabel("runs that succeed")
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.set_title(f"Fault tolerance: {rows[0]['runs']} runs of a 30-task DAG per point")
    handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=len(labels), bbox_to_anchor=(0.5, 1.0))
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(out / "e4_fault_success.png", dpi=160)
    plt.close(fig)
    paths.append(out / "e4_fault_success.png")

    fig, ax = plt.subplots(figsize=(7, 4))
    for colour, k in zip(SERIES, retries, strict=False):
        sub = sorted((r for r in rows if r["retries"] == k), key=lambda r: r["failure_rate"])
        xs = [r["failure_rate"] for r in sub]
        ys = [r["attempts_per_executed_task"] for r in sub]
        ax.plot(xs, ys, color=colour, marker="o", label=_retries(k))
        _label_line_end(ax, xs, ys, _retries(k))
    ax.set_xlabel("probability that an attempt fails (p)")
    ax.set_ylabel("attempts per task that ran (mean)")
    ax.set_title("Cost of fault tolerance: extra attempts")
    ax.legend(loc="upper left")
    ax.margins(x=0.12)
    paths.append(_save(fig, out / "e4_fault_attempts.png"))
    return paths


def failure_aware(rows: list[dict[str, Any]], out: Path) -> Path:
    """Worker time per failed run, and successful-run wall time, relative to the default."""
    workloads = list(dict.fromkeys(r["workload"] for r in rows))
    policies = list(dict.fromkeys(r["policy"] for r in rows))
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.4), sharey=True)
    panels = [
        ("failed_worker_s_vs_default", "worker time per failed run"),
        ("ok_wall_s_vs_default", "wall time per successful run"),
    ]
    width = 0.8 / len(policies)
    for ax, (key, title) in zip(axes, panels, strict=True):
        for i, (colour, pol) in enumerate(zip([*SERIES, INK_2], policies, strict=False)):
            ys = [
                next(r[key] for r in rows if r["workload"] == w and r["policy"] == pol)
                for w in workloads
            ]
            xs = [j + (i - (len(policies) - 1) / 2) * width for j in range(len(workloads))]
            ax.bar(xs, ys, width=width, color=colour, label=pol)
        ax.axhline(1.0, color="#999891", linewidth=1, linestyle=(0, (4, 2)))
        ax.set_xticks(range(len(workloads)), workloads)
        ax.set_title(title)
    axes[0].set_ylabel("relative to critical path (default) = 1")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=len(labels), bbox_to_anchor=(0.5, 1.0))
    fig.tight_layout(rect=(0, 0, 1, 0.88))
    fig.savefig(out / "e6_failure_aware.png", dpi=160)
    plt.close(fig)
    return out / "e6_failure_aware.png"
