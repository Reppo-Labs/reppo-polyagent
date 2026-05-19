"""
CDK stack for Agent B in the ABC experiment.

Same shape as Agent A's stack. Differences vs A:
  - AGENT_VARIANT = "B"
  - Own bucket, own table (`abc-positions-b`), own Lambda (`abc-agent-b`)
  - No FEEDBACK_CSV_PATH (Variant B does not consume data)
  - No ENTRY_SCORE_THRESHOLD (Variant B has no crowd score gate)
  - Lambda asset from `_build/` (built by `./deploy.sh`; no feedback.csv)
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

_AGENT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_BUILD_DIR  = os.path.join(_AGENT_ROOT, "_build")

AGENT_VARIANT = "B"
TABLE_NAME    = "abc-positions-b"
FUNCTION_NAME = "abc-agent-b"


class AgentBStack(Stack):

    def __init__(self, scope: Construct, id_: str, **kwargs) -> None:
        super().__init__(scope, id_, **kwargs)

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
                # Variant identity
                "AGENT_VARIANT":          AGENT_VARIANT,
                # Starting capital — MUST be identical across A/B/C so ROI is
                # comparable. Fund each variant's wallet with this same amount
                # of pUSD before flipping DRY_RUN=false.
                "STARTING_BANKROLL":      "80",
                "GEO_MARKETS_ONLY":       "true",
                # Storage (no FEEDBACK_CSV_PATH — B does not read a CSV)
                "S3_BUCKET":              bucket.bucket_name,
                "DDB_TABLE":              TABLE_NAME,
                "DDB_REGION":             self.region,
                # Risk rails — tuned 2026-05-15 (parity with root; Phase 1 identical to A)
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
                # Variant-specific: minimum LLM-reasoned disagreement in cents
                # before B will enter (see agentB/strategy.md step 9).
                "MIN_DISAGREEMENT":       "0.08",
                "MAX_NEW_ORDERS_PER_RUN": "3",
                "AGENT_MAX_TOKENS":       "8192",
                "AGENT_MAX_ITERATIONS":   "15",
                "DRY_RUN":                "true",
            },
        )
        bucket.grant_read(fn)
        bucket.grant_put(fn, "dashboard/*")
        table.grant_read_write_data(fn)

        # Cron (not rate) so A/B/C all fire on the same wall-clock tick and
        # see the same Gamma top-N snapshot — see agentA/infra/stack.py.
        rule = events.Rule(
            self, "Schedule",
            schedule=events.Schedule.cron(minute="0/15"),
        )
        rule.add_target(targets.LambdaFunction(fn))

        CfnOutput(self, "BucketName",        value=bucket.bucket_name)
        CfnOutput(self, "TableName",         value=TABLE_NAME)
        CfnOutput(self, "LambdaName",        value=fn.function_name)
        CfnOutput(self, "DashboardJsonUrl",
                  value=f"https://{bucket.bucket_regional_domain_name}/dashboard/positions.json")
