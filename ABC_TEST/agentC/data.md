# Agent C — data source

**Source:** identical to Agent A.

This variant deliberately holds the data axis constant — same Reppo CSV,
same schema, same `preprocess_signals` pipeline, same prepended signal
table block in the system prompt. See `../agentA/data.md` for the full
description.

## Why identical

If A and C diverge in P&L, the **only** thing that differs is Phase 2
entry behaviour (see `strategy.md` in this directory). Differences in
data quality, freshness, schema interpretation, or scoring constants
would otherwise confound the strategy axis.

If you find yourself wanting to tune the data side (e.g. different
`SIGNAL_HALFLIFE_INTERACTIONS`, different `classify_theme` regexes), that
is a **separate axis** — name it `agent_variant="D"` and document it as
its own pack rather than mixing it into C.

## Notes for the experiment

- The handler for this variant should read the **same** S3 key /
  `FEEDBACK_CSV_PATH` as Agent A.
- DDB rows must carry `agent_variant="C"` (or `"C1"` / `"C2"` / `"C3"` —
  see `strategy.md`).
