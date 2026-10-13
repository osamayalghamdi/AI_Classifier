"""Tests for the SMAX client against the real handover contract.

The fake server enforces cookie auth and can force one 401, so login, token
caching and refresh are exercised rather than assumed.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from integrations.smax.smax_client import SmaxClient, build_filter


def _ticket(tid: str, updated_ms: int, attachments=None) -> dict:
    props = {
        "Id": tid, "DisplayLabel": f"t {tid}", "Description": "d",
        "Status": "active", "EmsCreationTime": 1735689600000,
        "LastUpdateTime": updated_ms,
    }
    if attachments is not None:
        props["IncidentAttachments"] = attachments
    return props


def _ms(y, m, d, hh=0, mm=0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp() * 1000)


class TestAuth:
    def test_logs_in_once_and_reuses_the_token(self, fake_smax, smax_client):
        list(smax_client.list_changed(0))
        list(smax_client.list_changed(0))
        assert len(fake_smax.auth_calls) == 1                       # cached, not re-sent
        assert fake_smax.auth_calls[0] == {"login": "integration-user",
                                          "password": "integration-pass"}

    def test_refreshes_once_on_401(self, fake_smax, smax_client):
        smax_client.authenticate()
        fake_smax.token_expired = True                              # next request 401s once
        list(smax_client.list_changed(0))                           # must not raise
        assert len(fake_smax.auth_calls) == 2                       # exactly one forced refresh

    def test_missing_credentials_fail_before_any_request(self, fake_smax):
        from integrations.smax.config import NotConfiguredError
        from integrations.smax.smax_client import SmaxClient
        client = SmaxClient(base_url=f"http://127.0.0.1:{fake_smax.server_address[1]}",
                            tenant="t1", username="", password="")
        with pytest.raises(NotConfiguredError):
            list(client.list_changed(0))
        assert fake_smax.auth_calls == []


class TestSearch:
    def test_paginates_until_a_short_page(self, fake_smax, smax_client):
        size = smax_client.page_size
        fake_smax.tickets = [_ticket(f"T-{i}", 1735689600000 + i) for i in range(size + 3)]
        assert len(list(smax_client.list_changed(0))) == size + 3
        assert len(fake_smax.filters) == 2                          # full page, then a short one

    def test_filter_uses_epoch_ms_in_the_configured_offset(self):
        expr = build_filter(start="2026-10-01", end="2026-10-07", entity_ids="200001",
                            utc_offset_hours=3)
        assert "EmsCreationTime >=" in expr and "EmsCreationTime <=" in expr
        assert "Entity_c = '200001'" in expr
        # 2026-10-01T00:00+03:00 == 2026-09-30T21:00Z
        assert str(_ms(2026, 9, 30, 21)) in expr
        # the last boundary covers the whole final day
        assert str(_ms(2026, 10, 7, 21) - 1) in expr

    def test_several_ids_become_an_in_expression(self):
        expr = build_filter(incident_ids=["100001", "100002"])
        assert "Id in ('100001','100002')" in expr

    def test_requires_a_criterion(self):
        with pytest.raises(ValueError):
            build_filter()

    def test_rejects_a_bad_identifier(self):
        with pytest.raises(ValueError):
            build_filter(incident_ids="bad id!")


class TestLayout:
    """The layout is how a tenant's field names get verified — a field that is
    never requested never comes back, so the entity field is always asked for."""

    def test_the_entity_field_is_requested_at_its_default_name_too(
            self, fake_smax, smax_client):
        list(smax_client.list_changed(0))
        assert "Entity_c" in fake_smax.layouts[-1].split(",")

    def test_a_custom_entity_field_replaces_the_default_one(self, fake_smax):
        client = SmaxClient(base_url=f"http://127.0.0.1:{fake_smax.server_address[1]}",
                            tenant="t1", username="u", password="p",
                            entity_field="Queue_c")
        list(client.list_changed(0))
        fields = fake_smax.layouts[-1].split(",")
        assert "Queue_c" in fields and "Entity_c" not in fields

    def test_an_explicit_layout_still_wins(self, fake_smax, smax_client):
        list(smax_client.search("(Id = 'T-1')", layout="Id,Status"))
        assert fake_smax.layouts[-1] == "Id,Status"


class TestAttachments:
    def test_hidden_files_are_skipped_and_visible_ones_download(self, fake_smax, smax_client):
        fake_smax.files["f1"] = b"hello"
        fake_smax.tickets = [_ticket("T-1", 1735689600000, attachments=[
            {"properties": {"id": "f1", "file_name": "a.txt",
                            "mime_type": "text/plain", "size": 5}},
            {"properties": {"id": "f2", "file_name": "hidden.txt", "IsHidden": "true"}},
        ])]
        entity = next(iter(smax_client.list_changed(0)))
        metas = list(smax_client.iter_attachments(entity))
        assert [m["file_id"] for m in metas] == ["f1"]              # hidden one dropped
        assert metas[0]["mime_type"] == "text/plain"
        assert smax_client.download_file("f1") == b"hello"

    def test_oversized_download_is_refused(self, fake_smax, smax_client):
        fake_smax.files["big"] = b"x" * 100
        with pytest.raises(ValueError):
            smax_client.download_file("big", max_bytes=10)

    def test_attachment_metadata_may_arrive_as_a_json_string(self, fake_smax, smax_client):
        fake_smax.files["f9"] = b"data"
        fake_smax.tickets = [_ticket("T-2", 1735689600000,
                                     attachments='[{"id": "f9", "file_name": "s.pdf"}]')]
        entity = next(iter(smax_client.list_changed(0)))
        assert [m["file_name"] for m in smax_client.iter_attachments(entity)] == ["s.pdf"]


class TestWriteBack:
    def test_write_back_refuses_rather_than_inventing_an_endpoint(self, smax_client):
        with pytest.raises(NotImplementedError):
            smax_client.write_suggestion("T-1", {"classification": {}})
