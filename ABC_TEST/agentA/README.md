# Agent A — deploy

Self-contained CDK app for Variant A of the ABC experiment.

## What's here

```
agentA/
  agent/              full Python package (Lambda code)
    handler.py        Lambda entry — loads CSV + strategy.md, runs harness
    signals.py
    tools/            shared execution layer (CLOB V2, DDB, risk rails)
  infra/
    app.py            CDK app entry
    stack.py          Stack: S3 + DynamoDB + Lambda + EventBridge
  strategy.md         strategy artifact (loaded at runtime)
  data.md             data source documentation
  cdk.json            tells `cdk` which app to run
  requirements.txt    Python dependencies for the Lambda bundle
  deploy.sh           one-command build + deploy
```

The `agent/` package is intentionally duplicated into each variant (A/B/C)
so the three deployments can diverge independently without cross-contamination.

## Prerequisites

- AWS account with `cdk bootstrap` already run in `eu-west-1`.
- `aws-cdk-lib`, `constructs`, and the Python dependencies in `requirements.txt`
  installable.
- A Reppo `feedback.csv` placed at the repo's top-level `data-assets/feedback.csv`.

## Deploy

From this directory:

```bash
./deploy.sh
```

The script (a) builds `_build/` with deps + agent package + strategy.md + feedback.csv,
then (b) runs `cdk deploy`.

After deploy, fill in the sensitive env vars on the Lambda:

```bash
AWS_PROFILE=… scripts/update_abc_lambda_env.sh A
# or all three after each cdk deploy:
AWS_PROFILE=… scripts/update_abc_lambda_env.sh all
```

(Or merge secrets manually via `aws lambda update-function-configuration` — same outcome.)

## What gets created

| Resource | Name |
|---|---|
| Lambda function | `abc-agent-a` |
| DynamoDB table | `abc-positions-a` |
| S3 bucket | (CDK-named — see `BucketName` output) |
| EventBridge rule | cron `0/15 * * * ? *` (aligned with B/C at :00, :15, :30, :45) |

Stack outputs include `LambdaName`, `BucketName`, `TableName`, and
`DashboardJsonUrl` (the live positions JSON).

## Local invocation

```bash
export AWS_PROFILE=…
export DRY_RUN=true
export S3_BUCKET=<the bucket name from CDK output>
export FEEDBACK_CSV_PATH="$(pwd)/../../data-assets/feedback.csv"
export ANTHROPIC_API_KEY=…
export POLYGON_PRIVATE_KEY=…
export POLYMARKET_WALLET_ADDRESS=…
export BUILDER_CODE=…
python -m agent.handler
```

This runs the same composition pipeline as Lambda — handy for debugging
the strategy without paying for Lambda invocations.

## Monitoring

`dashboard/positions.json` is uploaded to S3 at the end of every run. Point
the project's `dashboard.html` at the `DashboardJsonUrl` output to view
Agent A's open positions and P&L.

Every position row carries `agent_variant="A"`, so if you later consolidate
all three buckets into one dashboard, A's rows remain distinguishable from
B's and C's.

## Tear-down

```bash
cd ABC_TEST/agentA
cdk destroy
```

The DDB table and S3 bucket are set to `RemovalPolicy.RETAIN` — `cdk destroy`
will leave them intact so position history is not lost. Delete them manually
if you really want a clean slate.
