#!/usr/bin/env bash
# Push secrets from ABC_TEST/agent{X}/.env onto the deployed Lambda.
# Merges with existing env (CDK risk rails, etc.) so nothing is wiped.
#
# Usage (after cdk deploy):
#   AWS_PROFILE=… scripts/update_abc_lambda_env.sh A
#   AWS_PROFILE=… scripts/update_abc_lambda_env.sh all   # A, B, C

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGION="${AWS_REGION:-eu-west-1}"

push_one() {
  local VARIANT
  VARIANT=$(echo "$1" | tr '[:lower:]' '[:upper:]')
  local AGENT_DIR FN_FALLBACK STACK_NAME
  case "$VARIANT" in
    A) AGENT_DIR="agentA"; FN_FALLBACK="abc-agent-a"; STACK_NAME="ABC-AgentA" ;;
    B) AGENT_DIR="agentB"; FN_FALLBACK="abc-agent-b"; STACK_NAME="ABC-AgentB" ;;
    C) AGENT_DIR="agentC"; FN_FALLBACK="abc-agent-c"; STACK_NAME="ABC-AgentC" ;;
    *) echo "Unknown variant: $1" >&2; return 1 ;;
  esac

  local ENV_FILE="$ROOT/ABC_TEST/$AGENT_DIR/.env"
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found" >&2
    return 1
  fi

  local FN_NAME
  FN_NAME="$(aws cloudformation describe-stacks \
    --region "$REGION" \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='LambdaName'].OutputValue" \
    --output text 2>/dev/null || true)"
  [[ -z "$FN_NAME" || "$FN_NAME" == "None" ]] && FN_NAME="$FN_FALLBACK"

  echo "── Agent $VARIANT ($FN_NAME) ──"

  VARS_JSON=$(python3 - "$ENV_FILE" "$REGION" "$FN_NAME" <<'PY'
import json, subprocess, sys
from pathlib import Path

env_file, region, fn_name = sys.argv[1:4]
secrets = {}
for line in Path(env_file).read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    secrets[k.strip()] = v.strip()

# Start from whatever CDK deployed (risk rails, variant knobs, etc.)
current = {}
try:
    out = subprocess.check_output(
        [
            "aws", "lambda", "get-function-configuration",
            "--region", region,
            "--function-name", fn_name,
            "--query", "Environment.Variables",
            "--output", "json",
        ],
        text=True,
    )
    current = json.loads(out) or {}
except subprocess.CalledProcessError:
    pass

merged = {**current, **{
    "AGENT_VARIANT": secrets.get("AGENT_VARIANT", current.get("AGENT_VARIANT", "")),
    "ANTHROPIC_API_KEY": secrets["ANTHROPIC_API_KEY"],
    "POLYGON_PRIVATE_KEY": secrets["POLYGON_PRIVATE_KEY"],
    "POLYMARKET_WALLET_ADDRESS": secrets["POLYMARKET_WALLET_ADDRESS"],
    "BUILDER_CODE": secrets["BUILDER_CODE"],
    "SIGNATURE_TYPE": secrets.get("SIGNATURE_TYPE", "POLY_1271"),
    "DRY_RUN": secrets.get("DRY_RUN", "false"),
    "DDB_TABLE": secrets.get("DDB_TABLE", current.get("DDB_TABLE", "")),
    "DDB_REGION": secrets.get("DDB_REGION", current.get("DDB_REGION", region)),
}}

# Bucket from stack if missing
if not merged.get("S3_BUCKET"):
    stack = secrets.get("STACK_NAME", "")
    if stack:
        try:
            out = subprocess.check_output(
                [
                    "aws", "cloudformation", "describe-stacks",
                    "--region", region,
                    "--stack-name", stack,
                    "--query", "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue",
                    "--output", "text",
                ],
                text=True,
            ).strip()
            if out and out != "None":
                merged["S3_BUCKET"] = out
        except subprocess.CalledProcessError:
            pass

variant = merged.get("AGENT_VARIANT", "")
if variant in ("A", "C"):
    merged["FEEDBACK_CSV_PATH"] = "/var/task/feedback.csv"

# Trading knobs: only override CDK defaults when explicitly set in .env
# (avoids stale MAX_ORDER_USD=5 etc. wiping stack tuning on every push).
_TRADING_KNOBS = (
    "MIN_BALANCE_RESERVE", "MAX_ORDER_USD", "MIN_ENTRY_PRICE", "MIN_DISAGREEMENT",
    "MIN_EDGE", "KELLY_FRACTION", "MAX_NEW_ORDERS_PER_RUN",
    "EVIDENCE_INTERACTION_CAP", "ENTRY_SCORE_THRESHOLD",
    "AGENT_MAX_TOKENS", "AGENT_MAX_ITERATIONS",
    "STARTING_BANKROLL",
)
for k in _TRADING_KNOBS:
    v = secrets.get(k)
    if v is not None and str(v).strip() != "":
        merged[k] = str(v).strip()

print(json.dumps({"Variables": merged}))
PY
)

  aws lambda update-function-configuration \
    --region "$REGION" \
    --function-name "$FN_NAME" \
    --environment "$VARS_JSON" \
    --query 'Environment.Variables.[DRY_RUN,AGENT_VARIANT,MAX_ORDER_USD,STARTING_BANKROLL]' \
    --output table

  echo "Test: aws lambda invoke --region $REGION --function-name $FN_NAME --payload '{}' /tmp/abc-${VARIANT}.json"
  echo
}

VARIANT_ARG="${1:-}"
if [[ -z "$VARIANT_ARG" ]]; then
  echo "Usage: $0 {A|B|C|all}" >&2
  exit 1
fi

VARIANT_LOWER=$(echo "$VARIANT_ARG" | tr '[:upper:]' '[:lower:]')
if [[ "$VARIANT_LOWER" == "all" ]]; then
  push_one A
  push_one B
  push_one C
else
  push_one "$VARIANT_ARG"
fi
