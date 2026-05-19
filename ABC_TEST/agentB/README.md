# Agent B — deploy

Self-contained CDK app for Variant B of the ABC experiment (no crowd data,
LLM-reasoned mispricing entries).

## What's different from Agent A

- `agent/handler.py` substitutes an explicit empty-signal notice for the
  crowd table. No CSV is read.
- `strategy.md` keeps Phase 1 identical to A and replaces Phase 2.
- `deploy.sh` does NOT bundle feedback.csv.
- Stack name: `ABC-AgentB`. Table: `abc-positions-b`. Lambda: `abc-agent-b`.

Everything else (tool set, risk rails, dashboard format) is identical to
Agent A by design — those are platform invariants of the experiment.

## Prerequisites

- AWS account with `cdk bootstrap` already run in `eu-west-1`.
- Dependencies installable from `requirements.txt`.

## Deploy

```bash
./deploy.sh
```

After deploy, fill in the sensitive Lambda env vars (same as Agent A —
ANTHROPIC_API_KEY, POLYGON_PRIVATE_KEY, POLYMARKET_WALLET_ADDRESS,
BUILDER_CODE, SIGNATURE_TYPE).

## Resources created

| Resource | Name |
|---|---|
| Lambda function | `abc-agent-b` |
| DynamoDB table | `abc-positions-b` |
| S3 bucket | (CDK-named) |
| EventBridge rule | cron `0/15 * * * ? *` (aligned with A/C) |

## Local invocation

```bash
export DRY_RUN=true
export S3_BUCKET=<bucket name>
export ANTHROPIC_API_KEY=…
export POLYGON_PRIVATE_KEY=…
export POLYMARKET_WALLET_ADDRESS=…
export BUILDER_CODE=…
python -m agent.handler
```

No feedback CSV is needed — B's data block is hardcoded in `handler.py`.

## Monitoring

Same shape as Agent A — `dashboard/positions.json` to the bucket, rows
tagged `agent_variant="B"`.

## Experimental note

B's results are only comparable to A's if both run on the **same wallet
isolation strategy and same calendar window**. If B trades on a shared
wallet with A, theme caps interact and the variants are not independent.
See `../README.md` for the full experiment controls.
