"""Heal sweep tests — periodic re-classification of fallback-classified
incidents (LLM outage self-heal). Only fallback-marked rows are touched;
good classifications are never re-run; LLM down fails open."""

from __future__ import annotations

import json
import uuid

import psycopg2
import pytest

import ai_classification.services.classify.classifier as classifier_mod
import ai_classification.services.classify.llm as mod_llm
from ai_classification.shared.config import settings
from ai_classification.services.jobs.heal import reclassify_fallback_incidents
from ai_classification.shared.store import store

from tests.services.classify.test_cascade import _settings_with, make_fake_completion

FALLBACK = json.dumps({
    "affected_system": "Other",
    "service": "General / Unspecified",
    "incident_type": "Degradation",
    "severity": "Minor",
    "urgency": "Low",
    "category": "Other",
    "confidence": "low",
    "reasoning": "Classification failed after 2 attempts. Last error: Connection error.",
    "canonical_statement": "Incident reported: test",
    "signature": "Generic/Unknown",
})

GOOD = json.dumps({
    "affected_system": "Nusuk Masar Haj",
    "service": "pilgrim groups and issue permit - Nusuk Masar Haj.Issue Permits",
    "incident_type": "Unavailability",
    "severity": "Major",
    "urgency": "High",
    "category": "Software",
    "confidence": "high",
    "reasoning": "ok",
    "canonical_statement": "Permit issuance fails when selecting a date for the group.",
    "signature": "permit issuance fails on date selection",
    
})


@pytest.fixture(scope="module", autouse=True)
def _store_ready():
    store.setup()
    yield
    store.setup()  # reopen the pool in case a lifespan closed it


@pytest.fixture(autouse=True)
def _clean_state():
    yield
    conn = psycopg2.connect(
        host=settings.pg_host, port=settings.pg_port,
        user=settings.pg_user, password=settings.pg_password,
        dbname=settings.pg_database,
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("TRUNCATE incidents RESTART IDENTITY CASCADE")
    conn.close()


@pytest.fixture(autouse=True)
def _fake_llm(monkeypatch):
    """Cascade off (one LLM call per classify); body switchable per test."""
    monkeypatch.setattr(classifier_mod, "settings", _settings_with(False))
    state = {"body": FALLBACK, "calls": 0, "raise_exc": False}

    def fake_completion(**kwargs):
        state["calls"] += 1
        if state["raise_exc"]:
            raise RuntimeError("LLM API call failed: connection error")
        return make_fake_completion(state["body"])

    monkeypatch.setattr(mod_llm, "completion", fake_completion)
    return state


def _seed(title: str = "Rawdah permit date error",
          description: str = "Error when selecting a date for the group") -> str:
    resp = classifier_mod.classify_and_store(
        title, description, source_ticket_id=f"heal-{uuid.uuid4().hex[:8]}"
    )
    assert resp.incident_id is not None
    return resp.incident_id


def _row(iid: str) -> dict:
    row = store.get_incident(iid)
    assert row is not None
    return row


# ── The sweep heals fallback-classified incidents ────────────────────

def test_heal_reclassifies_fallback_incident(_fake_llm):
    iid = _seed()
    assert _row(iid)["classification_dict"]["confidence"] == "low"

    _fake_llm["body"] = GOOD
    result = reclassify_fallback_incidents()
    assert result == {"healed": 1, "still_fallback": 0}

    row = store.get_incident(iid)
    cls = row["classification_dict"]
    assert cls["confidence"] == "high"
    assert cls["affected_system"] == "Nusuk Masar Haj"
    assert "Issue Permits" in cls["service"]  # offering picked correctly


# ── Good classifications are NEVER re-run ────────────────────────────

def test_heal_leaves_good_classifications_alone(_fake_llm):
    _fake_llm["body"] = GOOD
    iid = _seed()
    _fake_llm["calls"] = 0

    result = reclassify_fallback_incidents()
    assert result == {"healed": 0, "still_fallback": 0}
    assert _fake_llm["calls"] == 0  # no LLM call at all — nothing to heal
    assert _row(iid)["classification_dict"]["confidence"] == "high"


# ── LLM down → fail open, incident untouched, no crash ───────────────

def test_heal_llm_down_fails_open(_fake_llm):
    iid = _seed()  # fallback stored
    _fake_llm["raise_exc"] = True

    result = reclassify_fallback_incidents()  # must not raise
    assert result["healed"] == 0
    assert _row(iid)["classification_dict"]["confidence"] == "low"


# ── Re-classification that still falls back is NOT persisted ─────────

def test_heal_keeps_fallback_if_reclassify_again_falls_back(_fake_llm):
    iid = _seed()
    # LLM "recovers" but still returns the fallback shape
    _fake_llm["body"] = FALLBACK.replace("Connection error", "Timeout")

    result = reclassify_fallback_incidents()
    assert result == {"healed": 0, "still_fallback": 1}
    assert _row(iid)["classification_dict"]["confidence"] == "low"


# ── Resolved incidents are not healed ────────────────────────────────

def test_heal_skips_resolved_incidents(_fake_llm):
    iid = _seed()
    store.resolve_incident(iid)
    _fake_llm["body"] = GOOD

    result = reclassify_fallback_incidents()
    assert result == {"healed": 0, "still_fallback": 0}
    assert _row(iid)["classification_dict"]["confidence"] == "low"


# ── Store-level queries ──────────────────────────────────────────────

def test_find_fallback_marks_only_fallback_rows(_fake_llm):
    _fake_llm["body"] = GOOD
    good_id = _seed("Good ticket", "works fine")
    _fake_llm["body"] = FALLBACK
    bad_id = _seed("Broken ticket", "fails")

    rows = store.find_fallback_incidents(10)
    ids = {r["id"] for r in rows}
    assert bad_id in ids
    assert good_id not in ids
