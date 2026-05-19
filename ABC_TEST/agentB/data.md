# Agent B — data source

**Source:** none.

This variant intentionally removes the curated crowd signal. The "signal
table" block normally prepended to the system prompt is replaced with a
short explicit notice so the model does not hallucinate a missing input:

```
NO CROWD SIGNAL AVAILABLE FOR THIS DEPLOYMENT.

Phase 2 entries must be reasoned from public market information
(get_open_markets snapshot) plus the model's own world knowledge.
Do not refer to "the crowd", "weighted_score", "max_conviction",
"interactions", or "theme_key from signals" — none of those exist
in this run.
```

## What this isolates

If Agent A outperforms Agent B over the same calendar window, the
**curated crowd signal** is the contributor — not just "the LLM is good
at picking mispricings on Polymarket."

If Agent B matches or beats Agent A, the crowd signal is not adding edge
on top of what a competent generic reasoner can extract from public
information.

## What this does NOT isolate

- **Model knowledge cutoff.** If recent events are outside the LLM's
  training data, B is starved of context that A gets for free via the
  crowd table. Be aware of this when interpreting short-window results.
- **Phrasing of the empty-signal notice.** Different wording can change
  trade rate. Keep it identical across deployments of B.

## Notes for the experiment

- The CSV path / S3 read is **not** invoked for this variant. The handler
  branch that loads `feedback.csv` should short-circuit on
  `AGENT_VARIANT=B` (or equivalent) and substitute the empty-signal
  notice above.
- No `source_headline` from a crowd CSV exists here. See
  `strategy.md` step 10 for what to log instead.
