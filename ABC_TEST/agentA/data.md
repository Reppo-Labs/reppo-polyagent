# Agent A — data source

**Source:** Reppo crowd feedback CSV (the schema defined in `data-assets/feedback.sample.csv`,
fixed and shared across deployments).

**Pipeline:**

```
S3 (or FEEDBACK_CSV_PATH) → agent/signals.py:preprocess_signals(csv)
                          → topic-level signal table (plain text)
                          → prepended to system prompt above the strategy
```

## What the model actually sees

For each topic with `interactions ≥ MIN_INTERACTIONS` (default **3**) and
non-zero total voting power, the signal table contributes one block:

```
Topic: "<pod_name>"
  theme=<bucket>
  crowd_direction=YES|NO
  weighted_score=<-1.00..+1.00>
  max_conviction=<0.00..1.00>
  interactions=<int>
  comment: "<first non-empty feedback row, if any>"
```

`weighted_score = direction × confidence`, where:
- `direction = (up_vp − down_vp) / total_vp`
- `confidence = 1 − exp(-interactions / SIGNAL_HALFLIFE_INTERACTIONS)` (default halflife **10**)

`theme` is computed by `agent/signals.py:classify_theme()` from the pod name
(regex-based bucketer; `"other"` when nothing matches).

## CSV columns consumed

| Column | Purpose |
|---|---|
| `name` | Pod / topic headline (group-by key). |
| `up_vote` | `True`/`False` (case-insensitive). |
| `votes` | Voting power this voter staked on this topic. |
| `voting_power` | Voter's total available voting power (denominator for `max_conviction`). |
| `feedback` | Optional free-text comment — first non-empty wins. |
| `status` | Only `ACTIVE` rows are aggregated; `DRAFT` is skipped. |

Extra columns from full Reppo dumps (`id`, `pod_id`, `description`, `url`, …)
are ignored — `csv.DictReader` only reads the keys above.

## Refresh cadence

A new CSV is uploaded to S3 between Lambda runs (manual via
`scripts/upload_csv.py` or operator's pipeline). Each agent invocation reads
the latest snapshot fresh; there is no in-process caching.

## Notes for the experiment

- This is the **control** data source. Variant A and Variant C both consume
  it; only Variant B replaces it (see `../agentB/data.md`).
- The schema is fixed by product decision — see top-level
  `data-assets/feedback.sample.csv`. Do not introduce variant-specific
  columns; that would confound the data axis.
