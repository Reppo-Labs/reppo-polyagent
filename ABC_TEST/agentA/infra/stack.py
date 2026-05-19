"""
CDK stack for Agent A in the ABC experiment.

Provisions a fully-isolated stack for one variant:
  - S3 bucket (dashboard snapshot + optional feedback.csv backup)
  - DynamoDB table (positions ledger for this agent only)
  - Lambda function (handler.py + strategy.md + feedback.csv + shared `agent/`)
  - EventBridge schedule (cron every 15 min, aligned with B/C)

Each variant gets its own stack so resource lifecycles and capital flows
are physically separated — no shared bucket, no shared table, no cross-
contamination of dashboard rows or position state.

Sensitive env vars (ANTHROPIC_API_KEY, POLYGON_PRIVATE_KEY,
POLYMARKET_WALLET_ADDRESS, BUILDER_CODE) are NOT set here. Fill them via
the AWS Console or `scripts/update_abc_lambda_env.sh` after `cdk deploy`.
"""
import os

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_s3 as s3,
)
from constructs import Construct

# Build directory sits next to this agent's directory: ABC_TEST/agentA/_build/
# Populated by `./deploy.sh` before `cdk deploy`. Fully self-contained — does
# not reference the repo's top-level `agent/` package.
_AGENT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_BUILD_DIR  = os.path.join(_AGENT_ROOT, "_build")

AGENT_VARIANT = "A"
TABLE_NAME    = "abc-positions-a"
FUNCTION_NAME = "abc-agent-a"


class AgentAStack(Stack):

    def __init__(self, scope: Construct, id_: str, **kwargs) -> None:
        super().__init__(scope, id_, **kwargs)

        # ── S3: dashboard JSON + optional feedback backup ─────────────────────
        bucket = s3.Bucket(
            self, "Bucket",
            block_public_access=s3.BlockPublicAccess(
                block_public_acls=False,
                block_public_policy=False,
                ignore_public_acls=False,
                restrict_public_buckets=False,
            ),
            cors=[s3.CorsRule(
                allowed_methods=[s3.HttpMethods.GET],
                allowed_origins=["*"],
                allowed_headers=["*"],
            )],
            removal_policy=RemovalPolicy.RETAIN,
        )
        bucket.add_to_resource_policy(iam.PolicyStatement(
            actions=["s3:GetObject"],
            principals=[iam.AnyPrincipal()],
            resources=[bucket.arn_for_objects("dashboard/*")],
        ))

        # ── DynamoDB: positions ledger (Agent A only) ─────────────────────────
        table = dynamodb.Table(
            self, "Positions",
            table_name=TABLE_NAME,
            partition_key=dynamodb.Attribute(
                name="token_id", type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )
        table.add_global_secondary_index(
            index_name="market_id-index",
            partition_key=dynamodb.Attribute(
                name="market_id", type=dynamodb.AttributeType.STRING,
            ),
        )

        # ── Lambda asset ──────────────────────────────────────────────────────
        # Built by `./deploy.sh` from this agent's directory — pre-pip-installs
        # deps into `_build/`, copies the `agent/` package, `strategy.md`, and
        # (for A and C) bundles `feedback.csv`.
        if not os.path.isdir(_BUILD_DIR):
            raise FileNotFoundError(
                f"{_BUILD_DIR} not found. Run ./deploy.sh from this agent's directory first."
            )

        fn = lambda_.Function(
            self, "Function",
            function_name=FUNCTION_NAME,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="agent.handler.lambda_handler",
            code=lambda_.Code.from_asset(_BUILD_DIR),
            timeout=Duration.seconds(300),
            memory_size=512,
            environment={
                # Variant identity (tags every new DDB position row)
                "AGENT_VARIANT":          AGENT_VARIANT,
                # Starting capital — MUST be identical across A/B/C so ROI is
                # comparable. Fund each variant's wallet with this same amount
                # of pUSD before flipping DRY_RUN=false.
                "STARTING_BANKROLL":      "80",
                "GEO_MARKETS_ONLY":       "true",
                # Storage
                "S3_BUCKET":              bucket.bucket_name,
                "FEEDBACK_CSV_PATH":      "/var/task/feedback.csv",
                "DDB_TABLE":              TABLE_NAME,
                "DDB_REGION":             self.region,
                # Risk rails — tuned 2026-05-15 (parity with root infra/stack.py)
                "TAKE_PROFIT_PCT":        "0.25",
                "STOP_LOSS_PCT":          "0.20",
                "TRAIL_ACTIVATE_PCT":     "0.20",
                "TRAIL_GIVEBACK_PCT":     "0.30",
                "LOW_PRICE_THRESHOLD":    "0.15",
                "LOW_PRICE_SL_TICKS":     "8",
                "MAX_ABS_LOSS_USD":       "2.50",
                "MIN_BALANCE_RESERVE":    "10.0",
                "MAX_ORDER_USD":          "10.0",
                "MIN_ENTRY_PRICE":        "0.05",
                "MAX_PER_THEME":          "5",
                "ENTRY_SCORE_THRESHOLD":  "0.65",
                "AGENT_MAX_TOKENS":       "8192",
                "AGENT_MAX_ITERATIONS":   "15",
                "TAIL_PRICE_FLOOR":             "0.15",
                "TAIL_SCORE_THRESHOLD":         "0.90",
                "TAIL_INTERACTIONS_THRESHOLD":  "10",
                "SIGNAL_HALFLIFE_INTERACTIONS": "10",
                # Safety: start DRY_RUN — flip to false only after live audit.
                "DRY_RUN":                "true",
            },
        )
        bucket.grant_read(fn)
        bucket.grant_put(fn, "dashboard/*")
        table.grant_read_write_data(fn)

        # ── EventBridge schedule ──────────────────────────────────────────────
        # Cron (not rate) so A/B/C all fire on the same wall-clock tick
        # (HH:00, :15, :30, :45) and therefore see the same Gamma top-N
        # snapshot. rate() drifts based on deploy time and silently
        # decorrelates the market state across variants.
        rule = events.Rule(
            self, "Schedule",
            schedule=events.Schedule.cron(minute="0/15"),
        )
        rule.add_target(targets.LambdaFunction(fn))

        # ── Outputs ───────────────────────────────────────────────────────────
        CfnOutput(self, "BucketName",        value=bucket.bucket_name)
        CfnOutput(self, "TableName",         value=TABLE_NAME)
        CfnOutput(self, "LambdaName",        value=fn.function_name)
        CfnOutput(self, "DashboardJsonUrl",
                  value=f"https://{bucket.bucket_regional_domain_name}/dashboard/positions.json")
