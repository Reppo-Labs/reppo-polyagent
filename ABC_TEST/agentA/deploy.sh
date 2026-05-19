#!/usr/bin/env bash
# Build + deploy Agent A (one command).
#
# Usage (from this directory):
#   ./deploy.sh                 # build then `cdk deploy`
#   ./deploy.sh --hotswap       # forward any flags to cdk
#
# What this script bundles into _build/ for the Lambda asset:
#   agent/         the Python package next to this script
#   strategy.md    Agent A's strategy artifact (loaded at runtime)
#   feedback.csv   the Reppo crowd CSV (copied from ../../data-assets/)
#   <pip deps>     installed from requirements.txt

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE"

echo "[A] Building _build/ in $HERE"
rm -rf _build
mkdir -p _build

echo "[A] Installing dependencies for Lambda (linux/arm64 wheels)..."
pip install -q \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  -r requirements.txt \
  -t _build/

echo "[A] Copying agent/ package..."
cp -r agent _build/

echo "[A] Copying strategy.md..."
cp strategy.md _build/

# Agent A bundles the canonical feedback.csv from the repo's data-assets/.
# If it's missing, the Lambda will fall back to S3 — but we prefer the
# bundled path so the variant is hermetic and doesn't depend on a shared
# bucket that B never touches.
csv_src="$HERE/../../data-assets/feedback.csv"
if [[ -f "$csv_src" ]]; then
  cp "$csv_src" _build/feedback.csv
  echo "[A] Bundled feedback.csv ($(wc -l < "$csv_src" | tr -d ' ') lines)"
else
  echo "[A] WARNING: $csv_src missing. Lambda will need S3_FEEDBACK_KEY set." >&2
fi

echo "[A] cdk deploy $*"
cdk deploy --require-approval never "$@"
