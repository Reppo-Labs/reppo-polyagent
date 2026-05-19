#!/usr/bin/env bash
# Build + deploy Agent C (one command).
#
# Variant C bundles feedback.csv (same source as A) because the data
# source is held identical to A on purpose.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE"

echo "[C] Building _build/ in $HERE"
rm -rf _build
mkdir -p _build

echo "[C] Installing dependencies for Lambda (linux/arm64 wheels)..."
pip install -q \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  -r requirements.txt \
  -t _build/

echo "[C] Copying agent/ package..."
cp -r agent _build/

echo "[C] Copying strategy.md..."
cp strategy.md _build/

csv_src="$HERE/../../data-assets/feedback.csv"
if [[ -f "$csv_src" ]]; then
  cp "$csv_src" _build/feedback.csv
  echo "[C] Bundled feedback.csv ($(wc -l < "$csv_src" | tr -d ' ') lines)"
else
  echo "[C] WARNING: $csv_src missing. Lambda will need S3_FEEDBACK_KEY set." >&2
fi

echo "[C] cdk deploy $*"
cdk deploy --require-approval never "$@"
