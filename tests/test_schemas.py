"""Tests for shared taxonomy constants."""

from ai_classification.schemas import SEVERITY_RANK


class TestSeverityRank:
    def test_critical_beats_major(self):
        assert SEVERITY_RANK["Critical"] > SEVERITY_RANK["Major"]

    def test_major_beats_minor(self):
        assert SEVERITY_RANK["Major"] > SEVERITY_RANK["Minor"]

    def test_minor_beats_cosmetic(self):
        assert SEVERITY_RANK["Minor"] > SEVERITY_RANK["Cosmetic"]

    def test_max_picks_highest(self):
        severities = ["Minor", "Critical", "Cosmetic", "Major"]
        worst = max(severities, key=lambda s: SEVERITY_RANK.get(s, 0))
        assert worst == "Critical"
