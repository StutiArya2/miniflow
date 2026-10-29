# Failure-aware priority and pruning: technical note

Status: opt-in prototype (`Scheduler(priority="failure_aware", prune=True, salvage=True)`,
CLI `--priority failure-aware --prune --salvage`). Code: `miniflow/priority.py`. Tests:
`tests/test_priority.py`. Measurements: sections 6 and 7 of `docs/results/results.md`.

## Problem

A DAG scheduler that orders ready tasks by critical path assumes every task succeeds.
When a task fails for good, its descendants are skipped, but *sibling* work whose only
consumers are those descendants still runs. In the worst case, a critical-path order
puts a cheap, often-failing precondition last, after all the expensive work it gates.

## Mechanism

1. **Learned failure model.** From the attempt log of earlier runs of the same flow,
   estimate each task's per-attempt failure probability `p` (Beta prior, mean 1 %,
   weight of two attempts) and its mean successful duration. With `k` attempts left,
   permanent failure has probability `q = p^k` and expected cost
   `c = duration · (1 − p^k) / (1 − p)`. The estimates keep updating during the run.
2. **Doomed set.** Given the dead tasks `D` (skipped, or failed and required), the
   doomed set is every task outside `D` none of whose downstream paths reaches a sink
   while avoiding `D`. It is computed by walking upstream from `D` with a
   live-children counter, touching only the affected part of the graph.
3. **Fail-fast promotion.** `exposure(t)` is the expected cost of the not-yet-started
   tasks that would become doomed if `t` failed, excluding `t`'s own descendants. A
   task with at least one recorded failure is promoted ahead of all other ready work
   when `q · exposure > (1 − q) · c`, an exchange argument: running it first delays
   other work by at most `c`, and only when it succeeds. Promoted tasks are ordered by
   `q · exposure / c`. Everything else keeps critical-path order with expected costs.
4. **Pruning.** On a permanent failure, waiting tasks (PENDING, READY or in retry
   backoff) in the doomed set are marked SKIPPED with a `pruned:` reason. Sinks are
   never pruned, running tasks are not interrupted, and pruned tasks rerun on resume.

5. **Reuse-aware salvage (adaptive pruning).** Pruning only postpones work when the
   failed run is later resumed: the pruned steps then run inside the resume, on its
   critical path. From the run history, pair each failed run with the next run of the
   same flow and count, per task, how often a result computed in the failed run was
   taken from the cache by the next one. This gives a reuse probability `r` (Beta
   prior, mean 30 %). On a permanent failure, a doomed task with `r ≥ 0.5` (expected
   reuse beats expected waste, `r · c > (1 − r) · c`) is kept, at the back of the ready
   queue behind all live work; the rest are pruned.

The measured effect of 1–4 (section 6) comes from 3 and 4 together. Promotion alone moves
failure detection earlier without saving work; pruning alone finds little left to
prune because the doomed work already ran.

Section 7 measures 5. When failures are resumed, pruning slows the resume by 55 %;
salvage keeps the resume as fast as the default. When failures are abandoned, salvage
saves the same 7 % as pruning. No fixed setting does both. Over a full failure + resume
cycle the total work is the same, because the work is done once either way.

## Limitations

- Gains depend on graph shape: large when cheap, flaky steps can start early beside
  expensive siblings that feed the same join, and small when flaky steps sit right after
  their only input or feed many consumers.
- Needs history: a flow with no recorded failures is scheduled exactly as before.
- Promotion slightly delays expensive steps in runs that succeed (+2 % on the gated
  workload).
- Exposure is recomputed when a task with recorded failures becomes ready, which costs
  time proportional to its descendants and their parents. On a 5,000-task layered graph
  of empty tasks where a quarter of the tasks have failure history, scheduler overhead
  rose from about 145 to 610 µs per task. That is negligible for real work but worth
  optimising (e.g. caching exposure between state changes) for very large graphs.
- Salvage needs history pairing failed runs with what followed. The next run of the
  flow is treated as the follow-up, which can be wrong when unrelated runs interleave.
- Adds two state transitions (`READY → SKIPPED`, `RETRYING → SKIPPED`), used only with
  `prune=True`.

## Before treating this as an invention

This note describes what the code does; it is not a patent analysis. Things to settle
with a patent agent or the university IPR cell first:

- **Inventorship.** The concept should be the team's own. Whoever conceived the
  promotion rule and its combination with pruning, or a better variant of it, should be
  identified, and the design refined by the team.
- **Prior art to search**, at least: HEFT and other list schedulers; fault-tolerant
  and reliability-aware DAG scheduling (e.g. failure-probability-weighted ranks in grid
  and cloud workflow research); sequential testing and the `q / c` ratio rule; Airflow
  trigger rules and `depends_on_past`; Dagster, Prefect and Luigi failure handling;
  Nextflow and Snakemake error strategies; Bazel and Make `--keep-going`; CI systems
  that order or cancel jobs by flakiness history.
- **Technical effect.** Section 6 measures worker time, failed-run wall time and
  time to first failure. A filing would need stronger evidence: more workloads, real
  pipelines and resource measurements beyond sleep-based tasks.
- **Disclosure.** Publishing this repository, a paper or a demo before filing can
  destroy novelty.
