#!/usr/bin/env bash
# Update the deployed Lambda's environment from values in .env.
# Usage:
#   AWS_PROFILE=484907511683_AdministratorAccess scripts/update_lambda_env.sh
#
# Reads .env (gitignored). Looks up the Lambda function name from the
# CloudFormation stack output (LambdaName) so you don't have to hard-code it.

set -euo pipefail

REGION="${AWS_REGION:-eu-west-1}"
STACK_NAME="${STACK_NAME:-GeoTrading}"

if [[ ! -f .env ]]; then
  echo "ERROR: .env not found in $(pwd). Run from repo root." >&2
  exit 1
fi

# Source .env into local shell vars without exporting to global env
set -a
# shellcheck disable=SC1091
source .env
set +a

# Lookup Lambda name from CFN outputs (set by infra/stack.py CfnOutput LambdaName)
FN_NAME="$(aws cloudformation describe-stacks \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='LambdaName'].OutputValue" \
  --output text)"

if [[ -z "$FN_NAME" || "$FN_NAME" == "None" ]]; then
  echo "ERROR: Could not find LambdaName output on stack '$STACK_NAME' in $REGION." >&2
  echo "       Has 'cdk deploy' completed successfully?" >&2
  exit 1
fi

echo "Stack    : $STACK_NAME ($REGION)"
echo "Function : $FN_NAME"

# Build the env JSON. Only include the keys Lambda actually needs at runtime.
# Anything in .env that is irrelevant for production (e.g. BUILDER_API_KEY/SECRET
# from the deprecated V1 builder-relayer flow) is omitted intentionally.
read -r -d '' VARS_JSON <<JSON || true
{
  "Variables": {
    "ANTHROPIC_API_KEY":         "${ANTHROPIC_API_KEY:?missing in .env}",
    "POLYGON_PRIVATE_KEY":       "${POLYGON_PRIVATE_KEY:?missing in .env}",
    "POLYMARKET_WALLET_ADDRESS": "${POLYMARKET_WALLET_ADDRESS:?missing in .env}",
    "BUILDER_CODE":              "${BUILDER_CODE:?missing in .env}",
    "SIGNATURE_TYPE":            "${SIGNATURE_TYPE:-POLY_1271}",
    "DRY_RUN":                   "${DRY_RUN:-true}",
    "MAX_ORDER_USD":             "${MAX_ORDER_USD:-10.0}",
    "MIN_BALANCE_RESERVE":       "${MIN_BALANCE_RESERVE:-10.0}",
    "TAKE_PROFIT_PCT":           "${TAKE_PROFIT_PCT:-0.50}",
    "STOP_LOSS_PCT":             "${STOP_LOSS_PCT:-0.30}",
    "S3_BUCKET":                 "${S3_BUCKET:-}",
    "S3_FEEDBACK_KEY":           "${S3_FEEDBACK_KEY:-geo-signals/feedback.csv}",
    "DDB_TABLE":                 "${DDB_TABLE:-geo-trading-positions}",
    "DDB_REGION":                "${DDB_REGION:-$REGION}"
  }
}
JSON

# If S3_BUCKET wasn't in .env, fill it from the stack output so the dashboard
# upload path keeps working without manual editing.
if [[ -z "${S3_BUCKET:-}" ]]; then
  BUCKET_FROM_STACK="$(aws cloudformation describe-stacks \
    --region "$REGION" \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" \
    --output text)"
  if [[ -n "$BUCKET_FROM_STACK" && "$BUCKET_FROM_STACK" != "None" ]]; then
    VARS_JSON="${VARS_JSON/\"S3_BUCKET\":                 \"\"/\"S3_BUCKET\":                 \"$BUCKET_FROM_STACK\"}"
    echo "Bucket   : $BUCKET_FROM_STACK (from stack output)"
  fi
fi

echo
echo "Pushing environment to $FN_NAME …"
aws lambda update-function-configuration \
  --region "$REGION" \
  --function-name "$FN_NAME" \
  --environment "$VARS_JSON" \
  --query 'Environment.Variables | keys(@)' \
  --output table

echo
echo "Done. Trigger a test run with:"
echo "  aws lambda invoke --region $REGION --function-name $FN_NAME --payload '{}' /tmp/out.json && cat /tmp/out.json"
