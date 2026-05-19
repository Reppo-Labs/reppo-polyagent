#!/usr/bin/env bash
# Build + deploy Agent B (one command).
#
# Variant B does NOT bundle feedback.csv — the empty-signal notice is
# produced by handler.py itself.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE"

echo "[B] Building _build/ in $HERE"
rm -rf _build
mkdir -p _build

echo "[B] Installing dependencies for Lambda (linux/arm64 wheels)..."
pip install -q \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  -r requirements.txt \
  -t _build/

echo "[B] Copying agent/ package..."
cp -r agent _build/

echo "[B] Copying strategy.md..."
cp strategy.md _build/

echo "[B] cdk deploy $*"
cdk deploy --require-approval never "$@"
