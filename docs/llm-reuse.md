# Downstream-verified tolerant reuse of LLM answers: technical note

Status: opt-in prototype, the `llm_map` block with `"reuse": "tolerant"`. Code:
`miniflow/blocks/builtin/llm.py`. Tests: `tests/test_llm.py`. Example:
`examples/ticket_triage.json`. Measurements: section 8 of `docs/results/results.md`.

## Problem

A flow that runs every day asks a model about rows that are mostly the same as
yesterday's. Exact caching reuses identical inputs only, but rows are often edited
in ways that don't matter: a typo fix, a greeting, a note line. Similarity caching
(reuse when the input is "close enough") saves more calls but has no way to know when a
small difference matters. A verified semantic cache can learn its threshold by
checking whether the step's own answer changes. For free-text steps such as summaries
that check fails every time, because a model words its answer differently on every
call, even for identical input.

## Mechanism

1. **Near-match lookup.** For a row with no exact match, find the earlier answer for
   the same `key` (or among recent rows) and measure text similarity. Similarity falls
   into a band: ≥0.98, ≥0.95, ≥0.9 or ≥`min_similarity`.
2. **Checks judged downstream.** A check asks the model anyway and compares the
   result with the answer that would have been reused:
   - a step with `choices` (a classifier) compares its own two answers;
   - a free-text step records the pair as *pending*. When the next `llm_map` step that
     reads that column processes the row, it compares its fresh answer with its own
     stored answer for the reused text. Two summaries that word things differently
     but lead to the same label count as *no change*.
3. **Noise floor.** A share of exact matches is also re-asked and judged the same way.
   This measures how often the outcome changes when the input didn't change at all:
   the model's own run-to-run variation.
4. **Trust rule.** A band is trusted when the 95 % upper bound (Wilson) on its change
   rate is at most the noise floor plus `max_change_rate`. A near match in a trusted
   band reuses the earlier answer, except for a share `verify_rate` that is still
   checked so the estimate follows drift. In an untrusted band every near match is
   checked, which costs the same as not reusing.

## What was measured

On the simulated model (section 8):

- **Downstream judging is the key part.** Downstream-verified reuse needed 9–13 % fewer
  calls than exact caching. Judging the summary by its own text gave no saving.
- **Accuracy depends on the workload.** On small edits, wrong labels stayed at the
  exact-caching level. On note edits they rose from 3.3 % to 5.0 %.
- **A fixed 0.95 threshold beat it on one workload** (small edits) and saved little on
  the other (note edits).

## Limitations

- Results come from a simulated, keyword-based model. Real models and real edits will
  behave differently.
- Checks cost model calls. With a confidence bound and a 5 % check rate, learning takes
  several days of traffic.
- Similarity is a character-level `difflib` ratio (the standard library only). When
  harmless and meaningful edits are equally similar, no band can separate them, and an
  answer reused wrongly persists until the row changes again.
- Downstream judging needs a later `llm_map` step that reads the column. A free-text
  step whose output only goes to non-LLM steps or files is never judged, so it never
  reuses tolerantly.
- The chain is judged one step ahead, not end to end.

## Before treating this as an invention

The closest work found (see the patentability assessment):

- *Exact match only:* build-system early cutoff (Mokhov et al., 2018) and execution
  lineage (2026).
- *No learned downstream judgement:* LLM caches Mnimi, Cache Saver, StepCache and AI21's
  pipeline caching.
- *Verified semantic caching:* learns thresholds against the step's own answers.

Judging reuse of one step by the next step's outcome, against a measured noise floor,
was not found in that search. Recent filings are not visible for about 18 months, the
search was not professional, and the design was produced by Claude, so the team should
develop its own version before considering a filing.
