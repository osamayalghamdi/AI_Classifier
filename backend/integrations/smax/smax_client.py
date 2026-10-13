"""SMAX REST surface — the ONLY place this connector talks to SMAX.

Implements the contracts in `SMAX_INTEGRATION.md` (kept beside this file):

    auth     POST {base}/auth/authentication-endpoint/authenticate/token?TENANTID={tenant}
             body {"login","password"} → plain-text JWT
             afterwards:  Cookie: SMAX_AUTH_TOKEN=<jwt>; TENANTID=<tenant>
    search   GET  {base}/rest/{tenant}/ems/Incident?layout=&filter=&size=&skip=
             → {"entities":[{"properties":{…}}], "meta":{"completion_status":…}}
    files    GET  {base}/rest/{tenant}/frs/file-list/{file_id}

Add a new SMAX call by adding one method below that uses `self.get_json(...)`
/ `self.post_text(...)`; auth, timeouts and retries come from
http_client.JsonHttpClient. Payload shaping lives in smax_models.py, the poll
loop in poller.py.

Write-back is NOT implemented: the SMAX handover documents no write endpoint,
so `write_suggestion` refuses instead of inventing one.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from .config import NotConfiguredError
from .http_client import DEFAULT_TIMEOUT_S, JsonHttpClient

_log = logging.getLogger(__name__)

AUTH_PATH = "/auth/authentication-endpoint/authenticate/token"
# The fields every search asks for (the handover's layout). Entity_c is
# overridden by settings.smax_entity_field when a tenant uses another field.
DEFAULT_LAYOUT = (
    "Id", "DisplayLabel", "Description", "Status",
    "EmsCreationTime", "LastUpdateTime", "IncidentAttachments", "NumberOfAttachments",
)
MAX_PAGE_SIZE = 250
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def identifier(value) -> str:
    """Validate an identifier exactly as the handover's example does."""
    text = str(value).strip()
    if not _IDENTIFIER_RE.fullmatch(text):
        raise ValueError("Identifiers must contain letters, digits, '_' or '-'.")
    return text


def _id_condition(field: str, values) -> str | None:
    """`Field = 'x'` for one id, `Field in ('x','y')` for several."""
    if values is None:
        return None
    if isinstance(values, (str, int)):
        values = [values]
    items = [identifier(v) for v in values]
    if not items:
        raise ValueError("An identifier list cannot be empty.")
    if len(items) == 1:
        return f"{field} = '{items[0]}'"
    return field + " in (" + ",".join(f"'{v}'" for v in items) + ")"


def _epoch_ms(boundary: datetime) -> int:
    return int(boundary.timestamp() * 1000)


def build_filter(*, start=None, end=None, entity_ids=None, incident_ids=None,
                 changed_since_ms: int | None = None, entity_field: str = "Entity_c",
                 utc_offset_hours: int = 3) -> str:
    """Build the SMAX `filter` expression.

    Dates are inclusive and interpreted in a FIXED UTC offset (the handover
    defaults to +3, Riyadh), then sent as epoch milliseconds. `changed_since_ms`
    is the update-time equivalent used by the poller — the handover documents
    creation time and asks for a corresponding update-time query, so that is
    LastUpdateTime with the same comparison.
    """
    if not _FIELD_RE.fullmatch(entity_field):
        raise ValueError("Invalid entity field name.")
    tz = timezone(timedelta(hours=utc_offset_hours))

    def as_day(value) -> date:
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        return date.fromisoformat(str(value))

    first = as_day(start) if start is not None else None
    last = as_day(end) if end is not None else None
    if first and last and first > last:
        raise ValueError("Start date must not be later than end date.")

    parts: list[str] = []
    if first:
        parts.append("EmsCreationTime >= "
                     f"{_epoch_ms(datetime.combine(first, datetime.min.time(), tzinfo=tz))}")
    if last:
        boundary = datetime.combine(last, datetime.min.time(), tzinfo=tz) \
            + timedelta(days=1, milliseconds=-1)
        parts.append(f"EmsCreationTime <= {_epoch_ms(boundary)}")
    if changed_since_ms is not None:
        parts.append(f"LastUpdateTime >= {int(changed_since_ms)}")
    for field, values in (("Id", incident_ids), (entity_field, entity_ids)):
        condition = _id_condition(field, values)
        if condition:
            parts.append(condition)
    if not parts:
        raise ValueError(
            "Provide a date boundary, an update time, an entity ID, or an incident ID.")
    return "(" + " and ".join(parts) + ")"


def _layout_for(entity_field: str) -> str:
    """The layout string: the handover's fields, with the tenant's entity field."""
    fields: list[str] = list(DEFAULT_LAYOUT)
    if entity_field != "Entity_c":
        fields.append(entity_field)
    return ",".join(fields)


def iter_attachment_metadata(properties: dict, incident_id: str):
    """Yield visible attachments from `properties.IncidentAttachments`.

    The value arrives either as serialized JSON or as a parsed object/list;
    entries may carry their fields under `properties`. Hidden attachments
    (`IsHidden`) are skipped, exactly like the handover's example.
    """
    value = properties.get("IncidentAttachments")
    if value is None or value == "":
        return
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict):
        value = value.get("complexTypeProperties", [])
    if not isinstance(value, list):
        raise ValueError("Expected a list of attachment metadata.")
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("Invalid attachment metadata entry.")
        attachment = entry.get("properties", entry)
        if not isinstance(attachment, dict):
            raise ValueError("Invalid attachment properties.")
        if str(attachment.get("IsHidden", "")).strip().lower() in ("true", "1"):
            continue
        raw_size = attachment.get("size")
        expected = int(raw_size) if raw_size not in (None, "") else None
        if expected is not None and expected < 0:
            raise ValueError("Attachment size cannot be negative.")
        yield {
            "incident_id": incident_id,
            "file_id": identifier(attachment.get("id", "")),
            "file_name": str(attachment.get("file_name") or "attachment.bin"),
            "mime_type": str(attachment.get("mime_type") or ""),
            "expected_size": expected,
        }


class SmaxClient(JsonHttpClient):
    """SMAX REST client: log in once, then cookie auth on every call."""

    label = "SMAX"
    subject = "SMAX source"
    credential_env = "SMAX_USER / SMAX_PASS"
    url_env = "SMAX_BASE"

    def __init__(self, *, base_url: str, tenant: str, username: str, password: str,
                 token_ttl: float = 1500.0, timeout: float = DEFAULT_TIMEOUT_S,
                 entity_field: str = "Entity_c", utc_offset_hours: int = 3,
                 page_size: int = MAX_PAGE_SIZE,
                 max_attachment_bytes: int = 15 * 1024 * 1024) -> None:
        super().__init__(base_url, token="", timeout=timeout)
        if not 1 <= int(page_size) <= MAX_PAGE_SIZE:
            raise ValueError(f"Page size must be between 1 and {MAX_PAGE_SIZE}.")
        if not _FIELD_RE.fullmatch(entity_field):
            raise ValueError("Invalid entity field name.")
        self._tenant = (tenant or "").strip()
        self._username = username
        self._password = password
        self._token_ttl = float(token_ttl)
        self._token: str | None = None
        self._token_time = 0.0
        self.entity_field = entity_field
        self.utc_offset_hours = int(utc_offset_hours)
        self.page_size = int(page_size)
        self.max_attachment_bytes = int(max_attachment_bytes)

    # ── configuration guard ───────────────────────────────────────────
    def _require_configured(self) -> None:
        missing = [name for name, value in (
            ("SMAX_BASE", self._api_url), ("SMAX_TENANT", self._tenant),
            ("SMAX_USER", self._username), ("SMAX_PASS", self._password),
        ) if not value]
        if missing:
            raise NotConfiguredError(
                "SMAX source is not configured: set " + ", ".join(missing)
                + " (the connector logs in with the integration account)."
            )

    # ── auth ──────────────────────────────────────────────────────────
    def _auth_headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        return {"Cookie": f"SMAX_AUTH_TOKEN={self._token}; TENANTID={self._tenant}"}

    def authenticate(self, force: bool = False) -> str:
        """Log in and cache the JWT. The TTL is a client refresh hint, not a
        promise about the server's lifetime — a 401/403 always forces a refresh."""
        if not force and self._token and time.monotonic() - self._token_time < self._token_ttl:
            return self._token
        path = f"{AUTH_PATH}?TENANTID={quote(self._tenant, safe='')}"
        try:
            token = self.post_text(path, {"login": self._username, "password": self._password})
        except Exception as exc:  # noqa: BLE001 — re-raised with context
            raise RuntimeError(f"SMAX authentication failed: {exc}") from exc
        if token.count(".") != 2 or not re.fullmatch(r"[A-Za-z0-9_.-]+", token):
            raise RuntimeError("Expected a plain-text JWT authentication token from SMAX.")
        self._token = token
        self._token_time = time.monotonic()
        _log.info("SMAX authenticated (tenant=%s, refresh every %.0fs)",
                  self._tenant, self._token_ttl)
        return token

    def _on_unauthorized(self) -> bool:
        """401/403 (or an HTML login page) → refresh once, let the transport retry."""
        try:
            self.authenticate(force=True)
            return True
        except Exception as exc:  # noqa: BLE001 — the original 401 is more useful
            _log.warning("SMAX re-authentication failed: %s", exc)
            return False

    def _is_login_page(self, raw: bytes, content_type: str) -> bool:
        if "text/html" not in (content_type or "").lower():
            return False
        head = raw[:5000].decode("utf-8", "ignore").lower()
        return "login" in head or "lwsso" in head

    # ── search ────────────────────────────────────────────────────────
    def search(self, filter_expr: str, *, layout: str | None = None,
               page_size: int | None = None):
        """Yield incident entities, paging until a short page arrives."""
        size = int(page_size or self.page_size)
        if not 1 <= size <= MAX_PAGE_SIZE:
            raise ValueError(f"Page size must be between 1 and {MAX_PAGE_SIZE}.")
        fields = layout or _layout_for(self.entity_field)
        skip = 0
        while True:
            path = (f"/rest/{quote(self._tenant, safe='')}/ems/Incident"
                    f"?layout={quote(fields, safe='')}"
                    f"&filter={quote(filter_expr, safe='')}"
                    f"&size={size}&skip={skip}")
            data = self.get_json(path)
            if not isinstance(data, dict):
                raise ValueError("Expected an incident response object.")
            status = (data.get("meta") or {}).get("completion_status")
            if status not in (None, "OK"):
                raise RuntimeError(f"SMAX reported an incomplete or failed search: {status}")
            entities = data.get("entities")
            if not isinstance(entities, list):
                raise ValueError("Expected an 'entities' array.")
            for entity in entities:
                if not isinstance(entity, dict) or not isinstance(entity.get("properties"), dict):
                    raise ValueError("An incident is missing its properties object.")
                yield entity
            if len(entities) < size:
                return
            skip += size

    def get_ticket(self, ticket_id: str) -> dict:
        """Fetch one ticket by Id (raw SMAX entity — see smax_models.py)."""
        for entity in self.search(build_filter(incident_ids=ticket_id,
                                               entity_field=self.entity_field,
                                               utc_offset_hours=self.utc_offset_hours),
                                  page_size=1):
            return entity
        raise KeyError(f"SMAX has no incident with Id={ticket_id!r}")

    def list_by_created(self, *, start=None, end=None, entity_ids=None, incident_ids=None):
        """Incidents created in a date range / belonging to entities or ids."""
        return self.search(build_filter(start=start, end=end, entity_ids=entity_ids,
                                        incident_ids=incident_ids,
                                        entity_field=self.entity_field,
                                        utc_offset_hours=self.utc_offset_hours))

    def list_changed(self, since_ms: int):
        """Incidents updated at or after an epoch-millisecond timestamp.

        The handover documents the creation-time filter and notes that an
        updated-record synchronisation needs the corresponding update-time
        query; that is LastUpdateTime, filtered the same way.
        """
        return self.search(build_filter(changed_since_ms=int(since_ms),
                                        entity_field=self.entity_field,
                                        utc_offset_hours=self.utc_offset_hours))

    # ── attachments ───────────────────────────────────────────────────
    def iter_attachments(self, entity: dict):
        """Visible attachments of one incident entity (metadata only)."""
        properties = entity.get("properties") or {}
        yield from iter_attachment_metadata(properties, identifier(properties.get("Id", "")))

    def download_file(self, file_id: str, *, max_bytes: int | None = None) -> bytes:
        """Download one attachment body from the SMAX file service."""
        limit = int(max_bytes if max_bytes is not None else self.max_attachment_bytes)
        raw = self.get_bytes(f"/rest/{quote(self._tenant, safe='')}/frs/file-list/"
                             f"{quote(identifier(file_id), safe='')}")
        if len(raw) > limit:
            raise ValueError(f"Attachment exceeds the {limit} byte limit.")
        return raw

    # ── no documented contract → refuse rather than invent ────────────
    def write_suggestion(self, ticket_id: str, suggestion: dict) -> None:
        """Write-back is deliberately unimplemented.

        The SMAX handover documents authentication, incident search and
        attachment download only — there is no write endpoint to call. Confirm
        the API (and its permissions) before enabling SMAX_WRITE_BACK.
        """
        raise NotImplementedError(
            "SMAX write-back is not implemented: the integration handover "
            f"documents no write endpoint (attempted for ticket {ticket_id})."
        )
