"""Service-string repair + system-preserving fallback (classifier v3.3).

Prod's classification_log held 41 of 1505 stage-2/3 responses (2.7%) whose
``service`` was a near-miss of the exact option — the model abbreviates the
system suffix ("licensing - Nusuk Hajj" for "licensing - Nusuk Masar Haj",
"contracts - Nusuk Hajj Services", even the concatenation
"licensing - Nusuk Hajj services - Nusuk Masar Haj"). The strict validator
rejected the string, the stage retried, and on a second miss the ENTIRE
classification was discarded: the ticket landed on Other / "General /
Unspecified" with classification_status="failed" — a wrong classification
caused by a hyphen and a missing word.

These tests pin the repair: near-misses snap onto the real option, an
invented service still fails (no fake picks), and a failure AFTER the system
is resolved keeps that system instead of collapsing the ticket to "Other".
"""

import json

import pytest

from ai_classification.domain.taxonomy import AffectedSystem
from ai_classification.services.classify.cascade import _cascade_fallback
from ai_classification.services.classify.parsing import (
    _parse_stage_service,
    generic_service,
    snap_service,
)

# Every string below was observed in the live classification_log.
PROD_NEAR_MISSES = [
    ("Nusuk Masar Haj", "licensing - Nusuk Hajj services - Nusuk Masar Haj", "licensing - Nusuk Masar Haj"),
    ("Nusuk Masar Haj", "licensing - Nusuk Hajj services", "licensing - Nusuk Masar Haj"),
    ("Nusuk Masar Haj", "licensing - Nusuk Hajj", "licensing - Nusuk Masar Haj"),
    ("Nusuk Masar Haj", "contracts - Nusuk Hajj services - Nusuk Masar Haj", "contracts - Nusuk Masar Haj"),
    ("Nusuk Masar Haj", "contracts - Nusuk Hajj Services", "contracts - Nusuk Masar Haj"),
    ("Nusuk Masar Haj", "contracts - Nusuk Hajj", "contracts - Nusuk Masar Haj"),
    ("Nusuk Masar Haj", "contracts", "contracts - Nusuk Masar Haj"),
    ("Nusuk Masar Haj", "Between cities - Nusuk Haj", "Between cities - Nusuk Masar Haj"),
    ("Nusuk Masar Haj", "System/Application - Nusuk Hajj Services", "System/Application - Nusuk Masar Haj"),
    ("Nusuk Masar Haj", "Pre Arrival - Nusuk Hajj", "Pre Arrival - Nusuk Masar Haj"),
    ("Nusuk Masar Haj", "Istiqbal - Nusuk Hajj Services", "Istiqbal - Nusuk Masar Haj"),
    ("Nusuk Masar Haj", "Public Services - Nusuk Hajj", "Public Services - Nusuk Masar Haj"),
    ("Nusuk Masar Haj", "Financial Transactions - Nusuk Hajj", "Financial Transactions - Nusuk Masar Haj"),
    ("Nusuk Masar Umrah", "System/Application - Nusuk Umrah", "System/Application - Nusuk Masar Umrah"),
]


class TestSnapService:
    def test_every_prod_near_miss_snaps_to_the_real_option(self):
        for system, written, expected in PROD_NEAR_MISSES:
            assert snap_service(system, written) == expected, (system, written)

    def test_exact_values_and_dot_paths_pass_through(self):
        assert snap_service("Nusuk Masar Haj", "licensing - Nusuk Masar Haj") == "licensing - Nusuk Masar Haj"
        assert snap_service("CRM", "System/Application - CRM.Service Unavailability") \
            == "System/Application - CRM"

    def test_invented_service_is_not_invented_back(self):
        assert snap_service("Nusuk Masar Haj", "totally made up surface") is None
        assert snap_service("Nusuk Masar Haj", "") is None

    def test_snap_stays_inside_the_system(self):
        """A CRM service string is never snapped to a Haj option."""
        assert snap_service("Nusuk Masar Haj", "System/Application - CRM") is None


class TestGenericService:
    def test_prefers_the_application_catch_all(self):
        assert generic_service("Nusuk Masar Haj") == "System/Application - Nusuk Masar Haj"
        assert generic_service("CRM") == "System/Application - CRM"

    def test_unknown_system_degrades_to_general(self):
        assert generic_service("Nonexistent") == "General / Unspecified"


class TestStageServiceLenientParse:
    def _raw(self, service: str) -> str:
        return json.dumps({
            "affected_system": "Nusuk Masar Haj", "service": service,
            "incident_type": "Degradation", "severity": "Major", "urgency": "High",
            "category": "Configuration", "confidence": "high",
            "reasoning": "rejects a valid CR number", "canonical_statement": "c",
            "signature": "s",
        })

    def test_near_miss_service_is_snapped_and_the_classification_survives(self):
        r = _parse_stage_service(self._raw("licensing - Nusuk Hajj Services"), "Nusuk Masar Haj")
        assert r is not None
        assert r.service == "licensing - Nusuk Masar Haj"
        assert r.affected_system == AffectedSystem.nusuk_masar_haj
        assert r.severity.value == "Major"           # the rest of the answer is kept

    def test_unsnappable_service_keeps_the_system_on_its_generic_surface(self):
        r = _parse_stage_service(self._raw("some brand new surface"), "Nusuk Masar Haj")
        assert r is not None
        assert r.affected_system == AffectedSystem.nusuk_masar_haj
        assert r.service == "System/Application - Nusuk Masar Haj"
        assert r.confidence == "low"                 # honest: the surface is unknown

    def test_stage_two_cannot_move_the_ticket_to_another_system(self):
        """Stage 2's system is fixed; a drifting answer must not change it."""
        raw = self._raw("licensing - Nusuk Masar Haj")
        r = _parse_stage_service(raw, "Nusuk Masar Haj")
        assert r.affected_system == AffectedSystem.nusuk_masar_haj

    def test_unusable_json_still_returns_none(self):
        assert _parse_stage_service("not json at all", "Nusuk Masar Haj") is None


class TestFallbackKeepsTheResolvedSystem:
    def test_without_a_system_the_legacy_fallback_is_unchanged(self):
        r = _cascade_fallback("t", "boom")
        assert r.affected_system == AffectedSystem.other
        assert r.service == "General / Unspecified"
        assert r.classification_status == "failed" and r.needs_review is True
        assert r.reasoning.startswith("Classification failed after 2 attempts")

    def test_with_a_resolved_system_the_ticket_stays_on_that_system(self):
        r = _cascade_fallback("t", "boom", system=AffectedSystem.nusuk_masar_haj)
        assert r.affected_system == AffectedSystem.nusuk_masar_haj
        assert r.service == "System/Application - Nusuk Masar Haj"
        # still an honest failure: the E5 marker and the review flag survive
        assert r.classification_status == "failed" and r.needs_review is True
        assert r.reasoning.startswith("Classification failed after 2 attempts")


class TestRobustness:
    """Production safety: the repair must never be the thing that crashes."""

    def test_snap_service_tolerates_junk_input(self):
        for bad in (None, "", "   ", 42, [], {}, "\n\t"):
            assert snap_service("Nusuk Masar Haj", bad) is None

    def test_snap_service_tolerates_an_unknown_system(self):
        assert snap_service("Nonexistent System", "licensing - Nusuk Masar Haj") is None
        assert generic_service("Nonexistent System") == "General / Unspecified"

    def test_lenient_stage_parse_tolerates_junk(self):
        for bad in ("", "not json", "[]", '"a string"', "{}"):
            r = _parse_stage_service(bad, "Nusuk Masar Haj")
            assert r is None or r.service, f"unexpected result for {bad!r}"

    def test_lenient_stage_parse_survives_a_missing_service_key(self):
        """No service at all: the SYSTEM is kept, the surface falls back to the
        system's generic one instead of the whole ticket becoming Other."""
        raw = json.dumps({"affected_system": "Nusuk Masar Haj", "incident_type": "Degradation",
                          "severity": "Major", "urgency": "High", "category": "Software",
                          "confidence": "high", "reasoning": "r", "canonical_statement": "c",
                          "signature": "s"})
        r = _parse_stage_service(raw, "Nusuk Masar Haj")
        assert r is not None and r.service == "System/Application - Nusuk Masar Haj"

    def test_lenient_stage_parse_returns_none_when_required_fields_are_missing(self):
        """A response missing OTHER required fields is not usable — the caller
        then takes the (system-preserving) failure path."""
        assert _parse_stage_service(json.dumps({"affected_system": "Nusuk Masar Haj",
                                                "severity": "Major"}), "Nusuk Masar Haj") is None

    def test_snapped_service_is_always_valid_for_the_system(self):
        """Whatever the model writes, the stored value belongs to the system."""
        from ai_classification.domain.taxonomy import SERVICES_BY_SYSTEM
        services = SERVICES_BY_SYSTEM["Nusuk Masar Haj"]
        for written in [w for _, w, _ in PROD_NEAR_MISSES] + ["nonsense", "contracts - CRM"]:
            snapped = snap_service("Nusuk Masar Haj", written)
            if snapped is not None:
                assert snapped in services, (written, snapped)


class TestSystemPinFromTheTicketingSystem:
    """The ticketing system knows its own queue — the classifier must not guess
    over it. `affected_system` in the payload is validated and PINNED, on BOTH
    classifier paths (cascade and single-shot), and an unknown value falls back
    instead of failing the ingest."""

    FAKE = json.dumps({
        "affected_system": "Nusuk Masar Haj",
        "service": "pilgrim groups and issue permit - Nusuk Masar Haj.Issue Permits",
        "incident_type": "Unavailability", "severity": "Major", "urgency": "High",
        "category": "Software", "confidence": "high", "reasoning": "t",
        "canonical_statement": "x", "signature": "y"})

    def _wire(self, monkeypatch, cascade: bool):
        from tests.services.classify.test_cascade import _settings_with, make_fake_completion
        import ai_classification.services.classify.llm as mod_llm
        import ai_classification.services.classify.classifier as classifier_mod
        monkeypatch.setattr(classifier_mod, "settings", _settings_with(cascade))
        monkeypatch.setattr(mod_llm, "completion", lambda **kw: make_fake_completion(self.FAKE))
        return classifier_mod

    @pytest.mark.parametrize("cascade", [True, False])
    def test_pin_wins_on_both_paths(self, monkeypatch, cascade):
        cm = self._wire(monkeypatch, cascade)
        r = cm.classify("Rawdah permit date error", "Error selecting a date",
                        affected_system="CRM")
        assert r.affected_system.value == "CRM"
        # …and the service is re-homed so the row is never "CRM + a Hajj service"
        assert "CRM" in r.service or r.service in ("General / Unspecified",)

    @pytest.mark.parametrize("cascade", [True, False])
    def test_unknown_pin_falls_back_instead_of_raising(self, monkeypatch, cascade):
        cm = self._wire(monkeypatch, cascade)
        r = cm.classify("Rawdah permit date error", "Error selecting a date",
                        affected_system="NotARealSystem")
        assert r.affected_system.value in ("Nusuk Masar Haj", "Nusuk Masar Umrah", "CRM", "OldSM", "Other")

    def test_no_pin_keeps_the_llm_answer(self, monkeypatch):
        cm = self._wire(monkeypatch, False)
        r = cm.classify("Rawdah permit date error", "Error selecting a date")
        assert r.affected_system.value == "Nusuk Masar Haj"
