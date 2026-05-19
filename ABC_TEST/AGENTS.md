# ABC experiment — agent identities

Each variant has its **own Polymarket portfolio** (separate wallet, DDB table, S3 bucket, Lambda).

| Agent | Data | Strategy | Portfolio address |
|-------|------|----------|-------------------|
| **A** | Reppo `feedback.csv` | Strategy 1 (baseline thresholds) | `0x4aA88C56208864fd53035B16B7EeE6E887d5c63F` |
| **B** | None (LLM-only) | Strategy 1 (disagreement gate) | `0xb14Cf74847ffA6bC9EbE4030cb73C40eEc699112` |
| **C** | Same CSV as A | Strategy 2 (Bayesian / Kelly) | `0x2e31030b9d3365d6D28c7B2bE794e1c7b2741003` |

## Env mapping

In each `ABC_TEST/agent{X}/.env` (gitignored):

- `POLYMARKET_WALLET_ADDRESS` — **portfolio address** above (CLOB funder + balance checks).
- `POLYGON_PRIVATE_KEY` — EOA that signs orders for that portfolio (`0x` prefix required).

Do **not** use a separate “deposit” address in config; it caused dashboard vs Polymarket UI mismatches.

## Polymarket UI

- **Trade history** — filled trades only.
- **Open orders** — resting limits (`pending` in our dashboard, `size_shares = 0` until filled).
- Check the portfolio address for **that agent** (A/B/C are different accounts).

## AWS resources

| Agent | Lambda | DDB table |
|-------|--------|-----------|
| A | `abc-agent-a` | `abc-positions-a` |
| B | `abc-agent-b` | `abc-positions-b` |
| C | `abc-agent-c` | `abc-positions-c` |
