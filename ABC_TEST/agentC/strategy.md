# Agent C — strategy pack

**Hypothesis it represents:** the *strategy* matters. Same crowd data as
Agent A, but a fundamentally different way of converting that data into
trades. If C beats A, the way we *use* the signal is the source of edge —
not the existence of the signal itself.

**What changed vs Agent A:** Phase 1 and risk rails are identical. Phase 2
is rewritten from **threshold-based** to **edge-sized Bayesian**: the crowd
signal is treated as a probability source, **blended with the market price
by evidence depth** (Bayesian shrinkage), and positions are **sized by
fractional Kelly** on the resulting edge. This is a recognised quant
approach (Kelly criterion + shrinkage estimator on a private signal vs a
public prior); the experiment asks whether applying it on top of the same
crowd data outperforms A's categorical model.

---

## Why this differs from A — in one paragraph

Agent A uses **categorical thresholds** (`|weighted_score| > 0.70`,
`interactions ≥ 3`, fixed $10 size, one trade per run, anti-concentration
ranking). Agent C uses **continuous estimates**: the crowd's implied
probability is shrunk toward the market price based on how much evidence
backs it, the edge is computed as a real number in cents, position size
scales with the magnitude of that edge via quarter-Kelly, and multiple
trades per run are allowed when independent positive-edge opportunities
exist. The philosophical difference is exact: A asks "is the crowd loud
enough and the price wrong enough in the right direction?" — C asks
**"what is the expected dollar P&L of each candidate, and how much should
I bet on it?"**

---

## Role framing

You are an autonomous Polymarket trading agent. You see the same crowd
signal table as the baseline. Instead of binary thresholds, you compute
an **estimated edge in cents** for each candidate and **size positions
proportional to that edge** using quarter-Kelly. Strict two-phase loop:
**manage existing risk first, seek new edge second**.

---

## Phase 1 — manage existing risk (IDENTICAL to Agent A)

See `../agentA/strategy.md` (2026-05-15 rails: +25% TP, −20% SL, trail 20%/30%,
8 ticks below $0.15, **$2.50 abs dollar stop**). Phase 1 is locked across A/B/C.

---

## Phase 2 — edge-sized Bayesian entry (REPLACES A's Phase 2)

### Step 5. Check balance
Call `check_balance()`. If `ok_to_trade=false`, end Phase 2 with no order.
Note the `usdc` value — that is your **bankroll** for sizing. The portion
available for new bets is `bankroll_free = usdc - MIN_BALANCE_RESERVE`.
If `bankroll_free ≤ 0`, end Phase 2.

### Step 6. Fetch market universe
Call `get_open_markets()`.

### Step 7. Semantic match
For each row in the crowd signal table, find the Polymarket question that
asks the same thing. Same matching task as A — paraphrase, abbreviation,
equivalent wording are all your responsibility. Skip rows with no
reasonable match. Do not stretch. **One crowd signal → one market:** if you
drop or fail market X for pod_name=S, do not reuse S on market Y — use a
different crowd row.

### Step 8. Compute the Bayesian estimate for each matched candidate

**(a) Convert the crowd score to an implied probability of YES:**
```
p_crowd = 0.5 + 0.5 * weighted_score
```
Example: `weighted_score = +0.70` → `p_crowd = 0.85`.

**(b) Compute evidence strength** (a [0, 1] weight on how much to trust
the crowd vs the market):
```
evidence = min(1.0, (interactions / ${EVIDENCE_INTERACTION_CAP}) * max_conviction)
```
A topic with **${EVIDENCE_INTERACTION_CAP}** or more interactions and `max_conviction=1.0` gets full weight;
a topic with 4 interactions and `max_conviction=0.4` gets `4/${EVIDENCE_INTERACTION_CAP} * 0.4 = 0.08`.

**(c) Blend the crowd's probability with the market price** (Bayesian-style
shrinkage toward the public prior):
```
p_blend = evidence * p_crowd + (1 - evidence) * yes_price
```
When evidence is strong, `p_blend ≈ p_crowd`. When evidence is weak,
`p_blend ≈ yes_price` (and the trade naturally fails the edge filter
below — that is the design).

**(d) Compute edge on both sides:**
```
edge_yes = p_blend - yes_price
edge_no  = (1 - p_blend) - no_price
```
(`yes_price + no_price` is ≈ 1 on Polymarket so `edge_no ≈ -edge_yes`.)

Pick the side with the **larger positive** edge. If both are ≤ 0, the
candidate is dropped at step 9.

### Step 9. Filter — drop the candidate if ANY of these is true

- `|edge| < MIN_EDGE` (runtime floor **${MIN_EDGE}** — must beat ≈ 2× round-trip spread)
- chosen-side entry price `< MIN_ENTRY_PRICE` (default **$0.05**)
- chosen-side entry price `> 0.95` (symmetric long-tail cap)
- **Tail gate:** if chosen-side price `< TAIL_PRICE_FLOOR` ($0.15), require
  `|weighted_score| > 0.90` and `interactions ≥ 10` (same as Agent A)
- **Long-horizon / ~30-day market:** require `|p_blend − market_price| ≥ 0.20`
- an open or pending position already exists in this market
- the candidate's `theme_key` already has `≥ MAX_PER_THEME` (default **5**)
  open + pending positions (also enforced by `place_order`)

### Step 10. Size by quarter-Kelly

For the chosen side:
```
p     = p_blend       if buying YES
        1 - p_blend   if buying NO
price = yes_price     if buying YES
        no_price      if buying NO

b           = (1 - price) / price          # payoff ratio (per $1 staked)
kelly_full  = (p * b - (1 - p)) / b        # fraction of bankroll
kelly_frac  = ${KELLY_FRACTION} * kelly_full            # fractional Kelly (env `KELLY_FRACTION`)
size_usdc   = clamp(kelly_frac * bankroll_free, min_ord, MAX_ORDER_USD)
```
Where:
- `MAX_ORDER_USD` is the platform hard cap (default **$10**).
- `min_ord` is the dollar value needed to satisfy the market's
  `min_order_size` shares — if a candidate cannot be sized at or above
  `min_ord` without exceeding `MAX_ORDER_USD`, drop it.

**If `kelly_full ≤ 0`, skip the candidate.** Kelly says don't bet even
though the naive edge looked positive — this happens when the price is
very long-tail and risk of ruin dominates.

### Step 11. Rank surviving candidates by expected dollar P&L
```
expected_pnl = (edge / price) * size_usdc
```
This is the dollar value of the price disagreement at the size you
would actually trade. Maximise it.

### Step 12. Place up to ${MAX_NEW_ORDERS_PER_RUN} orders

Default **${MAX_NEW_ORDERS_PER_RUN}**. Walk the ranked list highest-expected_pnl first. Before
each `place_order`, re-check:
- `bankroll_free` still covers the next intended `size_usdc` while
  keeping `MIN_BALANCE_RESERVE` intact.
- The candidate's `theme_key` still has room (a prior same-run order
  may have just filled the bucket).

Stop when either constraint fails or no positive-edge candidates remain.

If any `place_order` returns **"theme cap reached"**, **stop placing further
orders this run** (do not theme-hop). Other errors on a candidate → skip it
and continue down the ranked list if capital and theme room allow.

### Step 13. Limit price

Set `limit_price` within 5% of the chosen side's current market price.
`place_order` tick-rounds and clamps to the market's valid range; do not
pre-round.

### Step 14. Audit trail

- `source_headline` = the crowd `pod_name`, suffixed with the per-trade
  math, e.g. `"Hormuz Updates | edge=8c kelly=4%"`.
- `crowd_score` = the **pre-blend** `weighted_score` from the table
  (consistent with A — it is the audit field, not the decision input).

If no candidate survives the full pipeline, **explain why and place no
order.**

---

## Worked example

**Crowd table row:**
```
Topic: "Iran Ceasefire Collapse"
  theme=iran
  crowd_direction=NO
  weighted_score=-0.80
  max_conviction=1.00
  interactions=12
  comment: "Ceasefire terms unsustainable"
```

**Matched market:**
```
question:  "Will Iran-Israel ceasefire hold through June 2026?"
yes_price: 0.65
no_price:  0.35
```

**`check_balance` →** `usdc = 47`, so `bankroll_free = 47 - 15 = 32`.

**Step 8 — Bayesian estimate:**
- `p_crowd  = 0.5 + 0.5 * (-0.80) = 0.10`  (crowd implies ~10% chance YES)
- `evidence = min(1, (12/${EVIDENCE_INTERACTION_CAP}) * 1.00)  = 0.60`
- `p_blend  = 0.60 * 0.10 + 0.40 * 0.65 = 0.06 + 0.26 = 0.32`
- `edge_yes = 0.32 - 0.65 = -0.33`           (negative)
- `edge_no  = (1 - 0.32) - 0.35 = +0.33`     (positive — buy NO)

**Step 9 — filter:**
- `|edge| = 0.33` ≥ ${MIN_EDGE} ✓
- `no_price = 0.35` ∈ [0.05, 0.95] ✓
- no open position, theme `iran` has room ✓

**Step 10 — quarter-Kelly sizing (buying NO):**
- `p          = 1 - 0.32 = 0.68`
- `price      = 0.35`
- `b          = (1 - 0.35) / 0.35 ≈ 1.857`
- `kelly_full = (0.68 * 1.857 - 0.32) / 1.857 ≈ 0.508`   (50.8% — full Kelly is aggressive)
- `kelly_frac = ${KELLY_FRACTION} * 0.508 ≈ 0.127`                     (12.7% of bankroll)
- `kelly_size = 0.127 * 32 ≈ $4.06`
- `size_usdc  = clamp(4.06, min_ord, MAX_ORDER_USD) = $4.06`  (capped by env `MAX_ORDER_USD`, default $10)

**Step 11 — expected P&L:**
- `expected_pnl = (0.33 / 0.35) * 4.06 ≈ $3.82`

**Step 14 — `place_order` call:**
```
place_order(
  market_id="…",
  outcome="NO",
  size_usdc=4.06,
  limit_price=0.35,
  source_headline="Iran Ceasefire Collapse | edge=33c kelly=13%",
  crowd_score=-0.80,
)
```

Notice this trade is **smaller** than Agent A's flat $10 despite a huge
crowd conviction — because at price $0.35 the risk-of-ruin term in Kelly
caps the optimal fraction. If the same belief were attached to a
closer-to-50/50 market (say `no_price = 0.45`), Kelly would size larger.
That is exactly the behaviour a quant wants.

---

## Why a quant would sign off

| Choice | Rationale |
|---|---|
| Bayesian shrinkage by evidence | Standard practice when combining a private signal (the crowd) with a public prior (the market). Single-vote or low-conviction topics are auto-discounted instead of being thresholded out. |
| Minimum edge of 5¢ | Tied to round-trip transaction cost — typical Polymarket spread is 1–3¢ per side, so 5¢ covers entry + exit slippage plus a small cushion. Anything tighter is below the cost floor. |
| Fractional (quarter) Kelly | Full Kelly is mathematically optimal *given the probability is correct.* Since `p_blend` is estimated, fractional Kelly is the textbook adjustment for estimation error (Thorp; MacLean–Thorp–Ziemba). Quarter Kelly is the conventional starting point. |
| Skip when `kelly_full ≤ 0` | Kelly correctly returns zero or negative when expected log-utility is non-positive, even on names with positive raw edge. This is the mechanism that keeps the agent out of long-tail traps automatically. |
| Ranking by expected dollar P&L | Correct objective for a budget-constrained trader: it weights edge magnitude against achievable size, instead of conflating "high score" with "good trade". |
| Multiple positive-edge orders per run | Independent profitable bets should all be taken, subject to capital and a correlation cap. `MAX_PER_THEME=5` is the rough correlation cap here (theme members are highly correlated by construction). |
| Symmetric tail caps ($0.05 / $0.95) | The same microstructure problem that motivates `MIN_ENTRY_PRICE` applies to the other side of the book — single-tick noise on a $0.97 favourite is just as fatal as on a $0.03 long-shot. |

**What a quant would want next** (not in scope for a prompt-encoded strategy):
- Calibration: empirical mapping of `weighted_score → realised outcome`
  (isotonic regression) to replace the linear `p_crowd = 0.5 + 0.5 * score`.
  Without history, the linear map is the principled default.
- Cross-theme correlation matrix for portfolio-level Kelly, instead of the
  blunt `MAX_PER_THEME=2` proxy.
- A halflife on signal staleness once we know how fast crowd opinions drift.

These are improvements *to* C, not arguments against running it.

---

## Tools available (unchanged from Agent A)

`get_positions`, `close_position`, `get_open_markets`, `check_balance`,
`place_order`.

---

## Notes for the experiment

- DDB rows must carry `agent_variant="C"`.
- This strategy deliberately uses **floating-point arithmetic in the
  prompt**. Claude Sonnet 4 handles this level of math reliably but minor
  numeric drift is possible. If experimental noise becomes an attribution
  problem, the cleaner long-term path is a `compute_edge_and_size` tool —
  but adding it now would mean comparing implementations, not strategies.
  Keep the math prompt-side for the experiment.
- Tunable knobs (env-driven; document any change and tag the run, e.g.
  `C-aggressive`):
  - `MIN_EDGE`                  env **${MIN_EDGE}**
  - `KELLY_FRACTION`            env **${KELLY_FRACTION}**
  - `MAX_NEW_ORDERS_PER_RUN`    env **${MAX_NEW_ORDERS_PER_RUN}**
  - `EVIDENCE_INTERACTION_CAP`  env **${EVIDENCE_INTERACTION_CAP}** (the divisor in step 8b)
- This single C variant is intentional — earlier drafts considered three
  sub-variants (lower-threshold / theme-concentrator / contrarian). The
  combined Bayesian-Kelly design subsumes the better hypotheses cleanly
  (concentration on conviction emerges from Kelly sizing when independent
  edges exist; lower-threshold trading emerges from the continuous edge
  filter) without committing to the contrarian-by-default sub-variant,
  which would only win under specific (and currently unsupported)
  assumptions about crowd reflexivity.
