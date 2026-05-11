import unittest

from agent import signals
from agent.signals import preprocess_signals


def _csv(*rows: str) -> str:
    # Matches the actual feedback.csv column names from the backend export
    header = "name,votes,voting_power,up_vote,feedback,status"
    return "\n".join([header] + list(rows))


def _row(name, votes, vp, up=True, feedback="", status="ACTIVE"):
    return f"{name},{votes},{vp},{up},{feedback},{status}"


class TestPreprocessSignals(unittest.TestCase):
    """
    These tests assert the *directional* contract of preprocess_signals
    (sign + magnitude of weighted_score for unanimous / mixed crowds).

    The runtime formula is `direction * (1 - exp(-N / HALFLIFE))`, which
    intentionally damps low-N topics. To keep these assertions decoupled
    from the smoothing constant, we pin the halflife to a near-zero value
    so `confidence ≈ 1` for any N ≥ MIN_INTERACTIONS. That re-establishes
    the un-shrunken `±1.00` / `+0.50` reference outputs.
    """

    def setUp(self):
        self._orig_halflife = signals.SIGNAL_HALFLIFE_INTERACTIONS
        signals.SIGNAL_HALFLIFE_INTERACTIONS = 1e-6

    def tearDown(self):
        signals.SIGNAL_HALFLIFE_INTERACTIONS = self._orig_halflife

    def test_weighted_score_all_up(self):
        csv = _csv(
            _row("Topic A", 1.0, 1.0, True),
            _row("Topic A", 1.0, 1.0, True),
            _row("Topic A", 1.0, 1.0, True),
        )
        out = preprocess_signals(csv)
        self.assertIn("weighted_score=+1.00", out)
        self.assertIn("crowd_direction=YES", out)

    def test_weighted_score_all_down(self):
        csv = _csv(
            _row("Topic B", 1.0, 1.0, False),
            _row("Topic B", 1.0, 1.0, False),
            _row("Topic B", 1.0, 1.0, False),
        )
        out = preprocess_signals(csv)
        self.assertIn("weighted_score=-1.00", out)
        self.assertIn("crowd_direction=NO", out)

    def test_weighted_score_mixed(self):
        # 3 up (vp=1 each) vs 1 down (vp=1): score = (3-1)/(3+1) = 0.50
        csv = _csv(
            _row("Topic C", 1.0, 1.0, True),
            _row("Topic C", 1.0, 1.0, True),
            _row("Topic C", 1.0, 1.0, True),
            _row("Topic C", 1.0, 1.0, False),
        )
        out = preprocess_signals(csv)
        self.assertIn("weighted_score=+0.50", out)

    def test_max_conviction(self):
        # voter 1: votes/voting_power = 0.5; voter 2: 1.0 → max = 1.0
        csv = _csv(
            _row("Topic D", 0.5, 1.0, True),
            _row("Topic D", 1.0, 1.0, True),
            _row("Topic D", 1.0, 1.0, True),
        )
        out = preprocess_signals(csv)
        self.assertIn("max_conviction=1.00", out)

    def test_below_min_interactions_filtered(self):
        csv = _csv(
            _row("Topic E", 1.0, 1.0, True),
            _row("Topic E", 1.0, 1.0, True),
        )
        out = preprocess_signals(csv)
        self.assertNotIn("Topic E", out)

    def test_exactly_min_interactions_included(self):
        csv = _csv(
            _row("Topic F", 1.0, 1.0, True),
            _row("Topic F", 1.0, 1.0, True),
            _row("Topic F", 1.0, 1.0, True),
        )
        out = preprocess_signals(csv)
        self.assertIn("Topic F", out)

    def test_zero_total_vp_skipped(self):
        # votes=0 → no voting power committed → total_vp=0 → skip
        csv = _csv(
            _row("Topic G", 0, 1.0, True),
            _row("Topic G", 0, 1.0, True),
            _row("Topic G", 0, 1.0, True),
        )
        out = preprocess_signals(csv)
        self.assertNotIn("Topic G", out)

    def test_draft_rows_skipped(self):
        csv = _csv(
            _row("Topic H", 1.0, 1.0, True, status="DRAFT"),
            _row("Topic H", 1.0, 1.0, True, status="DRAFT"),
            _row("Topic H", 1.0, 1.0, True, status="DRAFT"),
        )
        out = preprocess_signals(csv)
        self.assertNotIn("Topic H", out)

    def test_comment_extracted(self):
        csv = _csv(
            _row("Topic I", 1.0, 1.0, True, feedback="interesting geopolitical shift"),
            _row("Topic I", 1.0, 1.0, True),
            _row("Topic I", 1.0, 1.0, False),
        )
        out = preprocess_signals(csv)
        self.assertIn("interesting geopolitical shift", out)

    def test_empty_feedback_no_comment_line(self):
        csv = _csv(
            _row("Topic J", 1.0, 1.0, True),
            _row("Topic J", 1.0, 1.0, True),
            _row("Topic J", 1.0, 1.0, True),
        )
        out = preprocess_signals(csv)
        self.assertNotIn("comment:", out)

    def test_multiple_topics_independent(self):
        csv = _csv(
            _row("Alpha", 1.0, 1.0, True),
            _row("Alpha", 1.0, 1.0, True),
            _row("Alpha", 1.0, 1.0, True),
            _row("Beta",  1.0, 1.0, False),
            _row("Beta",  1.0, 1.0, False),
            _row("Beta",  1.0, 1.0, False),
        )
        out = preprocess_signals(csv)
        self.assertIn("Alpha", out)
        self.assertIn("Beta", out)

    def test_interactions_count(self):
        csv = _csv(
            _row("Topic K", 1.0, 1.0, True),
            _row("Topic K", 0.5, 1.0, True),
            _row("Topic K", 1.0, 1.0, False),
            _row("Topic K", 0.8, 1.0, True),
        )
        out = preprocess_signals(csv)
        self.assertIn("interactions=4", out)


if __name__ == "__main__":
    unittest.main()
