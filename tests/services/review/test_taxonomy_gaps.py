"""GET /taxonomy-gaps — the review UI surfaces classifier v3 OFFERING-GAP hits.

Standalone: TestClient WITHOUT the `with` context manager (lifespan skipped,
no Postgres needed). store.list_taxonomy_gaps is monkeypatched — worker B
lands the real store implementation in a parallel worktree. Mirrors the
api() fixture pattern in tests/services/match/test_suboffering.py.
"""
import pytest
from fastapi.testclient import TestClient

import ai_classification.services.review.taxonomy_gaps_routes as tg
from ai_classification.services.ingest.routes import app

GAPS = [
    {
        "service": "System/Application - Nusuk Masar Haj",
        "suggested_offering": "Company Evaluation",
        "count": 9,
        "incident_refs": ["INC-0001", "INC-0002", "INC-0003"],
        "last_seen": "2026-08-19T10:00:00Z",
    },
    {
        "service": "Shared Services",
        "suggested_offering": "(unspecified)",
        "count": 2,
        "incident_refs": ["INC-0004", "INC-0005"],
        "last_seen": "2026-08-18T22:15:00Z",
    },
]


class _FakeStore:
    """Minimal stand-in for the real store while worker B is in flight."""

    def __init__(self, gaps=GAPS):
        self._gaps = gaps

    def list_taxonomy_gaps(self):
        return self._gaps


@pytest.fixture
def client():
    return TestClient(app)


def test_list_taxonomy_gaps_shape(client, monkeypatch):
    monkeypatch.setattr(tg, "store", _FakeStore())
    r = client.get("/taxonomy-gaps")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"gaps", "total"}
    assert body["total"] == 2
    assert len(body["gaps"]) == 2
    gap = body["gaps"][0]
    assert set(gap) == {
        "service", "suggested_offering", "count", "incident_refs", "last_seen",
    }
    assert gap["service"] == "System/Application - Nusuk Masar Haj"
    assert gap["suggested_offering"] == "Company Evaluation"
    assert gap["count"] == 9
    assert gap["incident_refs"] == ["INC-0001", "INC-0002", "INC-0003"]
    assert gap["last_seen"] == "2026-08-19T10:00:00Z"


def test_empty_gaps(client, monkeypatch):
    monkeypatch.setattr(tg, "store", _FakeStore(gaps=[]))
    r = client.get("/taxonomy-gaps")
    assert r.status_code == 200
    assert r.json() == {"gaps": [], "total": 0}


def test_missing_store_method_returns_empty(client, monkeypatch):
    # Defensive guard: while store.list_taxonomy_gaps does not exist in this
    # worktree the endpoint must return an empty body, never a 500.
    class _StoreWithoutGaps:
        pass

    monkeypatch.setattr(tg, "store", _StoreWithoutGaps())
    r = client.get("/taxonomy-gaps")
    assert r.status_code == 200
    assert r.json() == {"gaps": [], "total": 0}
