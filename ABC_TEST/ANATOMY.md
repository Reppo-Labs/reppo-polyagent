# ABC experiment — anatomy

Each variant (`agentA`, `agentB`, `agentC`) is a **full Lambda bundle**: harness + tools + infra wiring. They are duplicated on purpose so you can deploy **three independent stacks** and compare outcomes.

**Portfolio addresses** (one per agent — see `ABC_TEST/AGENTS.md`):

| Agent | Portfolio |
|-------|-----------|
| A | `0x4aA88C56208864fd53035B16B7EeE6E887d5c63F` |
| B | `0xb14Cf74847ffA6bC9EbE4030cb73C40eEc699112` |
| C | `0x2e31030b9d3365d6D28c7B2bE794e1c7b2741003` |

Configured as `POLYMARKET_WALLET_ADDRESS` in each variant’s `.env`.

## Three layers (conceptual)

| Layer | What it is in this repo | A / B / C |
| --- | --- | --- |
| **Data** | Text injected into the system prompt before `strategy.md` | A: bundled `feedback.csv` → `_load_signal_block`. B: empty block (“no CSV signal”). C: same CSV as A unless you swap the file. |
| **Strategy** | `strategy.md` (human-editable rules the model must follow) | Per variant directory; edit without touching Python. |
| **Execution** | CLOB client, DynamoDB positions, markets, wallet, tool implementations | Same code shape in each variant; **separate** DDB tables / S3 buckets per CDK stack. |

**Harness:** `agent/handler.py` — load env, optional signals, build `system_prompt`, call **`reconcile_resolved_positions()`** (close DDB rows when Polymarket has resolved), then the Anthropic **messages + tools** loop until `end_turn` or max turns.

**Tools:** `agent/tools/__init__.py` registers JSON-schema tools; `execute_tool_call` dispatches. The model does not “import” tools; the runtime does.

**Skills (Cursor):** Not part of this package. Here, the closest analogue is **`strategy.md`** as an externalized policy artifact.

## Runtime hooks (parity with main agent)

- **`reconcile_resolved_positions`** (`tools/positions.py`): scans open/pending rows, checks CLOB market payload for resolution, writes `close_price`, `pnl_usd`, `close_reason=resolution`.
- **Paginated DDB scans** (`tools/ddb.py`): `get_open_positions` / `get_open_or_pending_positions` use `_scan_all_pages` so large tables do not silently miss rows after 1 MB scan limits.
- **Tier 3 (2026-05-15):** `MAX_ABS_LOSS_USD` dollar stop, VWAP fill on `close_position`, `close_order_id` on DDB, `event_slug` on new positions, Gamma top-300 markets.
- **Tier 1 env** in each variant’s `infra/stack.py` matches root (`TAKE_PROFIT_PCT=0.25`, etc.).

## Deploy

`deploy.sh` installs **manylinux2014_aarch64** wheels into `_build/` so **Lambda on ARM64** matches production (plain `pip install -t` from macOS can produce broken `pydantic_core` etc.).

## Strategy knobs in the prompt

- **Agent B** — `handler.py` runs `${MIN_DISAGREEMENT}`, `${MIN_ENTRY_PRICE}`, `${MAX_ORDER_USD}`, `${MAX_PER_THEME}`, and shared risk env keys through `strategy.md` before the model sees it. Keep numbers in **one** place (Lambda env / CDK stack); the markdown uses placeholders like ``${MIN_DISAGREEMENT}``.
- **Agent C** — same pattern for `MIN_EDGE`, `KELLY_FRACTION`, `MAX_NEW_ORDERS_PER_RUN`, `EVIDENCE_INTERACTION_CAP`, plus the shared rails above. **Agent A** reads `strategy.md` verbatim (no substitution) — its thresholds are aligned with the root `agent/system_prompt.py` + CDK env.

## Schedule alignment

All three stacks use `events.Schedule.cron(minute="0/15")` so A/B/C fire on the **same** wall-clock ticks (…:00, :15, :30, :45). `rate(15 minutes)` would drift per deploy time and decorrelate the Gamma snapshot across variants.

## Pre-deploy drift check

Run `ABC_TEST/tools/check_variant_drift.sh` before shipping — it asserts the duplicated `agent/tools/*.py`, `signals.py`, `settlement.py`, `dashboard_history.py`, and shared risk env keys in `infra/stack.py` are still byte-identical across A/B/C. `scripts/deploy_abc_all.sh` runs this automatically.

## Evaluation matrix

| Agent | Data | Strategy |
| --- | --- | --- |
| A | CSV signals | baseline `strategy.md` |
| B | none (vanilla prompt) | strategy without CSV-dependent sections |
| C | CSV (same path pattern as A) | alternate `strategy.md` |

Profit attribution: compare stacks with identical capital/risk env vars where possible; differences then isolate **data vs strategy** (execution is shared in design, isolated per deploy in infra).
