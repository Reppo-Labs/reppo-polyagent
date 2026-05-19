#!/usr/bin/env bash
# Upload ABC experiment dashboards to agent A/B S3 buckets (public read on dashboard/*).
#
# Prereqs:
#   - cdk deploy done for A/B (stacks ABC-AgentA/B)
#   - python3 scripts/generate_abc_dashboards.py  (refresh per-agent HTML from dashboard.html)
#
# Usage:
#   AWS_PROFILE=… scripts/upload_abc_dashboards.sh
#   AWS_PROFILE=… scripts/upload_abc_dashboards.sh BUCKET_A BUCKET_B
#
# If bucket names omitted, reads BucketName from CloudFormation outputs.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGION="${AWS_REGION:-eu-west-1}"
DASH_DIR="$ROOT/ABC_TEST/dashboards"
COMPARE_SRC="$ROOT/ABC_TEST/abc-compare-dashboard.html"

python3 "$ROOT/scripts/generate_abc_dashboards.py"

if [[ ! -f "$COMPARE_SRC" ]]; then
  echo "ERROR: missing $COMPARE_SRC" >&2
  exit 1
fi

bucket_for() {
  local stack=$1
  if [[ -n "${2:-}" ]]; then
    echo "$2"
    return
  fi
  aws cloudformation describe-stacks \
    --region "$REGION" \
    --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" \
    --output text
}

BUCKET_A="${1:-$(bucket_for ABC-AgentA)}"
BUCKET_B="${2:-$(bucket_for ABC-AgentB)}"

BASE_A="https://${BUCKET_A}.s3.${REGION}.amazonaws.com"
URL_A="${BASE_A}/dashboard/positions.json"
URL_B="https://${BUCKET_B}.s3.${REGION}.amazonaws.com/dashboard/positions.json"

# Bake A/B bucket URLs into compare dashboard (charts + tables fetch from these).
COMPARE_HTML=$(mktemp)
python3 - "$COMPARE_SRC" "$COMPARE_HTML" "$URL_A" "$URL_B" <<'PY'
import json, sys
src, dst, ua, ub = sys.argv[1:5]
text = open(src, encoding="utf-8").read()
inject = json.dumps({"a": ua, "b": ub}, indent=2)
needle = "const INJECTED_POSITIONS_URLS = null;"
if needle not in text:
    raise SystemExit(f"missing {needle!r} in compare dashboard")
text = text.replace(needle, f"const INJECTED_POSITIONS_URLS = {inject};", 1)
open(dst, "w", encoding="utf-8").write(text)
PY

upload_agent() {
  local label=$1 bucket=$2 html=$3
  echo "[$label] s3://$bucket/dashboard/ …"
  aws s3 cp "$DASH_DIR/$html" "s3://$bucket/dashboard/index.html" \
    --region "$REGION" --content-type "text/html" --cache-control "no-cache"
  aws s3 cp "$COMPARE_HTML" "s3://$bucket/dashboard/abc-compare-dashboard.html" \
    --region "$REGION" --content-type "text/html" --cache-control "no-cache"
}

upload_agent A "$BUCKET_A" agent-a.html
upload_agent B "$BUCKET_B" agent-b.html

echo
echo "Per-agent dashboards (public under dashboard/*):"
echo "  A  https://${BUCKET_A}.s3.${REGION}.amazonaws.com/dashboard/index.html"
echo "  B  https://${BUCKET_B}.s3.${REGION}.amazonaws.com/dashboard/index.html"
echo
echo "A/B comparison (URLs baked in — open without query params):"
echo "  ${BASE_A}/dashboard/abc-compare-dashboard.html"
