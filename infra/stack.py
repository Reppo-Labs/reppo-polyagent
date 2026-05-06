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

# Absolute path to the polyagent project root (one level above infra/)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class GeoTradingStack(Stack):
    """
    Single stack for the geo-trading agent POC.

    After deploying, set the following Lambda env vars manually via the
    AWS Console or CLI — they are intentionally absent from this file:
      ANTHROPIC_API_KEY, POLYGON_PRIVATE_KEY, PRIVY_API_KEY,
      PRIVY_WALLET_ID, POLYMARKET_WALLET_ADDRESS

    Lambda encrypts env vars at rest with KMS at no extra cost, which is
    sufficient for a POC. Move to Secrets Manager before deploying
    significant capital.
    """

    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # ── S3: feedback CSV + dashboard snapshot ─────────────────────────────
        bucket = s3.Bucket(
            self,
            "GeoSignalsBucket",
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

        # Public read on dashboard/* so the static HTML page can fetch it
        bucket.add_to_resource_policy(iam.PolicyStatement(
            actions=["s3:GetObject"],
            principals=[iam.AnyPrincipal()],
            resources=[bucket.arn_for_objects("dashboard/*")],
        ))

        # ── DynamoDB: position ledger ─────────────────────────────────────────
        table = dynamodb.Table(
            self,
            "GeoTradingPositions",
            table_name="geo-trading-positions",
            partition_key=dynamodb.Attribute(
                name="token_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        table.add_global_secondary_index(
            index_name="market_id-index",
            partition_key=dynamodb.Attribute(
                name="market_id",
                type=dynamodb.AttributeType.STRING,
            ),
        )

        # ── Lambda ────────────────────────────────────────────────────────────
        fn = lambda_.Function(
            self,
            "GeoTradingAgent",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="agent.handler.lambda_handler",
            # _build/ is pre-populated by: pip install -r requirements.txt -t _build/ && cp -r agent _build/
            code=lambda_.Code.from_asset(os.path.join(_PROJECT_ROOT, "_build")),
            timeout=Duration.seconds(300),
            memory_size=512,
            environment={
                # Non-sensitive config — safe to version-control
                "S3_BUCKET":           bucket.bucket_name,
                "S3_FEEDBACK_KEY":     "geo-signals/feedback.csv",
                "DDB_TABLE":           "geo-trading-positions",
                "TAKE_PROFIT_PCT":     "0.50",
                "STOP_LOSS_PCT":       "0.30",
                "MIN_BALANCE_RESERVE": "15.0",
                "MAX_ORDER_USD":       "10.0",
                "DRY_RUN":             "true",
            },
        )

        bucket.grant_read(fn)
        bucket.grant_put(fn, "dashboard/*")
        table.grant_read_write_data(fn)

        # ── EventBridge cron: every 4 hours ───────────────────────────────────
        rule = events.Rule(
            self,
            "AgentSchedule",
            schedule=events.Schedule.rate(Duration.hours(4)),
        )
        rule.add_target(targets.LambdaFunction(fn))

        # ── Outputs ───────────────────────────────────────────────────────────
        CfnOutput(self, "BucketName",   value=bucket.bucket_name)
        CfnOutput(self, "DashboardUrl",
                  value=f"https://{bucket.bucket_regional_domain_name}/dashboard/positions.json")
        CfnOutput(self, "LambdaName",   value=fn.function_name)
