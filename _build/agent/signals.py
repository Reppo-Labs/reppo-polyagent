import csv
import io
from collections import defaultdict

MIN_INTERACTIONS = 3


def preprocess_signals(feedback_csv: str) -> str:
    """
    Aggregate feedback.csv into a crowd signal table string for the system prompt.

    feedback.csv schema (one row per vote, joined export from the backend):
      name          — pod topic headline
      up_vote       — "True" (upvote) or "False" (downvote)
      votes         — voting power this voter committed to this topic
      voting_power  — voter's total available voting power
      feedback      — optional free-text comment (may be empty)
      status        — ACTIVE | DRAFT  (DRAFT rows are skipped)

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

        weighted_score  = (t["up_vp"] - t["down_vp"]) / total_vp
        crowd_direction = "YES" if weighted_score >= 0 else "NO"
        max_conviction  = max(t["convictions"]) if t["convictions"] else 0.0

        lines.append(f'\nTopic: "{name}"')
        lines.append(
            f"  crowd_direction={crowd_direction}"
            f"  weighted_score={weighted_score:+.2f}"
            f"  max_conviction={max_conviction:.2f}"
            f"  interactions={t['interactions']}"
        )
        if t["comments"]:
            lines.append(f'  comment: "{t["comments"][0]}"')

    return "\n".join(lines) + "\n\n"
