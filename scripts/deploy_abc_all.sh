#!/usr/bin/env bash
# Build + deploy ABC agents A/B/C, push live env, upload dashboards.
#
# Prerequisites:
#   - AWS credentials (AWS_PROFILE or default chain)
#   - cdk bootstrap in eu-west-1
#   - ABC_TEST/agent{A,B,C}/.env filled (gitignored)
#   - data-assets/feedback.csv (copied from your Reppo export)
#
# Usage:
#   AWS_PROFILE=… scripts/deploy_abc_all.sh
#   LIVE=1 scripts/deploy_abc_all.sh    # sets DRY_RUN=false in .env before push

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGION="${AWS_REGION:-eu-west-1}"
CSV_SRC="$ROOT/feedback-15052026.csv"
CSV_DST="$ROOT/data-assets/feedback.csv"

echo "=== ABC experiment deploy ==="

# ── Feedback CSV for A + C bundles ───────────────────────────────────────────
if [[ -f "$CSV_SRC" ]]; then
  cp "$CSV_SRC" "$CSV_DST"
  echo "Bundled CSV: $(wc -l < "$CSV_DST" | tr -d ' ') lines → data-assets/feedback.csv"
elif [[ ! -f "$CSV_DST" ]]; then
  echo "ERROR: need data-assets/feedback.csv or feedback-15052026.csv at repo root" >&2
  exit 1
fi

# ── Drift check ──────────────────────────────────────────────────────────────
"$ROOT/ABC_TEST/tools/check_variant_drift.sh"

# ── Live trading flag ──────────────────────────────────────────────────────────
if [[ "${LIVE:-}" == "1" ]]; then
  for d in agentA agentB agentC; do
    envf="$ROOT/ABC_TEST/$d/.env"
    if [[ -f "$envf" ]]; then
      if grep -q '^DRY_RUN=' "$envf"; then
        if [[ "$(uname -s)" == "Darwin" ]]; then
          sed -i '' 's/^DRY_RUN=.*/DRY_RUN=false/' "$envf"
        else
          sed -i 's/^DRY_RUN=.*/DRY_RUN=false/' "$envf"
        fi
      else
        echo "DRY_RUN=false" >> "$envf"
      fi
    fi
  done
  echo "LIVE=1 → DRY_RUN=false in all agent .env files"
fi

# ── CDK deploy each variant ───────────────────────────────────────────────────
for label in A B C; do
  dir="$ROOT/ABC_TEST/agent$label"
  echo
  echo "========== Deploying Agent $label =========="
  (cd "$dir" && ./deploy.sh)
done

# ── Push secrets (merge with CDK env) ─────────────────────────────────────────
echo
echo "========== Pushing Lambda environment =========="
"$ROOT/scripts/update_abc_lambda_env.sh" all

# ── Dashboards ────────────────────────────────────────────────────────────────
echo
echo "========== Uploading dashboards =========="
"$ROOT/scripts/upload_abc_dashboards.sh"

echo
echo "=== Done ==="
echo "Compare dashboard URL will be printed above by upload_abc_dashboards.sh"
echo "Flip LIVE=1 before this script if you want DRY_RUN=false (or edit .env manually)."
