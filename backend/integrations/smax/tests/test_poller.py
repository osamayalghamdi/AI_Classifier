"""Tests for the SMAX change poller (poller.py).

Fake SMAX + fake classifier servers; the stamp file lives in tmp_path so
the repo's real .last_sync is never touched.
"""

from __future__ import annotations

from pathlib import Path

from integrations.smax import poller
from integrations.smax.smax_models import from_smax


def _ms(iso: str) -> int:
    """SMAX speaks epoch milliseconds; tests keep writing ISO for readability."""
    from datetime import datetime
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def _ticket(tid: str, title: str, updated: str) -> dict:
    """One SMAX `properties` dict, using the handover's real field names."""
    return {
        "Id": tid,
        "DisplayLabel": title,
        "Description": f"desc {tid}",
        "Status": "active",
        "EmsCreationTime": _ms("2025-01-01T00:00:00+00:00"),
        "LastUpdateTime": _ms(updated),
    }


class TestRunOnce:
    def test_submits_and_advances_stamp(self, fake_smax, smax_client,
                                        classifier_client, fake_classifier,
                                        connector_settings):
        fake_smax.tickets = [
            _ticket("T-1", "First", "2025-01-01T10:00:00+00:00"),
            _ticket("T-2", "Second", "2025-01-02T09:00:00+00:00"),
        ]
        stats = poller.run_once(smax_client, classifier_client, connector_settings)
        assert stats["listed"] == 2
        assert stats["submitted"] == 2
        # The filter is `>= since`, so the stamp advances one ms PAST the newest
        # ticket — otherwise that ticket would be re-listed on every tick.
        assert stats["advanced"] == _ms("2025-01-02T09:00:00+00:00") + 1
        assert Path(connector_settings.smax_sync_stamp_path).read_text() == \
            str(_ms("2025-01-02T09:00:00+00:00") + 1)
        # Both tickets reached the classifier API.
        submitted_refs = {r[3]["source_reference"] for r in fake_classifier.requests
                          if r[0] == "POST" and r[1] == "/api/v1/incidents"}
        assert submitted_refs == {"T-1", "T-2"}

    def test_does_not_resend_seen_tickets(self, fake_smax, smax_client,
                                          classifier_client, fake_classifier,
                                          connector_settings):
        fake_smax.tickets = [
            _ticket("T-1", "First", "2025-01-01T10:00:00+00:00"),
            _ticket("T-2", "Second", "2025-01-02T09:00:00+00:00"),
        ]
        poller.run_once(smax_client, classifier_client, connector_settings)
        first_submits = len([r for r in fake_classifier.requests
                             if r[0] == "POST" and r[1] == "/api/v1/incidents"])

        # Second tick: the fake SMAX only returns tickets updated after the
        # advanced stamp → nothing to submit.
        stats2 = poller.run_once(smax_client, classifier_client, connector_settings)
        assert stats2["listed"] == 0
        assert stats2["submitted"] == 0
        second_submits = len([r for r in fake_classifier.requests
                              if r[0] == "POST" and r[1] == "/api/v1/incidents"])
        assert second_submits == first_submits == 2

    def test_only_newer_tickets_since_default_epoch(self, fake_smax, smax_client,
                                                    classifier_client, connector_settings):
        # No stamp exists → defaults to epoch; tickets from 2025 are all newer.
        fake_smax.tickets = [_ticket("T-1", "First", "2025-01-01T10:00:00+00:00")]
        stats = poller.run_once(smax_client, classifier_client, connector_settings)
        assert stats["listed"] == 1
        assert stats["submitted"] == 1

    def test_outbox_receives_submitted_refs(self, fake_smax, smax_client,
                                            classifier_client, connector_settings):
        import queue
        fake_smax.tickets = [_ticket("T-1", "First", "2025-01-01T10:00:00+00:00")]
        outbox: queue.Queue = queue.Queue()
        poller.run_once(smax_client, classifier_client, connector_settings, outbox=outbox)
        assert outbox.get_nowait() == "T-1"
        assert outbox.empty()

    def test_payload_without_reference_skipped(self, fake_smax, smax_client,
                                               classifier_client, connector_settings):
        fake_smax.tickets = [{"DisplayLabel": "no id",
                              "LastUpdateTime": _ms("2025-01-01T10:00:00+00:00")}]
        stats = poller.run_once(smax_client, classifier_client, connector_settings)
        assert stats["listed"] == 1
        assert stats["submitted"] == 0


class TestStamp:
    def test_read_defaults_to_epoch(self, tmp_path):
        assert poller.read_stamp(tmp_path / "missing") == poller.DEFAULT_SINCE_MS

    def test_roundtrip(self, tmp_path):
        path = tmp_path / ".last_sync"
        poller.write_stamp(path, _ms("2025-01-02T09:00:00+00:00"))
        assert poller.read_stamp(path) == _ms("2025-01-02T09:00:00+00:00")

    def test_legacy_iso_stamp_is_converted(self, tmp_path):
        """An older connector left an ISO stamp; upgrading must not re-list history."""
        path = tmp_path / ".last_sync"
        path.write_text("2025-01-02T09:00:00+00:00")
        assert poller.read_stamp(path) == _ms("2025-01-02T09:00:00+00:00")

    def test_from_smax_roundtrip_shape(self):
        """from_smax output feeds submit — the connector's core mapping."""
        inc = from_smax(_ticket("T-9", "Nine", "2025-01-03T00:00:00+00:00"))
        assert inc.source_reference == "T-9"
        assert inc.title == "Nine"
