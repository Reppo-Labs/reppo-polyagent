# ABC experiment — what drives the P&L?

Three agent variants that share the **same Polymarket execution stack** and the
**same harness loop**, but differ on **data** and/or **strategy**. The goal is
to isolate which axis (if any) drives profitability and hit-rate.

Each variant is a **self-contained CDK app** under its own subdirectory:
`agent/` package, `infra/` stack, `strategy.md`, `data.md`, `requirements.txt`,
and a one-command `deploy.sh`. The Python `agent/` package is duplicated into
each variant on purpose — the three deployments must be able to diverge
independently without cross-contamination.

```
ABC_TEST/
  README.md          (this file)
  agentA/
    agent/           Lambda code (handler.py + signals.py + tools/…)
    infra/           CDK stack
    strategy.md      strategy artifact (loaded at runtime)
    data.md          data documentation
    requirements.txt
    cdk.json
    deploy.sh        one-command build + deploy
    README.md
  agentB/  (same shape)
  agentC/  (same shape)
```

To deploy any one of them:

```bash
cd ABC_TEST/agentA && ./deploy.sh
cd ABC_TEST/agentB && ./deploy.sh
cd ABC_TEST/agentC && ./deploy.sh
```

### Dashboards

Use **both**:

1. **Comparison (recommended)** — edit **`ABC_TEST/abc-compare-dashboard.html` only**
   (there is no second copy under `dashboards/`). It compares **Agent A vs B** only
   (loads each agent's `dashboard/positions.json` and **`dashboard/performance-history.json`**
   — one point per Lambda run). Charts: portfolio, ROI, win rate, payoff ratio;
   snapshot cards + per-agent position tables. `scripts/upload_abc_dashboards.sh`
   bakes the A/B bucket URLs into the HTML and uploads it to the A and B stacks as
   `dashboard/abc-compare-dashboard.html`.

2. **Per-agent detail** — **`ABC_TEST/dashboards/agent-{a,b,c}.html`** (generated from
   root `dashboard.html` via `scripts/generate_abc_dashboards.py`). Uploaded as
   `dashboard/index.html` per bucket. Data: that agent's `dashboard/positions.json`.

```bash
python3 scripts/generate_abc_dashboards.py
AWS_PROFILE=… scripts/upload_abc_dashboards.sh
```

Each `./deploy.sh` builds `_build/` (pip-installed deps + the agent package +
strategy.md + bundled feedback.csv for A and C) then runs `cdk deploy`.
Each variant creates its **own** S3 bucket, DDB table, Lambda function,
and EventBridge schedule — nothing is shared at the AWS level.

---

## The matrix

| Variant | Data signal | Strategy | What it tests |
|---|---|---|---|
| **A** | Reppo crowd CSV (current `feedback.csv` shape) | Current (`agent/system_prompt.py`) | **Baseline** — today's behaviour. |
| **B** | None — vanilla LLM, no crowd block | Same Phase 1 + risk rails; **Phase 2 rewritten** to enter from LLM-reasoned mispricing only | **Removes data.** Does the crowd signal beat "smart LLM + public market info"? |
| **C** | Same as A | Same Phase 1 + risk rails; **Phase 2 rewritten** as edge-sized Bayesian / quarter-Kelly over the same crowd table | **Removes strategy.** Does the *way* we use the same data matter? |

A vs B isolates **data**.
A vs C isolates **strategy**.
B vs C is informative but not a clean isolate (both data and strategy differ).

---

## Controlled (kept identical across A/B/C)

These live in the main codebase and **must not** vary per agent or the
experiment loses meaning:

- **Venue** — Polymarket CLOB V2 (`py_clob_client_v2`).
- **Signing protocol** — same `SIGNATURE_TYPE` (`POLY_1271`) and `BUILDER_CODE`
  across A/B/C. Each variant has its **own funded wallet + private key** (see
  "Wallet model" below). Keys are different by necessity — a shared key would
  mean shared on-chain inventory and the three Lambdas would interfere when
  Phase 1 tries to close positions another variant opened.
- **Order plumbing** — `place_order`, `close_position`, tick rounding,
  allowance management, fill reconciliation, DDB writes.
- **Risk rails enforced in Python** — tuned 2026-05-15 (parity with root):
  `TAKE_PROFIT_PCT=0.25`, `STOP_LOSS_PCT=0.20`, trail 20%/30%, `MIN_ENTRY_PRICE=0.10`,
  `MAX_PER_THEME=5`, `MAX_ORDER_USD=5`, `MAX_ABS_LOSS_USD=2.50`, VWAP close fills,
  `event_slug` on new rows, resolution reconcile before each run.
- **Market universe** — same `get_open_markets()` per run: top 300 by 24h volume
  after `GEO_MARKETS_ONLY` keyword filter (geopolitics / international relations).
  To make this true across variants, all three EventBridge rules use a shared
  cron (`minute=0/15`) so A/B/C wake on the same wall-clock tick.
- **Starting capital** — each variant is **funded with the same `STARTING_BANKROLL`**
  (default $80 in `infra/stack.py`). Different funding levels would make ROI
  incomparable. Set the env on each stack identically before deploy.
- **Dry-run flag** — same per comparison period.
- **Model** — same Claude version, same `max_tokens`, same `MAX_ITERATIONS`.
- **Persistence** — each variant uses its **own** DynamoDB table
  (`abc-positions-a` / `abc-positions-b` / `abc-positions-c`) and S3 bucket so
  runs do not mix rows. `AGENT_VARIANT` is still stamped on each position row
  for cross-dashboard analytics if you merge exports later.

If you change any of the above, you cannot attribute P&L differences to
data or strategy.

### Wallet model (one wallet per variant)

| Variant | Polymarket wallet (proxy) | Private key env |
|---|---|---|
| A | `0x4aA88C56208864fd53035B16B7EeE6E887d5c63F` | `POLYGON_PRIVATE_KEY` on `abc-agent-a` Lambda |
| B | `0xb14Cf74847ffA6bC9EbE4030cb73C40eEc699112` | `POLYGON_PRIVATE_KEY` on `abc-agent-b` Lambda |
| C | `0x2e31030b9d3365d6D28c7B2bE794e1c7b2741003` | `POLYGON_PRIVATE_KEY` on `abc-agent-c` Lambda |

These addresses are wired into each per-agent dashboard by
`scripts/generate_abc_dashboards.py`. Fund each wallet with **the same**
amount of pUSD before flipping `DRY_RUN=false` — otherwise larger starting
balances mechanically look like better performance.

---

## Varied (what each agent's directory contains)

Each `agentX/` has two artifacts:

- **`strategy.md`** — what would be compiled into the system prompt + tool
  copy. Mirrors the Phase 1 / Phase 2 structure of today's
  `agent/system_prompt.py` so deltas are visible at a glance.
- **`data.md`** — what gets prepended to the system prompt as the "signal
  table" section, and where it comes from.

Nothing in `agentX/` is venue- or harness-specific.

**Strategy env injection (B + C):** `strategy.md` may contain `${MIN_DISAGREEMENT}`,
`${MIN_EDGE}`, `${KELLY_FRACTION}`, etc. At Lambda cold start, `handler.py`
replaces those tokens with the live environment values from CDK / `update_abc_lambda_env.sh`
so the model never sees stale numbers baked into markdown. Agent A reads
`strategy.md` verbatim (baseline mirrors production prompt elsewhere).

---

## How to read the deltas

Open the three `strategy.md` files side-by-side:

- **Phase 1 (risk management)** should be **identical** across A/B/C
  — positions are positions regardless of how they were entered.
- **Phase 2 (new entries)** is where A/B/C diverge. Everything that
  references `weighted_score`, `max_conviction`, `interactions`, or
  `theme_key` either keeps, drops, or replaces those references.

If you find a Phase 2 rule that differs between two agents for a reason
unrelated to the experimental hypothesis, fix it before running — otherwise
the comparison is confounded.

---

## Metrics to log per variant

Every position written to DDB while a variant is active should carry an
`agent_variant` attribute (`"A"`, `"B"`, `"C"`) so the dashboard / analytics
can slice cleanly. Track at minimum:

- **Closed-trade ROI** (mean, median, std) and **hit rate**.
- **Time in market** per position; **turnover** per run.
- **Slippage** — `entry_price` vs Gamma `yes_price`/`no_price` at decision time.
- **Trade reasons** — fraction of closes by `take_profit` / `trailing_take_profit`
  / `stop_loss`.
- **Skipped runs** — how often each variant ends Phase 2 with no order (a
  selective agent looks great until it never trades).
- **Qualitative log** — final "Agent summary" text per run, kept alongside
  the row for audit.

Short samples will be noisy; plan for **same calendar window** A vs B and
A vs C (parallel deployments, not sequential — markets drift).

---

## How the refactor lands in code

The split between **data**, **strategy**, and **execution + harness** is
visible in each variant's `agent/handler.py`:

```python
signal_block  = _load_signal_block(...)       # ← data axis
strategy_text = _load_strategy_text()         # ← strategy axis (reads strategy.md)
system_prompt = signal_block + strategy_text  # ← composition
# (then the agent loop + tools + dashboard upload — these are platform)
```

- **Data adapter** differs per variant:
  - A and C: `preprocess_signals(csv)` over the bundled `feedback.csv`.
  - B: hardcoded empty-signal notice (no CSV).
- **Strategy** is `strategy.md` in each variant's directory, read at
  runtime. Edits to strategy.md ship on the next `./deploy.sh` — no Python
  changes required.
- **Execution + harness** (CLOB V2 client, DDB plumbing, risk rails,
  agent loop, dashboard snapshot) is the duplicated `agent/` package
  inside each variant. Today it is byte-identical across A/B/C; if a
  variant needs to fork it (e.g. a new tool only Agent C uses), the
  divergence is local to that variant.

`AGENT_VARIANT` is set as a Lambda env var in each stack and stamped onto
every new DDB position row by `agent/tools/wallet.py:place_order` so a
shared dashboard can attribute trades back to the variant that opened them.

---

## Variant summaries

- **`agentA/`** — `strategy.md` mirrors current Phase 1/2 verbatim;
  `data.md` describes the Reppo crowd CSV.
- **`agentB/`** — `strategy.md` keeps Phase 1 identical and replaces Phase 2
  with an explicit "LLM-reasoned mispricing only" entry policy. `data.md`
  documents the empty signal block.
- **`agentC/`** — `data.md` identical to A. `strategy.md` replaces A's
  threshold-based Phase 2 with a Bayesian-shrinkage + quarter-Kelly entry
  policy: same crowd signal, but converted to a continuous probability
  estimate, blended with the market price by evidence depth, and sized
  per trade by expected dollar P&L. Up to `MAX_NEW_ORDERS_PER_RUN` orders
  per run (vs A's hard one), no fixed size, no manual anti-concentration
  ranking (concentration emerges or doesn't from the sizing math itself).
