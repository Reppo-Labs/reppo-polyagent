# Agent C — deploy

Self-contained CDK app for Variant C (same crowd data as Agent A;
edge-sized Bayesian / quarter-Kelly strategy).

## What's different from Agent A

- `agent/handler.py` is structurally identical to A's — same data source,
  same composition, same harness loop.
- `strategy.md` is different — see `strategy.md` for the Bayesian /
  fractional-Kelly entry policy.
- CDK stack sets Variant C's strategy-specific env vars
  (`MIN_EDGE`, `KELLY_FRACTION`, `MAX_NEW_ORDERS_PER_RUN`,
  `EVIDENCE_INTERACTION_CAP`).
- Stack name: `ABC-AgentC`. Table: `abc-positions-c`. Lambda: `abc-agent-c`.

The data axis is held constant vs A on purpose. If you want to vary the
data, fork a new variant (e.g. `agentD/`).

## Prerequisites

Same as Agent A — bootstrapped AWS account, `data-assets/feedback.csv`
present at the repo root, dependencies installable.

## Deploy

```bash
./deploy.sh
```

After deploy, fill in sensitive Lambda env vars (same set as A and B).

## Resources created

| Resource | Name |
|---|---|
| Lambda function | `abc-agent-c` |
| DynamoDB table | `abc-positions-c` |
| S3 bucket | (CDK-named) |
| EventBridge rule | cron `0/15 * * * ? *` (aligned with A/B) |

## Local invocation

```bash
export DRY_RUN=true
export S3_BUCKET=<bucket name>
export FEEDBACK_CSV_PATH="$(pwd)/../../data-assets/feedback.csv"
export ANTHROPIC_API_KEY=…
export POLYGON_PRIVATE_KEY=…
export POLYMARKET_WALLET_ADDRESS=…
export BUILDER_CODE=…
python -m agent.handler
```

## Monitoring

Same shape as A and B — rows tagged `agent_variant="C"`. Watch in
particular:

- **Trade frequency** — Kelly + minimum-edge gates may make C trade more
  or less often than A; this is informative about whether the binary
  threshold gate was over-restrictive.
- **Per-trade sizes** — C should produce a range, not flat $10. If sizes
  cluster at MAX_ORDER_USD, the Kelly fraction is being clipped — consider
  raising it (or accepting that the bankroll is too small for Kelly to
  matter at $10 ceiling).
- **Theme distribution** — C should naturally concentrate or spread based
  on edge magnitudes; if it concentrates, that informs whether A's
  anti-concentration ranking was costing edge.
