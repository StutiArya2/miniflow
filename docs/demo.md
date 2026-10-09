# Demo script: Ocean Watch

A 10-minute walkthrough that shows what MiniFlow does on a realistic job: scanning
240 satellite radar images of the sea for **oil spills** and the **ships** that may have
caused them. Everything is driven from the dashboard; one comparison uses the terminal.

## Before class (once)

```bash
cd ~/Projects/miniflow
uv sync --all-extras
MINIFLOW_FAIL_RATE=0 uv run miniflow run examples/ocean_watch.py   # ~10 s: makes the 240 tiles once
uv run miniflow ui --flows examples                                   # opens the dashboard
```

Keep `examples/ocean_watch_data/report.html` ready to open, and `examples/ocean_watch.py`
open in an editor.

## 1. The problem (1 min)

> "A radar satellite has photographed 240 patches of sea, each 5 × 5 km. Oil calms the
> waves, so a spill shows up as a dark patch; a ship's hull shows up as a bright dot. We
> need to download every image, scan each one, and tell the coast guard which areas to
> check, especially spills with a ship right next to them, because that ship may be the
> polluter."

## 2. The flow is plain Python (1 min)

Open `examples/ocean_watch.py` and point out:

- `@task(...)` turns a function into a step. `after=[...]` says what it waits for.
- `kind="io"` for downloads (mostly waiting), `kind="cpu"` for detection (number crunching),
  `kind="async"` for the alert.
- `retries=3` on downloads, because satellite links drop.
- 28 steps: `prepare_tiles` → 12 × `download` → 12 × `detect` → `combine` → `write_report`
  and `alert_coast_guard`. MiniFlow works out the order and what can run at the same time.

## 3. Run it (2 min)

In the dashboard pick **ocean_watch**, set **Simulate failures** to 20 % and press **Run**. Point at:

- **Graph** tab: the columns fill left to right; nothing starts before what it depends on.
- **Timeline** tab: downloads arrive in two waves (8 threads for 12 downloads); each
  `detect` runs in its own `pid-…` lane, so they truly run at the same time on separate cores.
- **Orange / dotted lines**: a download timed out and was retried after a short wait.
- **Parallel speedup** at the top of the Timeline tab (about ×7): total work time divided by wall-clock time.

Then open `report.html`: the accuracy cards (every real ship and spill was found, no false
alarms; we know because the tiles are synthetic and record the right answers) and the alert
images, where red = spill, green = ship, yellow = ship within 500 m of a spill.

## 4. Why processes: the speedup (2 min)

Run the same job with a single worker process for detection, in a terminal:

```bash
uv run miniflow run examples/ocean_watch.py --cpu-workers 1
```

It takes about 24 s instead of about 6 s. Show both runs in **Recent runs** in the sidebar. Talking point:
Python threads can't run Python code on two cores at once (the GIL), so CPU-heavy steps go
to separate processes, while waiting steps share cheap threads.

## 5. Failures don't sink the job (2 min)

Set **Simulate failures** to 70 % and press **Run**. Usually 2 to 5 of the 12 downloads still fail
after 3 retries (press Run again if none do). Point at:

- the failed download in red, and everything that depends on it **skipped** (grey, dashed),
  while the other batches carry on;
- the run ends **failed**, with the reason under **What went wrong** and in **Step details** (click the red box).

Now set the slider to 0 % and press **Resume this run**. Only the failed batches and the
steps after them run again; everything else is marked **reused**.

## 6. Crash recovery (1 min)

Press **Run**, wait until a few `detect` boxes are green, then press **■ Cancel**. The run
ends **interrupted**, just like pressing Ctrl+C or a power cut. Press **↻ Resume this run**:
the finished batches are reused, because every result was saved to `miniflow.db` (SQLite)
as soon as it was ready.

## Likely questions

| Question | Answer |
|---|---|
| Is the data real? | Synthetic tiles that behave like radar images (speckle, dark slicks, bright ships). That lets us measure accuracy against known answers. Real Sentinel-1 data would need extra libraries, and the project's rule is standard library only. |
| How does it decide what runs first? | Ready steps are queued by the length of the longest chain of work still behind them (critical path), so steps that hold up the most work start first. |
| What if a step hangs? | Steps can have a `timeout`; see `examples/faulty_etl.py`, where `slow_source` hangs once and is retried. |
| How does Resume know what is done? | Every state change and result is written to SQLite; resume loads the successful results of the earlier run and only schedules the rest. |
| Where would this run for real? | On a server, started nightly by cron or when new images arrive; `miniflow run` exits with 1 on failure so the scheduler can raise an alarm. |
