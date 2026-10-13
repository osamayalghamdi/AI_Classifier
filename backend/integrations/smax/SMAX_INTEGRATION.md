# SMAX Integration Handover

## 1. Overview

This guide describes a reusable integration that authenticates with SMAX, retrieves incidents by date range, entity, or incident ID, and downloads each incident's visible attachments.

The HTTP contracts can be implemented in any system that supports HTTPS and JSON. The Python example is self-contained: it uses standard-library modules and the `requests` package, with no dependency on an existing application or project.

### Workflow

```text
Authenticate -> Search and paginate incidents -> Read attachment metadata
             -> Download each attachment -> Return records and outcomes
```

| Function | Input | Output |
| --- | --- | --- |
| `authenticate()` | Configured server, tenant, and credentials | Cached authentication token |
| `fetch_incidents()` | Date boundaries, entity IDs, or incident IDs | Iterator of incident records |
| `iter_attachments()` | One incident record | Iterator of visible attachment records |
| `download_attachment()` | Attachment metadata and output location | Download result or an exception |
| `export_incidents()` | Client, search criteria, and output location | Export files and a run summary |

## 2. Setup

Use Python 3.10 or later and install the single direct dependency:

```shell
python -m pip install "requests>=2.32,<3"
```

Provide these environment variables through the host system, service configuration, or secret manager:

| Variable | Value |
| --- | --- |
| `SMAX_BASE` | HTTPS SMAX base URL, for example `https://smax.example.com` |
| `SMAX_TENANT` | Target tenant identifier |
| `SMAX_USER` | Integration account username |
| `SMAX_PASS` | Integration account password |
| `SMAX_OUTPUT` | Optional output location; defaults to `smax_export` |

The account requires access to the selected incidents and their files. Keep TLS verification enabled and keep credentials and tokens out of logs.

**How to run the example:** Copy the five Python code blocks in sections 3 through 7, in order, into one new file named `smax_example.py`. Set the environment variables, adjust the search criteria in section 7, and run:

```shell
python smax_example.py
```

The blocks form one complete program. Each later block uses definitions from the earlier blocks. They contain real HTTP calls; the configured server and credentials must be valid.

## 3. Function 1: Authenticate and retrieve the API token

### HTTP contract

```http
POST {SMAX_BASE}/auth/authentication-endpoint/authenticate/token?TENANTID={SMAX_TENANT}
Content-Type: application/json
```

```json
{
  "login": "<integration-account>",
  "password": "<password>"
}
```

The expected response is HTTP `200` with a plain-text JWT token. The token is a temporary API credential. Send it with the tenant on subsequent requests:

```http
Cookie: SMAX_AUTH_TOKEN=<token>; TENANTID=<tenant>
```

The example caches the token for 25 minutes by default. This is a configurable client refresh interval, not a guarantee of the server's token lifetime. A `401` or `403` causes one forced refresh and retry. Persistent errors are raised to the caller.

The shared transport retries transient connection failures and HTTP `429`, `500`, `502`, `503`, and `504`, with backoff and support for `Retry-After`. Unexpected redirects are reported so configuration or authentication problems remain visible.

### Code block 1 of 5: Imports, client, and authentication

```python
import json
import os
import re
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def identifier(value):
    text = str(value).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", text):
        raise ValueError("Identifiers must contain letters, digits, '_' or '-'.")
    return text


class SmaxClient:
    def __init__(self, base_url, tenant, username, password,
                 token_ttl=1500, verify=True):
        url = urlsplit(base_url)
        if (url.scheme != "https" or not url.netloc or url.username
                or url.password or url.query or url.fragment):
            raise ValueError("Provide an HTTPS base URL without embedded credentials.")
        if not username or not password or token_ttl <= 0:
            raise ValueError("Credentials and a positive token lifetime are required.")
        self.base_url = base_url.rstrip("/")
        self.tenant = identifier(tenant)
        self.username = username
        self.password = password
        self.token_ttl = token_ttl
        self.token = None
        self.token_time = 0
        self.session = requests.Session()
        self.session.verify = verify
        retry = Retry(
            total=4,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(("GET", "POST")),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def authenticate(self, force=False):
        if (not force and self.token
                and time.monotonic() - self.token_time < self.token_ttl):
            return self.token
        url = self.base_url + "/auth/authentication-endpoint/authenticate/token"
        with self.session.post(
            url,
            params={"TENANTID": self.tenant},
            json={"login": self.username, "password": self.password},
            timeout=(15, 120),
            allow_redirects=False,
        ) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise RuntimeError("Unexpected authentication response.")
            token = response.text.strip()
            if (token.count(".") != 2
                    or not re.fullmatch(r"[A-Za-z0-9_.-]+", token)):
                raise RuntimeError("Expected a plain-text JWT authentication token.")
        self.token = token
        self.token_time = time.monotonic()
        return token

    def get(self, path, params=None, stream=False):
        self.authenticate()
        for attempt in range(2):
            response = self.session.get(
                self.base_url + path,
                params=params,
                headers={
                    "Cookie": (
                        f"SMAX_AUTH_TOKEN={self.token}; TENANTID={self.tenant}"
                    ),
                    "Accept": "*/*" if stream else "application/json",
                },
                stream=stream,
                timeout=(15, 120),
                allow_redirects=False,
            )
            html_login = (
                not stream
                and "text/html" in response.headers.get("Content-Type", "").lower()
                and any(word in response.text[:5000].lower()
                        for word in ("login", "lwsso"))
            )
            if attempt == 0 and (response.status_code in (401, 403) or html_login):
                response.close()
                self.authenticate(force=True)
                continue
            try:
                response.raise_for_status()
                if response.status_code != 200:
                    raise RuntimeError(
                        f"Unexpected SMAX response: HTTP {response.status_code}."
                    )
                if html_login:
                    raise RuntimeError("SMAX returned a login page after refresh.")
            except Exception:
                response.close()
                raise
            return response
        raise RuntimeError("SMAX authentication retry failed.")

    def close(self):
        self.session.close()
```

## 4. Function 2: Fetch incidents

### HTTP contract

```http
GET {SMAX_BASE}/rest/{SMAX_TENANT}/ems/Incident
Accept: application/json
Cookie: SMAX_AUTH_TOKEN=<token>; TENANTID=<tenant>
```

| Query parameter | Purpose |
| --- | --- |
| `layout` | Comma-separated list of requested fields |
| `filter` | Search expression |
| `size` | Requested page size |
| `skip` | Offset for pagination |

### Search criteria

| Input | SMAX field | Behavior |
| --- | --- | --- |
| `start`, `end` | `EmsCreationTime` | Inclusive creation dates in `YYYY-MM-DD` format |
| `entity_ids` | `Entity_c` by default | One entity ID or a list of entity IDs |
| `incident_ids` | `Id` | One incident ID or a list of incident IDs |
| `entity_field` | Configurable | Overrides the custom entity field name |
| `utc_offset_hours` | Date conversion setting | Defaults to `3` for Riyadh time |

Different criteria combine with AND. Multiple IDs within one field combine with an `in` expression. An incident ID means the record's `Id`; confirm the mapping if a target system uses a separate display number.

Example SMAX filters:

```text
Id = '100001'
Entity_c = '200001'
Id in ('100001','100002')
(EmsCreationTime >= <start_epoch_ms> and EmsCreationTime <= <end_epoch_ms>)
```

Dates include the entire final day, through `23:59:59.999`. A single date boundary is also accepted. At least one date boundary, entity ID, or incident ID is required. These examples use creation time; an updated-record synchronization should define a corresponding update-time query.

Responses contain an `entities` array, with each record's fields under `properties`. The example requests pages until it receives fewer records than the requested size. Select a page size supported by the target tenant.

### Code block 2 of 5: Validated search and pagination

```python
def id_condition(field, values):
    if values is None:
        return None
    if isinstance(values, (str, int)):
        values = [values]
    values = [identifier(value) for value in values]
    if not values:
        raise ValueError("An identifier list cannot be empty.")
    if len(values) == 1:
        return f"{field} = '{values[0]}'"
    return field + " in (" + ",".join(f"'{value}'" for value in values) + ")"


def build_filter(start=None, end=None, entity_ids=None, incident_ids=None,
                 entity_field="Entity_c", utc_offset_hours=3):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", entity_field):
        raise ValueError("Invalid entity field name.")
    tz = timezone(timedelta(hours=utc_offset_hours))
    first = date.fromisoformat(str(start)) if start is not None else None
    last = date.fromisoformat(str(end)) if end is not None else None
    if first and last and first > last:
        raise ValueError("Start date must not be later than end date.")
    parts = []
    if first:
        boundary = datetime.combine(first, datetime.min.time(), tzinfo=tz)
        parts.append(f"EmsCreationTime >= {int(boundary.timestamp() * 1000)}")
    if last:
        boundary = datetime.combine(last, datetime.min.time(), tzinfo=tz)
        boundary += timedelta(days=1, milliseconds=-1)
        parts.append(f"EmsCreationTime <= {int(boundary.timestamp() * 1000)}")
    for field, values in (("Id", incident_ids), (entity_field, entity_ids)):
        condition = id_condition(field, values)
        if condition:
            parts.append(condition)
    if not parts:
        raise ValueError("Provide a date boundary, entity ID, or incident ID.")
    return "(" + " and ".join(parts) + ")"


def fetch_incidents(client, *, page_size=250, **criteria):
    if not isinstance(page_size, int) or not 1 <= page_size <= 250:
        raise ValueError("Page size must be between 1 and 250.")
    expression = build_filter(**criteria)
    entity_field = criteria.get("entity_field", "Entity_c")
    layout = ",".join((
        "Id", "DisplayLabel", "Description", "Status",
        "EmsCreationTime", "LastUpdateTime", entity_field,
        "IncidentAttachments", "NumberOfAttachments",
    ))
    skip = 0
    while True:
        with client.get(
            f"/rest/{client.tenant}/ems/Incident",
            params={
                "layout": layout,
                "filter": expression,
                "size": page_size,
                "skip": skip,
            },
        ) as response:
            data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Expected an incident response object.")
        metadata = data.get("meta") or {}
        if metadata.get("completion_status") not in (None, "OK"):
            raise RuntimeError("SMAX reported an incomplete or failed search.")
        incidents = data.get("entities")
        if not isinstance(incidents, list):
            raise ValueError("Expected an 'entities' array.")
        for incident in incidents:
            if (not isinstance(incident, dict)
                    or not isinstance(incident.get("properties"), dict)):
                raise ValueError("An incident is missing its properties object.")
            yield incident
        if len(incidents) < page_size:
            break
        skip += page_size
```

## 5. Function 3: Iterate through attachment metadata

Read attachments from `properties.IncidentAttachments`. The example accepts either serialized JSON or a parsed object/list and reads entries from `complexTypeProperties` where present.

Attachments marked as hidden are excluded. Incidents without attachments remain in the incident export. Malformed metadata is reported explicitly.

Each yielded record contains:

| Field | Purpose |
| --- | --- |
| `incident_id` | Owning incident |
| `file_id` | SMAX file identifier used for download |
| `file_name` | Original attachment filename |
| `mime_type` | Reported content type |
| `expected_size` | Expected bytes, or `None` if unavailable |

### Code block 3 of 5: Visible attachment iterator

```python
def iter_attachments(incident):
    properties = incident["properties"]
    incident_id = identifier(properties["Id"])
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
        expected_size = int(raw_size) if raw_size not in (None, "") else None
        if expected_size is not None and expected_size < 0:
            raise ValueError("Attachment size cannot be negative.")
        yield {
            "incident_id": incident_id,
            "file_id": identifier(attachment["id"]),
            "file_name": str(attachment.get("file_name") or "attachment.bin"),
            "mime_type": str(attachment.get("mime_type") or ""),
            "expected_size": expected_size,
        }
```

## 6. Function 4: Download an attachment

### HTTP contract

```http
GET {SMAX_BASE}/rest/{SMAX_TENANT}/frs/file-list/{file_id}
Accept: */*
Cookie: SMAX_AUTH_TOKEN=<token>; TENANTID=<tenant>
```

Files are streamed into a temporary file, checked against the configured limit and expected size, and then moved to their final location. The default maximum is 50 MiB per attachment.

Relative to the configured attachment output location, files use:

```text
tenant_<tenant_id>/
  incident_<incident_id>/
    file_<full_file_id>_<safe_filename>
```

The full file ID prevents ordinary filename collisions. Identifiers are validated, filenames are sanitized, and resolved paths are checked against the output root.

| Result | Meaning |
| --- | --- |
| `OK` | File downloaded; expected size checked when available |
| `SKIPPED` | Existing file matches the known expected size |
| Exception | Caller records a failed download and can continue |

Existing files with unknown expected sizes are downloaded again. Reuse checks compare size, not content hashes. Interrupted downloads restart from the beginning, and temporary files are cleaned up. The example uses one sequential worker; use separate clients and coordination if adding parallel workers.

### Code block 4 of 5: Download, validation, and recovery

```python
class IncompleteDownloadError(RuntimeError):
    pass


def safe_filename(value):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip(" .")
    return name[:100].rstrip(" .") or "attachment.bin"


def download_attachment(client, attachment, output_dir, max_bytes=50 * 1024 * 1024):
    if max_bytes <= 0:
        raise ValueError("Maximum file size must be positive.")
    incident_id = identifier(attachment["incident_id"])
    file_id = identifier(attachment["file_id"])
    expected = attachment.get("expected_size")
    if expected is not None:
        expected = int(expected)
        if expected < 0 or expected > max_bytes:
            raise ValueError("Attachment size is outside the configured limit.")

    root = Path(output_dir).resolve()
    folder = (root / f"tenant_{client.tenant}" / f"incident_{incident_id}").resolve()
    destination = (folder / f"file_{file_id}_{safe_filename(attachment['file_name'])}").resolve()
    if not folder.is_relative_to(root) or not destination.is_relative_to(root):
        raise ValueError("Attachment destination escapes the output location.")
    folder.mkdir(parents=True, exist_ok=True)

    def result(status, size):
        return {
            **attachment,
            "status": status,
            "storage_reference": str(destination.relative_to(root)),
            "bytes_downloaded": size,
            "error": None,
        }

    # Unknown-size files are downloaded again because size cannot be verified.
    if (expected is not None and destination.is_file()
            and destination.stat().st_size == expected):
        return result("SKIPPED", expected)

    for attempt in range(3):
        temporary = None
        try:
            with client.get(
                f"/rest/{client.tenant}/frs/file-list/{quote(file_id, safe='')}",
                stream=True,
            ) as response:
                with tempfile.NamedTemporaryFile(
                    dir=folder, prefix="download_", suffix=".part", delete=False
                ) as handle:
                    temporary = Path(handle.name)
                    received = 0
                    for chunk in response.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        received += len(chunk)
                        if received > max_bytes:
                            raise ValueError("Attachment exceeds the size limit.")
                        handle.write(chunk)
                if expected is not None and received != expected:
                    raise IncompleteDownloadError("Downloaded size does not match metadata.")
            temporary.replace(destination)
            return result("OK", received)
        except (requests.RequestException, IncompleteDownloadError) as error:
            # HTTP status retries already happened in the transport layer.
            if isinstance(error, requests.HTTPError) or attempt == 2:
                raise
            time.sleep(2 ** attempt)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
```

## 7. Run the complete workflow

This function connects authentication, incident retrieval, attachment iteration, and downloads. It writes each incident as it arrives, records individual download failures, and continues with later attachments.

Search or authentication failures stop the run and propagate to the caller. A malformed attachment collection produces a metadata error for that incident. The result summary includes those errors separately from failed file downloads.

### Code block 5 of 5: Export and runnable entry point

```python
def export_incidents(client, criteria, output_dir="smax_export"):
    # Validate before authenticating or creating output files.
    build_filter(**criteria)
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    summary = {
        "incidents": 0,
        "downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "metadata_errors": 0,
    }
    client.authenticate()
    with (
        (root / "incidents.jsonl").open("w", encoding="utf-8") as incident_log,
        (root / "attachment_results.jsonl").open("w", encoding="utf-8") as result_log,
    ):
        for incident in fetch_incidents(client, **criteria):
            summary["incidents"] += 1
            incident_log.write(json.dumps(incident, ensure_ascii=False) + "\n")
            incident_log.flush()
            try:
                attachments = list(iter_attachments(incident))
            except (ValueError, TypeError, KeyError) as error:
                summary["metadata_errors"] += 1
                result_log.write(json.dumps({
                    "incident_id": str(incident["properties"].get("Id", "")),
                    "status": "FAILED",
                    "stage": "attachment_metadata",
                    "error": str(error),
                }, ensure_ascii=False) + "\n")
                result_log.flush()
                continue
            for attachment in attachments:
                try:
                    result = download_attachment(client, attachment, root / "attachments")
                    counter = "downloaded" if result["status"] == "OK" else "skipped"
                    summary[counter] += 1
                except (requests.RequestException, RuntimeError, OSError,
                        ValueError, TypeError, KeyError) as error:
                    summary["failed"] += 1
                    result = {
                        **attachment,
                        "status": "FAILED",
                        "storage_reference": None,
                        "bytes_downloaded": None,
                        "error": str(error),
                    }
                result_log.write(json.dumps(result, ensure_ascii=False) + "\n")
                result_log.flush()
    return summary


if __name__ == "__main__":
    client = SmaxClient(
        base_url=os.environ["SMAX_BASE"],
        tenant=os.environ["SMAX_TENANT"],
        username=os.environ["SMAX_USER"],
        password=os.environ["SMAX_PASS"],
    )
    try:
        # Replace these example dates with the required search period.
        criteria = {"start": "2026-10-01", "end": "2026-10-07"}
        summary = export_incidents(
            client, criteria, output_dir=os.getenv("SMAX_OUTPUT", "smax_export")
        )
        print(json.dumps(summary, indent=2))
    finally:
        client.close()
```

### Alternative criteria

Replace the `criteria` dictionary in the entry point with the required selection. These JSON objects also use valid Python dictionary syntax.

**Date range:**

```json
{"start": "2026-10-01", "end": "2026-10-07"}
```

**One incident:**

```json
{"incident_ids": "100001"}
```

**Multiple incidents:**

```json
{"incident_ids": ["100001", "100002"]}
```

**One entity:**

```json
{"entity_ids": "200001"}
```

**Entity within a date range:**

```json
{
  "start": "2026-10-01",
  "end": "2026-10-07",
  "entity_ids": ["200001", "200002"]
}
```

The IDs and dates above are illustrative. Replace them with the required values.

## 8. Outputs and integration into another system

The runnable example writes these outputs relative to `SMAX_OUTPUT`:

| Output | Contents |
| --- | --- |
| `incidents.jsonl` | One complete incident JSON object per line |
| `attachment_results.jsonl` | One download result or metadata error per line |
| `attachments/` | Downloaded original files |
| Standard output | Summary counts for the completed run |

The two JSONL reports are replaced at the start of each authenticated export. Downloaded files remain available for reuse. Use a distinct output location per source server, and a run-specific report location when report history is required.

A successful attachment result resembles:

```json
{
  "incident_id": "100001",
  "file_id": "example-file-id",
  "file_name": "document.pdf",
  "mime_type": "application/pdf",
  "expected_size": 245760,
  "status": "OK",
  "storage_reference": "tenant_123/incident_100001/file_example-file-id_document.pdf",
  "bytes_downloaded": 245760,
  "error": null
}
```

The `storage_reference` is relative to the `attachments` directory.

To connect another application, consume `fetch_incidents()` directly and deliver each incident to a database, queue, or application service. Use `iter_attachments()` and `download_attachment()` for its files. The export function is an example of orchestration; the individual functions can be called independently.

For object storage, replace the local destination handling with temporary staging and an object upload, then return the object key. Preserve the incident-to-attachment relationship.

| Record | Suggested identity |
| --- | --- |
| Incident | `(source_server, tenant_id, incident_id)` |
| Attachment | `(source_server, tenant_id, incident_id, file_id)` |

Keep identifiers as strings and preserve UTF-8 content. Upsert records using these identities to avoid duplicates during repeat runs.

## 9. Validation and deployment notes

The example is designed for the HTTP routes, cookie authentication, JWT response, and incident/attachment structures documented here. Confirm these contracts and the custom entity field against the target tenant.

| Check | Expected result |
| --- | --- |
| Valid credentials | Token returned and cached |
| Expired or rejected token | One forced refresh before reporting a persistent failure |
| Date range | Inclusive boundaries in the configured fixed UTC offset |
| Entity or incident IDs | Correct server-side conditions |
| Multiple pages | All pages yielded until a short page is received |
| No matches | Successful empty export |
| No attachments | Incident exported without file requests |
| Hidden attachments | Excluded |
| Invalid metadata | Recorded as an incident metadata error |
| Repeated filenames | Separate paths based on full file IDs |
| Known-size completed file | Reused |
| Interrupted or truncated file | Bounded retry and temporary-file cleanup |
| Oversized file | Rejected |
| One failed attachment | Failure recorded; other attachments continue |

The example has been checked with simulated SMAX responses, including authentication refresh, filtering, pagination, attachment parsing, file downloads, and failure continuation. Live target-tenant connectivity and permissions require environment-specific verification.

