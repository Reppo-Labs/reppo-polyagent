#!/usr/bin/env python3
"""Generate branded per-agent dashboards from dashboard.html."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "dashboard.html").read_text(encoding="utf-8")
OUT_DIR = ROOT / "ABC_TEST" / "dashboards"
OUT_DIR.mkdir(parents=True, exist_ok=True)

AGENTS = [
    {
        "file": "agent-a.html",
        "title": "Agent A — Crowd data + Strategy 1",
        "h1": 'Agent A <span>/ Crowd data + baseline strategy</span>',
        "wallet": "0x4aA88C56208864fd53035B16B7EeE6E887d5c63F",
        "intro": """
<section class="intro" aria-label="Agent A">
  <div class="intro-eyebrow"><b>ABC experiment</b> · Variant A</div>
  <p class="intro-lede">
    <strong>Reppo crowd CSV</strong> + production-style <strong>threshold Phase 2</strong>
    (one trade/run, ranked by |score| × conviction). This is the experiment <em>control</em> —
    same data and policy family as the main geo agent.
  </p>
  <p class="intro-tagline"><a href="abc-compare-dashboard.html" style="color:var(--accent)">← Compare Agent A vs B</a></p>
</section>""",
    },
    {
        "file": "agent-b.html",
        "title": "Agent B — No data + Strategy 1",
        "h1": 'Agent B <span>/ LLM-only entries</span>',
        "wallet": "0xb14Cf74847ffA6bC9EbE4030cb73C40eEc699112",
        "intro": """
<section class="intro" aria-label="Agent B">
  <div class="intro-eyebrow"><b>ABC experiment</b> · Variant B</div>
  <p class="intro-lede">
    <strong>No crowd signal block</strong> — entries from LLM reasoning over
    <code>get_open_markets()</code> only (high confidence + ≥15¢ disagreement gate).
    Tests whether curated data beats generic model + public prices.
  </p>
  <p class="intro-tagline"><a href="abc-compare-dashboard.html" style="color:var(--accent)">← Compare Agent A vs B</a></p>
</section>""",
    },
    {
        "file": "agent-c.html",
        "title": "Agent C — Crowd data + Strategy 2",
        "h1": 'Agent C <span>/ Bayesian + Kelly strategy</span>',
        "wallet": "0x2e31030b9d3365d6D28c7B2bE794e1c7b2741003",
        "intro": """
<section class="intro" aria-label="Agent C">
  <div class="intro-eyebrow"><b>ABC experiment</b> · Variant C</div>
  <p class="intro-lede">
    <strong>Same Reppo CSV as A</strong>, but Phase 2 uses <strong>Bayesian shrinkage</strong>
    + <strong>quarter-Kelly sizing</strong> (up to 3 orders/run). Tests whether
    <em>how</em> we use the same data beats A's categorical gates.
  </p>
  <p class="intro-tagline"><a href="abc-compare-dashboard.html" style="color:var(--accent)">← Compare Agent A vs B</a></p>
</section>""",
    },
]

import re

INTRO_RE = re.compile(
    r'<section class="intro"[\s\S]*?</section>',
    re.MULTILINE,
)

for cfg in AGENTS:
    html = TEMPLATE
    html = html.replace("<title>Geo Trading Agent</title>", f"<title>{cfg['title']}</title>")
    html = html.replace(
        "<h1>Geo Trading Agent <span>/ Dashboard</span></h1>",
        f"<h1>{cfg['h1']}</h1>",
    )
    html = html.replace("0x644f75f0b1a0175E09e1ef14861ceDa2DFe3252A", cfg["wallet"])
    html = INTRO_RE.sub(cfg["intro"], html, count=1)
    # Page is served at dashboard/index.html — positions.json is a sibling object.
    html = html.replace(
        'const DATA_URL = new URL("positions.json", location.href).href;',
        'const DATA_URL = new URL("positions.json", location.href).href;',
    )
    out = OUT_DIR / cfg["file"]
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out}")

# Compare dashboard: edit ABC_TEST/abc-compare-dashboard.html only (not copied here).
# upload_abc_dashboards.sh reads that file and uploads to each bucket.
