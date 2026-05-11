# Geopolitical Prediction Market Trading Agent
## Technical Design Document & Engineering Specification
**Version 1.0 · April 2026 · MVP / POC**

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [System Architecture](#2-system-architecture)
3. [Agent Framework](#3-agent-framework)
4. [Data Layer](#4-data-layer)
5. [Agent Tools & Skills](#5-agent-tools--skills)
6. [System Prompt Design](#6-system-prompt-design)
7. [Wallet Infrastructure — Privy](#7-wallet-infrastructure--privy)
8. [Runtime — AWS Lambda](#8-runtime--aws-lambda)
9. [Risk Management](#9-risk-management)
10. [Testing Strategy](#10-testing-strategy)
11. [Sequenced Build Plan](#11-sequenced-build-plan)
12. [Open Questions](#12-open-questions)
13. [Appendix: Key References](#appendix-key-references)

---

## 1. Executive Summary

This document is the authoritative engineering specification for the **Geopolitical Prediction Market Trading Agent** — an autonomous AI agent that reads crowdsourced geopolitical intelligence signals, matches them to live Polymarket prediction markets, and places and manages trades based on crowd sentiment divergence.

The agent is a single Python Lambda function triggered on a cron schedule. It preprocesses a curated CSV of geopolitical headlines with crowd voting signals, injects those signals into an Anthropic Claude system prompt, and runs a two-phase tool-use loop: first managing open positions (take-profit / stop-loss), then seeking new bets where crowd sentiment diverges meaningfully from the current market price.

**Key design principles guiding every decision in this spec:**

- **Simplest viable infrastructure.** No Bedrock, no RAG, no AgentCore. The CSV is small enough to preprocess in memory at runtime.
- **Risk-first agent loop.** Position management always runs before new entry logic.
- **Crowd signal as directional bias, not calibrated probability.** The system identifies divergence, not a fair-value price.
- **All risk parameters are environment variables.** No code changes required to adjust trading behaviour.
- **DynamoDB as the position ledger.** Single source of truth for open trades and P&L history.

---

## 2. System Architecture

### 2.1 Component Overview

| Component | Technology Choice | Rationale |
|---|---|---|
| Data source | CSV in S3 | 160-row file — no database needed. S3 is the natural Lambda data store. |
| Signal preprocessing | Python in-memory | `preprocess_signals()` runs at cold start. No vector DB, no embeddings. |
| Agent framework | Anthropic SDK (direct) | Tool-use loop is 50 lines. LangChain/Strands add complexity with no benefit here. |
| LLM | claude-sonnet-4-6 | Best balance of reasoning quality and latency for tool-use orchestration. |
| Runtime | AWS Lambda (5 min timeout) | Agent completes in <2 min. EventBridge cron triggers. No persistent session needed. |
| Position store | DynamoDB | Single-table design. Replaces fragile CLOB trade-history reconstruction. |
| Wallet | Privy server wallet (Polygon) | TEE key storage, policy engine (spend caps, protocol whitelist). Polygon-native for Polymarket. |
| Trading venue | Polymarket CLOB (Polygon) | `py-clob-client` library. EIP-712 signed limit orders. |
| Secrets | AWS Secrets Manager | API keys and wallet credentials. Referenced by Lambda IAM role. |
| Scheduling | EventBridge rule (cron) | Runs agent every N hours. Configurable. |

### 2.2 Data Flow

A single agent run from trigger to completion:

1. EventBridge fires Lambda on schedule (default: every 4 hours).
2. Lambda cold start: fetch CSV from S3, run `preprocess_signals()`, build system prompt with crowd signal table injected.
3. **Phase 1 — Position management:** call `get_positions()` from DynamoDB + live CLOB prices. For any position where `hit_take_profit` or `hit_stop_loss` is true, call `close_position()` and update DDB record.
4. **Phase 2 — New entry:** call `check_balance()` (Privy wallet). If `ok_to_trade`, call `get_open_markets()` from Polymarket CLOB, match questions to crowd signals, identify divergence, call `place_order()` if edge found. Write new position to DDB.
5. Agent loop ends on `end_turn` stop reason. Lambda exits.

### 2.3 Project Structure

```
geo-trading-agent/
├── agent/
│   ├── handler.py              # Lambda entrypoint + agent loop
│   ├── signals.py              # preprocess_signals() — CSV → prompt string
│   ├── system_prompt.py        # System prompt template
│   └── tools/
│       ├── __init__.py         # TOOLS list + TOOL_MAP
│       ├── positions.py        # get_positions(), close_position()
│       ├── markets.py          # get_open_markets()
│       ├── wallet.py           # check_balance(), place_order()
│       └── ddb.py              # DynamoDB read/write helpers
├── infra/
│   ├── app.py                  # CDK app entry
│   └── stack.py                # S3 + DynamoDB + Lambda + EventBridge
├── scripts/
│   └── upload_csv.py           # One-time: push CSV to S3
├── tests/
│   ├── test_signals.py         # Unit: preprocess_signals()
│   └── test_tools_mock.py      # Integration: tools with mocked CLOB/Privy
├── requirements.txt
└── cdk.json                    # CDK app config
```

---

## 3. Agent Framework

### 3.1 Framework Selection

| Framework | Verdict | Reason |
|---|---|---|
| Anthropic SDK (direct) | ✅ **Chosen** | Tool-use loop is native. No abstraction overhead. Full control over prompt and message history. 50 lines of code. |
| LangChain | ❌ Rejected | Significant abstraction overhead for a 5-tool agent. Debugging prompt injection is harder. Adds 10+ transitive dependencies. |
| AWS Strands | ❌ Rejected | Built around Bedrock, not the Anthropic API. Would require model ID changes, different SDK, different rate limits, and marginal behaviour differences. |

### 3.2 Agent Loop Pattern

The agent uses Anthropic's native tool-use loop. No framework needed:

```python
messages = [{"role": "user", "content": "Run trading analysis."}]

while True:
    response = client.messages.create(
        model=MODEL,
        system=SYSTEM,
        tools=TOOLS,
        messages=messages,
        max_tokens=4096,
    )
    if response.stop_reason == "end_turn":
        break
    tool_results = execute_tool_calls(response.content)
    messages.append({"role": "assistant", "content": response.content})
    messages.append({"role": "user", "content": tool_results})
```

The loop continues until Claude stops calling tools (`end_turn`). Typical runs: 4–8 tool calls, 60–90 seconds total runtime.

### 3.3 AgentCore — Explicitly Not Used

AWS AgentCore was evaluated and rejected for this stage:

- AgentCore requires switching from the Anthropic SDK to the `boto3` Bedrock client, which changes model IDs, rate limits, and introduces Bedrock-specific pricing on top of model costs.
- AgentCore's managed session and persistent memory features are irrelevant for a stateless cron job that runs, completes, and exits.
- **AgentCore becomes relevant if the agent evolves to:** (a) maintain memory across runs about its own P&L history, (b) run as a continuously-observing agent rather than a periodic batch job, or (c) orchestrate sub-agents. Revisit at that point.

---

## 4. Data Layer

### 4.1 CSV in S3

The geopolitical signal file (`geopolitics-dump.csv`) is stored in a private S3 bucket and fetched at Lambda cold start. The file is ~160 rows and reads in ~5ms. There is no need for a vector database or embedding index at this data volume.

- **S3 key:** `geo-signals/geopolitics-dump.csv`
- The Lambda execution role has `s3:GetObject` permission on this bucket only.
- Periodic refresh of the CSV is manual for MVP (operator uploads new version to S3). Automated refresh can be added later via an S3 trigger.

#### 4.1.1 CSV Schema

The file has one row per vote (one voter, one topic). ~160 rows across 51 topics and 26 voters.

| Column | Type | Description |
|---|---|---|
| `pod_name` | string | Topic headline (e.g. `"Hormuz Updates"`). Groups rows into a single signal. |
| `votes` | float | Voting power this voter committed to this topic. |
| `voting_power` | float | Voter's total available voting power on the platform. |
| `sentiment` | string | `UP` or `DOWN` — the voter's directional call. |
| `feedback` | string | Optional free-text comment. May be empty or NaN. |

`preprocess_signals()` groups by `pod_name`, then aggregates:
- `up_vp = sum(votes where sentiment == 'UP')`
- `down_vp = sum(votes where sentiment == 'DOWN')`
- `weighted_score`, `max_conviction`, `interactions`, `comment` — see §4.2 formulas.

### 4.2 Signal Preprocessing

`preprocess_signals()` in `agent/signals.py` reads the CSV and computes the following per topic (`pod_name`):

| Field | Formula | Meaning |
|---|---|---|
| `weighted_score` | `(up_vp − down_vp) / (up_vp + down_vp)` | Signed conviction, −1.0 to +1.0. Primary trading signal. |
| `crowd_direction` | `YES if weighted_score ≥ 0, else NO` | The directional outcome the crowd believes in. |
| `max_conviction` | `max(votes / voting_power)` across all voters for this topic | Depth of individual commitment. 0.0 to 1.0. |
| `interactions` | Count of vote rows for this topic | Signal reliability proxy. Filter threshold: ≥ 3. |

The formatted signal table is injected verbatim into the system prompt at runtime. This is the complete data pipeline for MVP.

> ⚠️ **Critical framing:** `weighted_score` is a directional bias, not a calibrated probability. Do not treat it as a fair-value price. The trading edge is divergence between crowd direction and current market price — not a predicted outcome probability. There is no ground-truth outcome data in the current CSV to support probability estimation.

### 4.3 DynamoDB — Position Ledger

DynamoDB is the single source of truth for open positions. It replaces the fragile approach of reconstructing positions from CLOB trade history on every run.

**Table name:** `geo-trading-positions`

| Attribute | Type | Notes |
|---|---|---|
| `token_id` | String (PK) | Polymarket ERC-1155 token ID. Unique per outcome per market. |
| `market_id` | String (GSI PK) | `conditionId` — groups YES and NO tokens for the same market. |
| `question` | String | Full market question text. For human readability and audit. |
| `outcome` | String | `YES` or `NO` |
| `entry_price` | Number | Price per share at time of fill. 0.01–0.99. |
| `size_shares` | Number | Net shares held. Updated on partial close. |
| `entry_time` | String (ISO 8601) | UTC timestamp of position open. |
| `source_headline` | String | The `pod_name` that triggered this trade. |
| `crowd_score` | Number | `weighted_score` at time of entry. Audit trail. |
| `status` | String | `open` \| `closed` |
| `close_reason` | String | `take_profit` \| `stop_loss` \| `expired` \| null |
| `close_price` | Number | Actual fill price on close. Null while open. |
| `close_time` | String (ISO 8601) | UTC timestamp of close. Null while open. |
| `pnl_usd` | Number | Realised P&L in USD. Null while open. |

**GSI:** `market_id-index` — allows querying all positions (YES + NO) for a given market. Used to prevent double-exposure to the same market on opposite sides.

**Write operations:**
- On `place_order()` success → write new item with `status=open`
- On `close_position()` success → update item: `status=closed`, `close_reason`, `close_price`, `close_time`, `pnl_usd`
- On partial close → update `size_shares` in place

---

## 5. Agent Tools & Skills

The agent has exactly **five tools**. Each tool has a Python implementation and an Anthropic tool schema registered in `TOOL_MAP`. Tools are called by the LLM; the loop executes them and returns results.

### 5.1 `get_positions`

**File:** `agent/tools/positions.py`

Reads all `status=open` items from DynamoDB. For each position, fetches the live CLOB best-bid price (what we'd receive selling right now). Computes P&L and pre-calculates `hit_take_profit` and `hit_stop_loss` boolean flags so the agent does not need to do arithmetic.

**Returns per position:**

| Field | Source | Description |
|---|---|---|
| `token_id` | DDB | ERC-1155 token ID |
| `question` | DDB | Market question text |
| `outcome` | DDB | YES or NO |
| `entry_price` | DDB | Price paid per share |
| `current_price` | CLOB live | Best bid (sell price right now) |
| `pnl_pct` | Computed | `(current − entry) / entry` |
| `pnl_usd` | Computed | `(current − entry) × size_shares` |
| `hit_take_profit` | Computed | `pnl_pct ≥ TAKE_PROFIT_PCT` |
| `hit_stop_loss` | Computed | `pnl_pct ≤ −STOP_LOSS_PCT` |

> Always call this tool **first** in each run, before any new entry logic. The system prompt enforces this ordering.

### 5.2 `close_position`

**File:** `agent/tools/positions.py`

Places a `SELL` limit order on the CLOB for the specified token. Updates the DDB record on successful order submission.

**Input parameters:**

| Parameter | Type | Description |
|---|---|---|
| `token_id` | string | From `get_positions()` |
| `size_shares` | number | Full position size to sell |
| `limit_price` | number | Minimum acceptable fill price |
| `reason` | enum | `take_profit` \| `stop_loss` \| `manual` |

**Limit price guidance** (encoded in system prompt):
- **Stop-loss:** `current_price − 0.02` — accept a slightly worse fill to guarantee exit. Being stubborn on price when cutting a loss compounds the loss.
- **Take-profit:** `current_price` — no urgency, the position is already winning.

### 5.3 `get_open_markets`

**File:** `agent/tools/markets.py`

Fetches active, non-resolved markets from the Polymarket CLOB API via `py-clob-client`. Filters to markets where `active=true` and `closed=false`, sorts by `volume` descending, and returns the top 100. Returns simplified dicts: `question`, `market_id`, `yes_token`, `no_token`, `yes_price`, `no_price`. Top-100 by volume keeps the injected list to ~4k tokens. Called in Phase 2 only, after balance check passes.

**Initialization (module-level, once per Lambda cold start):**
```python
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

clob = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=POLYGON,
    key=POLYMARKET_PRIVATE_KEY,          # Polygon wallet private key from Secrets Manager
    funder=POLYMARKET_WALLET_ADDRESS,    # Privy server wallet address
    signature_type=2,                    # EOA signing
)
clob.set_api_creds(clob.create_or_derive_api_creds())
```

`POLYMARKET_PRIVATE_KEY` is the private key for the Privy server wallet. Fetched from Secrets Manager at Lambda init. See §7.4 for how this is obtained from Privy.

### 5.4 `check_balance`

**File:** `agent/tools/wallet.py`

Reads the Privy server wallet USDC balance on Polygon mainnet. Returns:
- `usdc`: float — current balance
- `ok_to_trade`: bool — `balance ≥ MIN_BALANCE_RESERVE`

Phase 2 aborts entirely if `ok_to_trade` is false. Always called before `place_order`.

### 5.5 `place_order`

**File:** `agent/tools/wallet.py`

Builds a BUY limit order, signs it via Privy's EIP-712 signing API, submits to Polymarket CLOB, and writes a new position record to DynamoDB on success.

**Input parameters:**

| Parameter | Type | Description |
|---|---|---|
| `market_id` | string | `conditionId` from `get_open_markets()` |
| `outcome` | enum | `YES` \| `NO` |
| `size_usdc` | number | Dollar amount. Hard-capped at `MAX_ORDER_USD` in code. |
| `limit_price` | number | Price per share. 0.01–0.99. |

**Pre-flight validations before submitting (enforced in code, not just prompt):**
1. `size_usdc = min(size_usdc, MAX_ORDER_USD)` — hard cap regardless of what the LLM passes
2. `limit_price` within 5% of current market price — sanity check
3. No existing `open` DDB record for this `token_id` — prevent double entry
4. No existing `open` DDB record for this `market_id` on the opposite side — prevent opposing positions

---

## 6. System Prompt Design

### 6.1 Structure

The system prompt has two sections assembled at runtime:

**Section 1 — Crowd signal table** (computed by `preprocess_signals()`, injected per run):

```
CROWD SIGNALS FROM GEOPOLITICAL INTELLIGENCE PLATFORM
======================================================
Topic: "Hormuz Updates"
  crowd_direction=YES  weighted_score=+1.00  max_conviction=1.00  interactions=23
  comment: "Close or open Hormuz is just another way to play the markets"

Topic: "US is in recession by Q4"
  crowd_direction=YES  weighted_score=+0.97  max_conviction=1.00  interactions=5

Topic: "Ceasefire collapses before April 22 expiry"
  crowd_direction=NO  weighted_score=-1.00  max_conviction=1.00  interactions=4

Topic: "CENTCOM says Iran can no longer project power"
  crowd_direction=NO  weighted_score=-1.00  max_conviction=1.00  interactions=3

Topic: "Oil back above $100 before Saturday talks"
  crowd_direction=NO  weighted_score=-1.00  max_conviction=1.00  interactions=2
...
```

**Section 2 — Trading instructions** (static template, env var substitution):

```
EACH RUN: follow this sequence exactly.

PHASE 1 — MANAGE EXISTING RISK FIRST
1. Call get_positions()
2. For every position where hit_take_profit=true or hit_stop_loss=true:
   - Call close_position() immediately
   - stop-loss:   limit_price = current_price - 0.02  (fill > price)
   - take-profit: limit_price = current_price          (patient)
3. State your reasoning for each close or hold decision.

Current risk thresholds:
  Take profit : +{TAKE_PROFIT_PCT}% return on entry  → hit_take_profit flag
  Stop loss   : -{STOP_LOSS_PCT}% loss on entry      → hit_stop_loss flag

PHASE 2 — SEEK NEW ENTRY (only after Phase 1 complete)
4. Call check_balance() — abort Phase 2 if ok_to_trade=false
5. Call get_open_markets()
6. Match questions to crowd signals using your own reasoning.
   (Handle paraphrase, abbreviation, semantic equivalence yourself.)
7. Enter only when ALL conditions are met:
   - |weighted_score| > 0.70
   - max_conviction > 0.30
   - interactions >= 3
   - crowd says YES and yes_price < 0.40, OR
     crowd says NO  and yes_price > 0.60
8. Max 1 new order, max ${MAX_ORDER_USD} size.
   Set limit_price within 5% of current market price.

If no edge found: explain why and place no order.
Think step by step. Manage existing risk before seeking new reward.
```

### 6.2 Matching Logic

The agent uses its own reasoning to match Polymarket question strings to crowd signal topics. This is **deliberately left to the LLM** rather than implemented as a string-matching algorithm — Claude handles paraphrase, abbreviation, and semantic equivalence better than any hand-written matcher at this scale.

**Example matchings the agent should make autonomously:**

| Crowd topic | Polymarket question (example) | Action |
|---|---|---|
| "Ceasefire collapses before April 22" (NO, score −1.0) | "Will the US-Iran ceasefire hold through May?" | Buy YES — crowd says ceasefire holds |
| "US is in recession by Q4" (YES, score +0.97) | "Will the US enter recession in 2025?" | Buy YES — crowd agrees |
| "CENTCOM: Iran can't project power" (NO, score −1.0) | "Is Iran still capable of projecting military force?" | Buy YES — crowd rejects CENTCOM claim |
| "Oil back above $100 before talks" (NO, score −1.0) | "Will Brent crude exceed $100 before June?" | Buy NO — crowd disbelieves oil spike |

---

## 7. Wallet Infrastructure — Privy

### 7.1 Why Privy Over Coinbase CDP

| Criterion | Privy | Coinbase CDP / AgentKit |
|---|---|---|
| Key security | TEE + key sharding. Keys never fully reconstruct outside secure enclave. | MPC. Solid but less granular policy controls. |
| Policy engine | Server-side policy: spend caps, approved protocols, time windows — enforced before signature, not in application code. | No built-in spending limit features. |
| Chain fit | Multi-chain EVM including Polygon mainnet (Polymarket's native chain). | Optimised for Base L2. Polygon requires additional tooling or bridging. |
| Agent model | Model 1: agent-controlled, developer-owned. Full autonomy within defined policy. | Programmatic signing. No policy layer. |
| Status | Acquired by Stripe (June 2025). Enterprise-grade SLAs. | Native Coinbase product. Base-centric roadmap. |

### 7.2 Privy Setup Steps

1. Create **authorization keys** in Privy Dashboard. Store the corresponding private key in AWS Secrets Manager at `/geo-agent/privy-auth-key`.
2. Create a **server wallet** via Privy API: `network=polygon-mainnet`, `owner_id=<auth-key-id>`.
3. Attach a **policy** to the wallet:
   - Max per-transaction USDC spend = `MAX_ORDER_USD`
   - Approved protocol addresses = Polymarket CLOB contract only
   - Time window = business hours optional (MVP: unrestricted)
4. Fund the wallet with Polygon USDC. Recommended: $50–$100 for POC.
5. Store `wallet_id` in Secrets Manager at `/geo-agent/privy-wallet-id`. Lambda fetches at cold start.

### 7.3 Key Export from Privy (POC Assumption)

`py-clob-client` signs orders using a raw Polygon private key passed at initialization. Privy's TEE keeps the key inside a secure enclave and exposes a `sign_typed_data` API — but wiring that custom signer into `py-clob-client`'s internals is a non-trivial integration not yet documented publicly.

**POC assumption:** Export the private key from Privy once at wallet creation time and store it in AWS Secrets Manager at `/geo-agent/polygon-private-key`. The Lambda fetches this key at cold start and passes it to `ClobClient(key=...)`. The Privy server wallet remains the on-chain address that holds USDC and executes trades — the private key is simply also available in Secrets Manager.

This gives:
- Privy spend-cap policy still enforced at the on-chain level (wallet policy blocks oversized transactions regardless of what the key signs).
- Key at rest encrypted via Secrets Manager KMS.
- No bespoke signer shim required for Phase 3–5.

**Production upgrade path (post-POC):** Implement a `PrivySigner` class that calls `privy_client.wallets.sign_typed_data(wallet_id, typed_data)` and patches `py-clob-client`'s signing step, then rotate the key in Secrets Manager to a placeholder. Revisit once the POC has proven signal quality.

### 7.4 Privy Key Export (Phase 3 Step)

When creating the server wallet (§7.2), call:

```python
from privy import Client as PrivyClient

privy = PrivyClient(app_id=PRIVY_APP_ID, app_secret=PRIVY_APP_SECRET)
key_material = privy.wallets.export(wallet_id=PRIVY_WALLET_ID)
# key_material["private_key"] is the raw hex key — write to Secrets Manager immediately
```

Store the exported key in Secrets Manager at `/geo-agent/polygon-private-key`. Delete it from local memory. This step is one-time at wallet creation.

> ⚠️ **Spike this on Phase 3 Day 1.** Confirm that the Privy wallet address derived from the exported key matches `POLYMARKET_WALLET_ADDRESS`. If Privy does not support key export, fall back to generating a fresh Polygon key locally, importing it into Secrets Manager, and funding that address directly (bypassing Privy for POC). See [Polymarket CLOB auth docs](https://docs.polymarket.com/#authentication).

---

## 8. Runtime — AWS Lambda

### 8.1 Lambda Configuration

| Parameter | Value | Rationale |
|---|---|---|
| Runtime | Python 3.12 | Latest stable Lambda Python runtime. |
| Memory | 512 MB | Agent loop is I/O-bound (API calls). Not memory-bound. |
| Timeout | 300 seconds | Typical run: 60–120s. Buffer for slow CLOB responses. |
| Architecture | arm64 (Graviton2) | ~15% cheaper than x86 for same I/O-bound workload. |
| Trigger | EventBridge cron | Default: `rate(4 hours)`. Adjust via infrastructure config. |
| Package | Zip with dependencies | `anthropic`, `py-clob-client`, `boto3`. |

### 8.2 IAM Permissions (Least Privilege)

Lambda execution role requires exactly:

```
s3:GetObject               arn:aws:s3:::geo-signals-bucket/geo-signals/*
dynamodb:GetItem           arn:aws:dynamodb:*:*:table/geo-trading-positions
dynamodb:PutItem           arn:aws:dynamodb:*:*:table/geo-trading-positions
dynamodb:UpdateItem        arn:aws:dynamodb:*:*:table/geo-trading-positions
dynamodb:Query             arn:aws:dynamodb:*:*:table/geo-trading-positions/index/*
secretsmanager:GetSecretValue  arn:aws:secretsmanager:*:*:secret:/geo-agent/*
logs:CreateLogGroup        *
logs:CreateLogStream       *
logs:PutLogEvents          *
```

No other permissions. No `*` wildcards on resource ARNs.

### 8.3 Environment Variables

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — (required) | Fetched from Secrets Manager at init. |
| `POLYMARKET_API_KEY` | — (required) | Polymarket CLOB API key. |
| `POLYMARKET_API_SECRET` | — (required) | |
| `POLYMARKET_API_PASSPHRASE` | — (required) | |
| `POLYMARKET_WALLET_ADDRESS` | — (required) | Polygon address of Privy wallet. |
| `PRIVY_API_KEY` | — (required) | Privy API key for wallet operations. |
| `PRIVY_WALLET_ID` | — (required) | ID of the agent's server wallet. |
| `POLYGON_PRIVATE_KEY` | — (required) | Raw hex private key for the Privy server wallet. Fetched from Secrets Manager. Used by py-clob-client for EIP-712 order signing. |
| `S3_BUCKET` | — (required) | Bucket containing the CSV. |
| `S3_KEY` | `geo-signals/geopolitics-dump.csv` | S3 object key. |
| `DDB_TABLE` | `geo-trading-positions` | DynamoDB table name. |
| `TAKE_PROFIT_PCT` | `0.50` | Close when P&L ≥ +50% of entry price. |
| `STOP_LOSS_PCT` | `0.30` | Close when P&L ≤ −30% of entry price. |
| `MIN_BALANCE_RESERVE` | `15.0` | Min USDC balance to permit new orders. |
| `MAX_ORDER_USD` | `10.0` | Max dollar size per order. Hard-capped in code. |
| `DRY_RUN` | `false` | Set `true` to log trades without executing. |

All secret values (`ANTHROPIC_API_KEY`, `POLYMARKET_*`, `PRIVY_*`, `POLYGON_PRIVATE_KEY`) must be stored in AWS Secrets Manager and fetched at Lambda init — never hardcoded or stored as Lambda env vars in plaintext.

### 8.4 Error Handling Policy

**Principle: fail fast, log everything, never silently continue.**

| Failure scenario | Behaviour |
|---|---|
| Any unhandled exception in a tool | Log full traceback to CloudWatch, re-raise — Lambda exits non-zero. EventBridge marks the invocation as failed. |
| CLOB API error on `place_order` or `close_position` | Log error + order params, raise — do not retry. Stale data is safer than a duplicate order. |
| DDB write failure **after** a successful CLOB order | Log CRITICAL with order details, retry DDB write once (immediate), then raise. Operator must reconcile manually via CloudWatch log. |
| `check_balance()` returns `ok_to_trade=false` | Abort Phase 2 cleanly (not an error). Log balance and min threshold. |
| `get_positions()` returns empty | Continue normally — no positions to manage. |
| Agent loop exceeds 10 iterations | Raise `RuntimeError("agent loop safety limit exceeded")`. Should never happen; indicates runaway tool-call loop. |

No retries on CLOB or Privy API calls except the single DDB post-order retry. An EventBridge alarm on Lambda error metrics covers operator notification.

---

## 9. Risk Management

### 9.1 Position-Level Controls

| Control | Implementation | Where enforced |
|---|---|---|
| Take-profit | Close when `pnl_pct ≥ TAKE_PROFIT_PCT` | `get_positions()` flag + system prompt |
| Stop-loss | Close when `pnl_pct ≤ −STOP_LOSS_PCT` | `get_positions()` flag + system prompt |
| Max order size | `min(size_usdc, MAX_ORDER_USD)` | Hard-capped in `place_order()` Python code |
| Balance gate | `ok_to_trade = balance ≥ MIN_BALANCE_RESERVE` | `check_balance()` return value |
| No double entry | Check DDB before `place_order()` | `place_order()` pre-flight validation |
| Wallet-level cap | Privy policy: per-tx spend cap + approved protocols | Privy TEE — enforced before any signature |
| Max 1 trade per run | System prompt instruction | Claude's tool-use planning |
| Limit price sanity | Within 5% of current market price | System prompt + `place_order()` validation |

### 9.2 Signal Quality Entry Thresholds

A trade is only considered when **all three** signal quality conditions AND the price divergence condition are met simultaneously:

**Signal quality (all three required):**
- `|weighted_score| > 0.70` — strong directional conviction
- `max_conviction > 0.30` — at least one voter staked >30% of their total voting power
- `interactions ≥ 3` — more than 2 independent votes on this topic

**Price divergence (one of two required):**
- Crowd says YES and `yes_price < 0.40` — market underpricing the crowd's call
- Crowd says NO and `yes_price > 0.60` — market overpricing what the crowd rejects

> **Recommended POC settings:** Start with `TAKE_PROFIT_PCT=0.40`, `STOP_LOSS_PCT=0.20`, `MAX_ORDER_USD=5`. Tighter parameters reduce exposure while signal quality is being validated against live outcomes.

### 9.3 Known Limitations of the Signal

These must be understood before interpreting any P&L results:

- The CSV covers **3 weeks** of data from a **single community** (26 voters, 51 topics). This is too thin to validate signal quality statistically. The crowd may not be predictive.
- Most upvote rates are 100% — the binary upvote/downvote has low discriminating power. The real signal variance is in `weighted_score` and `max_conviction`.
- There is **no outcome ground-truth** in the current dataset. The system cannot self-validate. Live trading P&L history is the validation dataset. Prioritise logging.
- `crowd_direction` is a directional bias, not a calibrated probability. Do not use it as a fair-value price in any downstream calculation.
- `EVOF` (Economic Value of Feedback) scoring from the Reppo platform is not implemented. The breadth component collapses to `interactions` at 26 voters. Revisit when unique voter count per topic exceeds ~200.

---

## 10. Testing Strategy

### 10.1 Unit Tests

**`tests/test_signals.py`** — test `preprocess_signals()` with fixture CSVs:

- Correct `weighted_score` for known `voting_power` and `votes` inputs
- `max_conviction` calculation: `votes / voting_power`
- Topics with zero interactions filtered from output
- Edge cases: all upvotes (`score=+1.0`), all downvotes (`score=−1.0`), single voter
- Free-text comment extraction from `feedback` column

### 10.2 Integration Tests (Mocked)

**`tests/test_tools_mock.py`** — test agent tools with mocked external services:

- `get_positions()` with mocked DDB QueryResponse and mocked CLOB order book
- `close_position()` verifies correct SELL order parameters passed to mocked CLOB client
- `place_order()` verifies `MAX_ORDER_USD` hard cap regardless of input value
- `place_order()` verifies DDB write after successful mocked CLOB response
- `check_balance()` returns `ok_to_trade=false` when balance < `MIN_BALANCE_RESERVE`
- `place_order()` is blocked when an open DDB position exists for the same `token_id`

### 10.3 Paper Trading Phase (Required)

Before any live capital, run the agent in dry-run mode for a **minimum of 1 week**:

- Set `DRY_RUN=true` — `place_order()` and `close_position()` log the would-be action and return a mock success response without calling the CLOB or writing to DDB. All read tools (`get_positions()`, `get_open_markets()`, `check_balance()`) hit live services normally so the agent's reasoning is realistic.
- Review CloudWatch logs every 1–2 days: are matched markets sensible? Is divergence logic triggering on reasonable signals?
- After 1 week: review hypothetical P&L against actual Polymarket outcomes manually

**Do not skip this phase.** It is the only way to validate that the LLM's market matching is working correctly before committing real capital.

---

## 11. Sequenced Build Plan

| Phase | Scope | Exit Criteria |
|---|---|---|
| **1 — Signal pipeline** | S3 upload script, `preprocess_signals()`, unit tests | Correct `weighted_score` and `max_conviction` for all 51 topics. Signal string injects cleanly into a test prompt. |
| **2 — Read-only market tools** | `get_open_markets()`, `check_balance()` (read-only mock), DDB table + GSI creation | Agent fetches live Polymarket questions and logs matched signals with divergence scores. No wallet. No orders. |
| **3 — Privy wallet setup** | Create server wallet on Polygon, attach policy, fund with $50 USDC, wire `check_balance()` to live wallet | `balance > 0`, `ok_to_trade=true` confirmed. Privy EIP-712 signing tested end-to-end against Polymarket testnet or a small real order. |
| **4 — Paper trading** (`DRY_RUN=true`) | Full agent loop deployed to Lambda with dry-run guard. All 5 tools wired. | 1 week of CloudWatch logs. Review: matching quality, divergence signals, hypothetical P&L vs actual outcomes. |
| **5 — Live with small capital** | Remove dry-run guard. `MAX_ORDER_USD=5`, `STOP_LOSS_PCT=0.20`, `TAKE_PROFIT_PCT=0.40` | First real trade confirmed on Polygon. Position in DDB. `close_position()` exercised through a full TP/SL cycle. |
| **6 — Signal expansion** | Ingest additional CSV epochs. Evaluate signal predictive power from Phase 5 P&L data. | Backtested signal quality assessed. Thresholds adjusted based on evidence. EVOF scoring added if voter count warrants it. |

**Timeline estimate:** Phase 1–3 = ~4 days engineering. Phase 4 = 1 calendar week (non-negotiable). Phase 5 = 1 day engineering + 1 week monitored operation. **Total to first live trade: ~8–9 days elapsed.**

---

## 12. Open Questions

| Question | Current Answer | When to Revisit |
|---|---|---|
| Does the crowd signal have predictive power? | Unknown. No outcome ground-truth in the CSV. | After 4 weeks of live trading with CloudWatch P&L logs. |
| How often should the agent run? | 4 hours default. Geopolitical markets move slowly. | After Phase 5. Adjust based on observed market movement speed. |
| Should partial closes be supported? | No — `close_position()` always sells full `size_shares`. | Phase 6. Requires DDB update logic for partial fills. |
| Should EVOF scoring be implemented? | No — 26 voters collapses breadth component to `interactions`. | When unique voter count per topic exceeds ~200. |
| Multiple positions per market? | Blocked — `place_order()` rejects if any DDB record exists for `market_id`. | Probably never. Holding both sides eliminates P&L edge. |
| Virtuals Protocol integration? | Out of scope. Would require tokenizing the agent and sharing P&L with token holders. | If the agent is productised for external users or competition entry. |
| AgentCore migration? | Not needed now. | If agent evolves to continuous operation, multi-agent orchestration, or persistent P&L memory across runs. |
| CDK vs SAM? | **CDK.** Single stack in `infra/stack.py`. SAM template removed. | N/A — resolved. |
| Privy key export supported? | Assumed yes for POC. See §7.3–7.4. | Phase 3 Day 1 spike confirms or triggers fallback to direct Secrets Manager key. |

---

## Appendix: Key References

| Resource | URL |
|---|---|
| Polymarket CLOB API docs | https://docs.polymarket.com |
| py-clob-client (Python) | https://github.com/Polymarket/py-clob-client |
| Polymarket CLOB authentication | https://docs.polymarket.com/#authentication |
| Privy agentic wallets docs | https://docs.privy.io/recipes/agent-integrations/agentic-wallets |
| Privy sign_typed_data API | https://docs.privy.io/reference/server/wallets/signing |
| Anthropic tool use guide | https://docs.anthropic.com/en/docs/tool-use |
| Anthropic claude-sonnet-4-6 | https://docs.anthropic.com/en/docs/models-overview |
| AWS Lambda Python runtime | https://docs.aws.amazon.com/lambda/latest/dg/lambda-python.html |
| AWS DynamoDB single-table design | https://aws.amazon.com/blogs/database/single-table-vs-multi-table-design |
| AWS CDK Python reference | https://docs.aws.amazon.com/cdk/api/v2/python |

---

*All decisions in this spec supersede earlier conversation artefacts. v1.0 · April 2026.*