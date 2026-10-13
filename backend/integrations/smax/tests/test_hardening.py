"""First-live-run hardening.

Two promises are tested here:
  * one bad ticket never loses the batch (a broken attachment list costs nothing),
  * a submission cap defers work instead of dropping it (nothing is re-sent and
    nothing is skipped), plus the `--probe` mode proves it submits nothing.
"""

from __future__ import annotations

from types import SimpleNamespace

from integrations.smax import main as smax_main
from integrations.smax import poller


def _props(tid: str, updated_ms: int, attachments=None) -> dict:
    """One SMAX `properties` dict."""
    props = {
        "Id": tid, "DisplayLabel": f"t {tid}", "Description": "d", "Status": "active",
        "EmsCreationTime": 1735689600000, "LastUpdateTime": updated_ms,
    }
    if attachments is not None:
        props["IncidentAttachments"] = attachments
    return props


def _submitted(fake_classifier) -> list[str]:
    return [r[3]["source_reference"] for r in fake_classifier.requests
            if r[0] == "POST" and r[1] == "/api/v1/incidents"]


class TestResilience:
    def test_broken_attachment_metadata_does_not_lose_the_ticket(
            self, fake_smax, smax_client, classifier_client, fake_classifier,
            connector_settings):
        fake_smax.tickets = [
            _props("T-1", 1735689600000, attachments="not json at all"),
            _props("T-2", 1735689601000),
        ]
        stats = poller.run_once(smax_client, classifier_client, connector_settings)
        assert stats["listed"] == 2
        assert stats["submitted"] == 2                 # the broken attachment cost nothing
        assert _submitted(fake_classifier) == ["T-1", "T-2"]

    def test_incident_without_an_id_is_skipped_not_fatal(
            self, fake_smax, smax_client, classifier_client, fake_classifier,
            connector_settings):
        fake_smax.tickets = [{"DisplayLabel": "no id", "LastUpdateTime": 1735689600000},
                             _props("T-9", 1735689601000)]
        stats = poller.run_once(smax_client, classifier_client, connector_settings)
        assert stats["submitted"] == 1 and stats["skipped"] == 1
        assert _submitted(fake_classifier) == ["T-9"]

    def test_attachment_download_failure_keeps_the_ticket(
            self, fake_smax, smax_client, classifier_client, fake_classifier,
            connector_settings):
        # metadata promises a file the file service does not have (empty body)
        fake_smax.tickets = [_props("T-5", 1735689600000, attachments=[
            {"properties": {"id": "missing", "file_name": "a.pdf", "size": 999}}])]
        stats = poller.run_once(smax_client, classifier_client, connector_settings)
        assert stats["submitted"] == 1                 # sent by name only
        body = [r[3] for r in fake_classifier.requests
                if r[0] == "POST" and r[1] == "/api/v1/incidents"][0]
        # name only: no content_base64 for an empty body
        assert body["attachments"] == [{"name": "a.pdf"}]


class TestSubmissionCap:
    def test_cap_defers_the_rest_and_the_next_tick_picks_them_up(
            self, fake_smax, smax_client, classifier_client, fake_classifier,
            connector_settings):
        fake_smax.tickets = [_props("T-1", 1735689600000),
                             _props("T-2", 1735689601000),
                             _props("T-3", 1735689602000)]

        first = poller.run_once(smax_client, classifier_client, connector_settings, limit=1)
        assert first["submitted"] == 1
        assert first["limited"] is True

        second = poller.run_once(smax_client, classifier_client, connector_settings)
        assert second["submitted"] == 2
        assert second["limited"] is False

        # Each ticket reached the classifier exactly once, in order: the cap
        # deferred work, it did not drop or repeat it.
        assert _submitted(fake_classifier) == ["T-1", "T-2", "T-3"]

    def test_settings_cap_is_used_when_no_flag_is_given(
            self, fake_smax, smax_client, classifier_client, connector_settings):
        capped = connector_settings.__class__(**{**connector_settings.__dict__,
                                                 "smax_max_incidents_per_tick": 2})
        fake_smax.tickets = [_props(f"T-{i}", 1735689600000 + i) for i in range(4)]
        stats = poller.run_once(smax_client, classifier_client, capped)
        assert stats["submitted"] == 2 and stats["limited"] is True


class TestProbe:
    def test_probe_shows_the_mapping_and_submits_nothing(
            self, fake_smax, smax_client, monkeypatch, capsys):
        fake_smax.tickets = [_props("100001", 1735689600000)]
        monkeypatch.setattr(smax_main, "_build_smax_client", lambda: smax_client)
        monkeypatch.setattr(smax_main, "settings", SimpleNamespace(
            smax_base="https://smax.test.local", smax_tenant="t1",
            classifier_api_url="http://classifier.invalid",
            require_smax=lambda: None))

        code = smax_main._probe("100001", raw=False, download=False)
        out = capsys.readouterr().out

        assert code == 0
        assert "100001" in out
        assert "ingest body that WOULD be sent" in out
        assert "Nothing was submitted" in out
        # it searched by Id, and posted nothing anywhere
        assert any("Id = '100001'" in expr for expr in fake_smax.filters)
        assert fake_smax.suggestions == []
        assert fake_smax.file_requests == []          # no download unless asked

    def test_probe_reports_a_missing_incident_without_a_traceback(
            self, fake_smax, smax_client, monkeypatch, capsys):
        fake_smax.tickets = []
        monkeypatch.setattr(smax_main, "_build_smax_client", lambda: smax_client)
        monkeypatch.setattr(smax_main, "settings", SimpleNamespace(
            smax_base="https://smax.test.local", smax_tenant="t1",
            classifier_api_url="http://classifier.invalid",
            require_smax=lambda: None))

        assert smax_main._probe("NOPE", raw=False, download=False) == 1
        assert "no incident with Id" in capsys.readouterr().err

    def test_check_mode_needs_no_credentials(self, capsys):
        assert smax_main.main(["--check"]) == 0
        assert "smax_base" in capsys.readouterr().out
