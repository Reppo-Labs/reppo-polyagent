#!/usr/bin/env bash
# Compare files inside ABC_TEST/agentA, agentB, agentC that are meant to stay
# in lock-step. The three agent directories are duplicated on purpose (so each
# can fork cleanly), but a handful of files MUST remain byte-identical or the
# experiment becomes uninterpretable:
#
#   agent/dashboard_history.py           — metric definitions must match
#   agent/tools/                         — execution / risk plumbing
#   agent/signals.py                     — scoring (A vs C share this; B too)
#   agent/settlement.py                  — resolution reconcile
#
# Phase 1 risk env vars inside infra/stack.py are also checked. Things that
# legitimately differ across variants (AGENT_VARIANT, TABLE_NAME, the
# variant-specific knobs, strategy.md, data.md, handler.py header docstring)
# are not flagged.
#
# Usage:
#   ABC_TEST/tools/check_variant_drift.sh             # report drift, exit 0/1
#   ABC_TEST/tools/check_variant_drift.sh --baseline B   # compare against B
#
# Exit code: 0 = no drift, 1 = drift found, 2 = missing variant dir.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
BASELINE="A"
if [[ "${1:-}" == "--baseline" && -n "${2:-}" ]]; then
  BASELINE="$2"
fi

base_dir() { echo "$ROOT/agent${1}"; }

for V in A B C; do
  if [[ ! -d "$(base_dir "$V")" ]]; then
    echo "missing: $(base_dir "$V")" >&2
    exit 2
  fi
done

# Files that must be identical across variants.
SHARED_FILES=(
  "agent/dashboard_history.py"
  "agent/signals.py"
  "agent/settlement.py"
  "agent/tools/__init__.py"
  "agent/tools/ddb.py"
  "agent/tools/markets.py"
  "agent/tools/positions.py"
  "agent/tools/wallet.py"
)

# Phase-1 risk env vars that must match in each infra/stack.py.
SHARED_ENV_VARS=(
  TAKE_PROFIT_PCT STOP_LOSS_PCT TRAIL_ACTIVATE_PCT TRAIL_GIVEBACK_PCT
  LOW_PRICE_THRESHOLD LOW_PRICE_SL_TICKS MAX_ABS_LOSS_USD
  MIN_BALANCE_RESERVE MAX_ORDER_USD MIN_ENTRY_PRICE MAX_PER_THEME
  STARTING_BANKROLL GEO_MARKETS_ONLY
)

drift=0
echo "=== File-level drift (baseline: agent${BASELINE}) ==="
for f in "${SHARED_FILES[@]}"; do
  base_path="$(base_dir "$BASELINE")/$f"
  if [[ ! -f "$base_path" ]]; then
    echo "  ! baseline missing $f"
    drift=1
    continue
  fi
  for V in A B C; do
    [[ "$V" == "$BASELINE" ]] && continue
    other="$(base_dir "$V")/$f"
    if [[ ! -f "$other" ]]; then
      echo "  ! agent${V} missing $f"
      drift=1
      continue
    fi
    if ! cmp -s "$base_path" "$other"; then
      echo "  ✗ $f  differs (agent${BASELINE} vs agent${V})"
      drift=1
    fi
  done
done
[[ $drift -eq 0 ]] && echo "  ✓ all shared files identical"

echo
echo "=== Risk env var drift in infra/stack.py ==="
# Extract `"KEY": "VAL"` pairs from each stack.py and compare for the
# whitelisted SHARED_ENV_VARS only.
extract_env() {
  local stack=$1 key=$2
  grep -E "\"$key\"\s*:" "$stack" | head -1 | sed -E 's/.*:[[:space:]]*"([^"]+)".*/\1/'
}

env_drift=0
for key in "${SHARED_ENV_VARS[@]}"; do
  base_val=$(extract_env "$(base_dir "$BASELINE")/infra/stack.py" "$key" || true)
  for V in A B C; do
    [[ "$V" == "$BASELINE" ]] && continue
    other_val=$(extract_env "$(base_dir "$V")/infra/stack.py" "$key" || true)
    if [[ "$base_val" != "$other_val" ]]; then
      echo "  ✗ $key:  agent${BASELINE}=\"$base_val\"  agent${V}=\"$other_val\""
      env_drift=1
    fi
  done
done
[[ $env_drift -eq 0 ]] && echo "  ✓ all shared env vars match"

if [[ $drift -ne 0 || $env_drift -ne 0 ]]; then
  echo
  echo "Drift detected. Either:"
  echo "  - copy the baseline file/value into the diverged variant, OR"
  echo "  - if the divergence is intentional, remove it from the SHARED lists"
  echo "    at the top of this script."
  exit 1
fi

echo
echo "No drift. A/B/C are in lock-step on the shared surface."
exit 0
