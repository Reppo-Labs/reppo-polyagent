import csv
import io
import math
import os
import re
from collections import defaultdict

MIN_INTERACTIONS = 3

# ── Signal scoring ────────────────────────────────────────────────────────────
#
# The original score `(up_vp - down_vp) / total_vp` saturates at ±1.0 whenever
# votes are one-sided — which is the *common* case in the CSV. With 33/45
# topics scoring exactly +1.0, the agent has no way to rank between
# "max-conviction" topics and ends up loading correlated bets.
#
# New score:
#
#   direction      = (up_vp - down_vp) / total_vp        ∈ [-1, +1]
#   confidence     = 1 - exp(-interactions / HALFLIFE)   ∈ [0, ~1]
#   weighted_score = direction * confidence
#
# Direction preserves the crowd's lean. Confidence scales it by how *much*
# evidence we have, measured in interactions (not raw vote-power, so the
# distribution is independent of the absolute scale of veREPPO voting).
# With HALFLIFE=10, a topic with 3 votes scores ~0.26 × direction; a topic
# with 30 votes scores ~0.95 × direction. That gives the agent real
# discrimination between strong and weak signals while preserving the
# entry threshold the system prompt already uses.
SIGNAL_HALFLIFE_INTERACTIONS = float(
    os.environ.get("SIGNAL_HALFLIFE_INTERACTIONS", "10")
)


# ── Theme classifier ──────────────────────────────────────────────────────────
#
# Headlines in source_headline are concatenated topic names ("Hormuz Updates /
# Iran rejecting diplomacy / ..."). A pure-string match is too granular to
# detect correlated bets ("Hormuz normalization", "US-Iran peace", "Israel
# withdraws Lebanon" are all the same macro thesis: regional de-escalation).
#
# We use a small regex bucketer over the headline text. Adding a new theme is
# one line. The order matters — the first matching pattern wins, so put more
# specific themes above broader ones.
_THEME_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("iran",      re.compile(r"\b(iran|tehran|khamenei|pahlavi|hormuz|kharg|persian)\b", re.I)),
    ("israel",    re.compile(r"\b(israel|lebanon|gaza|hezbollah|hamas|netanyahu)\b", re.I)),
    ("ukraine",   re.compile(r"\b(ukraine|russia|putin|zelensky|kyiv|kremlin)\b", re.I)),
    ("taiwan",    re.compile(r"\b(taiwan|china invade|prc|tsmc|strait of taiwan)\b", re.I)),
    ("oil",       re.compile(r"\b(crude|brent|wti|oil price|barrel)\b", re.I)),
    ("starmer",   re.compile(r"\b(starmer|labour|uk pm|10 downing)\b", re.I)),
    ("trump",     re.compile(r"\b(trump|maga|white house)\b", re.I)),
    ("election",  re.compile(r"\b(election|ballot|vote tally|polls)\b", re.I)),
]


def classify_theme(text: str | None) -> str:
    """
    Bucket a headline (or signal name) into a coarse macro theme so the agent
    can enforce concentration limits. Returns 'other' when nothing matches —
    that bucket is still capped, so 'other' positions don't pile up either.
    """
    if not text:
        return "other"
    for name, pat in _THEME_PATTERNS:
        if pat.search(text):
            return name
    return "other"


def preprocess_signals(feedback_csv: str) -> str:
    """
    Aggregate feedback.csv into a crowd signal table string for the system prompt.

    feedback.csv schema (one row per vote; exports may include many extra columns):
      name          — pod topic headline (required)
      up_vote       — "True"/"False" or "true"/"false" (upvote vs downvote)
      votes         — voting power this voter committed to this topic
      voting_power  — voter's total available voting power
      feedback      — optional free-text comment (may be empty)
      status        — ACTIVE | DRAFT  (DRAFT rows are skipped)

    Extra columns from full Reppo dumps (id, pod_id, description, url, …) are
    ignored; csv.DictReader only reads the keys above.

    Pods with no rows in this file have zero interactions and are irrelevant
    to signal generation — no need to load pods.csv separately.
    """
    topics: dict[str, dict] = defaultdict(lambda: {
        "up_vp": 0.0,
        "down_vp": 0.0,
        "convictions": [],
        "interactions": 0,
        "comments": [],
    })

    for row in csv.DictReader(io.StringIO(feedback_csv)):
        if row.get("status", "").strip().upper() != "ACTIVE":
            continue

        name = row.get("name", "").strip()
        if not name:
            continue

        try:
            votes        = float(row.get("votes")        or 0)
            voting_power = float(row.get("voting_power") or 0)
        except ValueError:
            continue

        is_up    = row.get("up_vote", "").strip().lower() == "true"
        feedback = row.get("feedback", "").strip()

        t = topics[name]
        if is_up:
            t["up_vp"] += votes
        else:
            t["down_vp"] += votes

        if voting_power > 0:
            t["convictions"].append(votes / voting_power)

        t["interactions"] += 1

        if feedback:
            t["comments"].append(feedback)

    lines = [
        "CROWD SIGNALS FROM GEOPOLITICAL INTELLIGENCE PLATFORM",
        "=" * 54,
    ]

    for name, t in sorted(topics.items()):
        total_vp = t["up_vp"] + t["down_vp"]
        if total_vp == 0 or t["interactions"] < MIN_INTERACTIONS:
            continue

        # direction × confidence — see module docstring. This both removes
        # the ±1.0 saturation and makes the score independent of the
        # voting-power scale (a 1× vote and a 1000× vote both register as
        # "one vote" for confidence purposes, while the directional share
        # still reflects veREPPO weight).
        direction       = (t["up_vp"] - t["down_vp"]) / total_vp
        confidence      = 1.0 - math.exp(-t["interactions"] / SIGNAL_HALFLIFE_INTERACTIONS)
        weighted_score  = direction * confidence
        crowd_direction = "YES" if weighted_score >= 0 else "NO"
        max_conviction  = max(t["convictions"]) if t["convictions"] else 0.0
        theme           = classify_theme(name)

        lines.append(f'\nTopic: "{name}"')
        lines.append(
            f"  theme={theme}"
            f"  crowd_direction={crowd_direction}"
            f"  weighted_score={weighted_score:+.2f}"
            f"  max_conviction={max_conviction:.2f}"
            f"  interactions={t['interactions']}"
        )
        if t["comments"]:
            lines.append(f'  comment: "{t["comments"][0]}"')

    return "\n".join(lines) + "\n\n"
