"""Admin console API — bearer-auth operations for the admin page.

Everything under /admin/* requires the same Bearer token as /api/v1/*
(api/auth.py require_token). Covers: overall status, taxonomy overrides
(add services/offerings to the FROZEN base taxonomy), env credentials
(write to the .env overrides file — restart required), add incident,
full DB reset, cluster group add/adjust, and running the smoke/pytest
suites in-container.

Design rules:
  - Base taxonomy is FROZEN (domain/taxonomy.py). Admin edits go to the
    taxonomy_overrides table + runtime registry; the base never changes.
  - Credentials are written to a file (ADMIN_ENV_FILE, default .env) —
    never stored in the DB. Settings is a frozen import-time singleton,
    so changes take effect after a container restart (the UI says so).
  - Test runs are subprocesses with a hard timeout; output is capped and
    returned as text (no streaming sockets in the API).
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from ai_classification.api.auth import require_token
from ai_classification.api.schemas import ClassifyRequest
from ai_classification.services.classify.classifier import classify_and_store
from ai_classification.shared.config import settings

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"],
                   dependencies=[Depends(require_token)])

# Env keys the admin page may manage. Secrets are written to the env file
# and NEVER echoed back in full — GET returns only set/unset + a mask.
MANAGED_ENV_KEYS = (
    "LLM_API_KEY", "LLM_MODEL", "LLM_API_BASE",
    "INTEGRATION_API_TOKEN", "INTEGRATION_TOKEN",
    "TICKETING_API_TOKEN",
)

_TEST_TIMEOUT_S = 600
_TEST_OUTPUT_CAP = 200_000  # chars returned to the page


# ── Status ────────────────────────────────────────────────────────────

@router.get("/status")
def admin_status():
    from ai_classification.shared.store import store

    incs = store.list_incidents()
    ok = sum(1 for i in incs if i.get("classification_status") == "ok")
    failed = sum(1 for i in incs if i.get("classification_status") == "failed")
    active = sum(1 for i in incs if i.get("status") == "active")
    resolved = sum(1 for i in incs if i.get("status") == "resolved")
    clusters = store.list_clusters()
    unassigned = len(store.list_incidents())  # derived: incidents without a cluster row
    pool_ids = store.cluster_member_ids  # noqa: F841 — see below

    # Unassigned pool is derived (no cluster_members row). Count it properly.
    assigned_ids = set()
    for c in clusters:
        assigned_ids.update(store.cluster_member_ids(c["id"]))
    unassigned = sum(1 for i in incs if i["id"] not in assigned_ids)

    return {
        "store_ready": store.ready,
        "embedding_ready": store.embedding_ready(),
        "model": settings.llm_model,
        "model_version": getattr(settings, "llm_model", ""),
        "incidents": {
            "total": len(incs), "ok": ok, "failed": failed,
            "active": active, "resolved": resolved,
        },
        "clusters": {
            "total": len(clusters),
            "active": sum(1 for c in clusters if c["status"] == "active"),
            "proposed": sum(1 for c in clusters if c["status"] == "proposed"),
        },
        "unassigned_pool": unassigned,
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "version": "0.2.0",
    }


# ── Taxonomy overrides ────────────────────────────────────────────────

@router.get("/taxonomy")
def admin_taxonomy():
    """Full effective taxonomy (frozen base + overrides) + override list."""
    from ai_classification.domain.taxonomy import (
        AffectedSystem,
        effective_services_by_system,
        runtime_overrides,
    )
    eff = effective_services_by_system()
    systems = []
    for system in AffectedSystem:
        services = eff.get(system, {})
        systems.append({
            "system": system.value,
            "services": [
                {"service": s, "offerings": list(o)}
                for s, o in services.items()
            ],
        })
    return {"systems": systems, "overrides": runtime_overrides()}


@router.post("/taxonomy/service")
def admin_taxonomy_add_service(payload: dict):
    """Add a service (optionally with offerings) under a system."""
    from ai_classification.shared.store import store
    system = str(payload.get("system", "")).strip()
    service = str(payload.get("service", "")).strip()
    offerings = payload.get("offerings") or []
    if not system or not service:
        raise HTTPException(422, "system and service are required")
    store.upsert_taxonomy_override(system, service, "")  # the service row
    for o in offerings:
        if str(o).strip():
            store.upsert_taxonomy_override(system, service, str(o).strip())
    store.reload_taxonomy_overrides()
    return {"status": "ok", "added": {"system": system, "service": service,
            "offerings": [str(o) for o in offerings]}}


@router.post("/taxonomy/offering")
def admin_taxonomy_add_offering(payload: dict):
    """Add an offering under an existing service."""
    from ai_classification.shared.store import store
    system = str(payload.get("system", "")).strip()
    service = str(payload.get("service", "")).strip()
    offering = str(payload.get("offering", "")).strip()
    if not system or not service or not offering:
        raise HTTPException(422, "system, service and offering are required")
    store.upsert_taxonomy_override(system, service, offering)
    store.reload_taxonomy_overrides()
    return {"status": "ok", "added": {"system": system, "service": service,
            "offering": offering}}


@router.delete("/taxonomy/service")
def admin_taxonomy_delete_service(payload: dict):
    """Remove ALL override rows for a service (service + its offerings)."""
    from ai_classification.shared.store import store
    system = str(payload.get("system", "")).strip()
    service = str(payload.get("service", "")).strip()
    if not system or not service:
        raise HTTPException(422, "system and service are required")
    removed = 0
    for row in store.list_taxonomy_overrides():
        if row["system"] == system and row["service"] == service:
            if store.delete_taxonomy_override(system, service, row["offering"]):
                removed += 1
    store.reload_taxonomy_overrides()
    return {"status": "ok", "removed": removed}


@router.delete("/taxonomy/offering")
def admin_taxonomy_delete_offering(payload: dict):
    from ai_classification.shared.store import store
    system = str(payload.get("system", "")).strip()
    service = str(payload.get("service", "")).strip()
    offering = str(payload.get("offering", "")).strip()
    if not system or not service or not offering:
        raise HTTPException(422, "system, service and offering are required")
    ok = store.delete_taxonomy_override(system, service, offering)
    store.reload_taxonomy_overrides()
    return {"status": "ok" if ok else "not-found", "deleted": ok}


@router.post("/taxonomy/import")
def admin_taxonomy_import(payload: dict):
    """Bulk-add taxonomy by pasting JSON.

    Payload shape (same as the effective view):
        {"system": {"service": ["offering", ...], "other": []}, ...}

    Every service/offering in the payload is upserted as an override.
    Existing overrides for those keys are kept; nothing is deleted by
    this endpoint (use the per-service DELETE for removals).
    """
    from ai_classification.shared.store import store
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(422, "payload must be a non-empty JSON object: "
                                '{"System": {"Service": ["Offering", ...]}}')
    services_added = 0
    offerings_added = 0
    for system, services in payload.items():
        system = str(system).strip()
        if not isinstance(services, dict):
            continue
        for service, offerings in services.items():
            service = str(service).strip()
            if not service:
                continue
            store.upsert_taxonomy_override(system, service, "")
            services_added += 1
            if isinstance(offerings, list):
                for o in offerings:
                    o = str(o).strip()
                    if o:
                        store.upsert_taxonomy_override(system, service, o)
                        offerings_added += 1
    store.reload_taxonomy_overrides()
    return {"status": "ok", "services_added": services_added,
            "offerings_added": offerings_added}


# ── Env credentials (write to .env overrides file; restart required) ──

def _env_file_path() -> Path:
    return Path(getattr(settings, "admin_env_file", "") or ".env")


@router.get("/env")
def admin_env_list():
    """Managed env keys: set/unset + masked value (never full secrets)."""
    out = []
    for key in MANAGED_ENV_KEYS:
        val = os.environ.get(key, "")
        out.append({
            "key": key,
            "set": bool(val),
            "masked": (val[:6] + "…" + val[-4:]) if len(val) > 12
                      else ("***" if val else ""),
        })
    return {"keys": out, "file": str(_env_file_path())}


@router.post("/env")
def admin_env_write(payload: dict):
    """Append/replace KEY=VALUE in the env file. Takes effect after a
    container restart (Settings is a frozen import-time singleton)."""
    key = str(payload.get("key", "")).strip()
    value = payload.get("value", "")
    if key not in MANAGED_ENV_KEYS:
        raise HTTPException(422, f"key must be one of {MANAGED_ENV_KEYS}")
    if value is None:
        value = ""
    path = _env_file_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        # replace existing occurrence of the key, else append
        new_lines, replaced = [], False
        for ln in lines:
            if ln.split("=", 1)[0].strip() == key:
                new_lines.append(f"{key}={value}")
                replaced = True
            else:
                new_lines.append(ln)
        if not replaced:
            new_lines.append(f"{key}={value}")
        path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except OSError as exc:
        raise HTTPException(500, f"failed to write env file: {exc}") from exc
    return {"status": "ok", "key": key, "restart_required": True,
            "note": "Restart the container for this to take effect."}


# ── Incidents ─────────────────────────────────────────────────────────

@router.post("/incidents")
def admin_add_incident(req: ClassifyRequest):
    """Add + classify one incident (same pipeline as POST /classify)."""
    return classify_and_store(
        req.title, req.description, req.extracted_text,
        documents=req.documents,
        assign_group=req.assign_group,
        assignee=req.assignee,
        priority=req.priority,
        notes=req.notes,
        discussion_history=req.discussion_history,
        escalation_info=req.escalation_info,
        completion_code=req.completion_code,
        source_ticket_id=req.source_ticket_id,
        affected_system=req.affected_system,
    )


# ── Full DB reset ─────────────────────────────────────────────────────

@router.post("/reset")
def admin_reset():
    """Delete ALL incidents + clusters + review queue + ingestion jobs."""
    from ai_classification.shared.store import store
    count = store.delete_all()
    # review queue + ingestion jobs are not covered by delete_all
    import psycopg2
    from ai_classification.shared.config import settings as s
    conn = psycopg2.connect(host=s.pg_host, port=s.pg_port, user=s.pg_user,
                            password=s.pg_password, dbname=s.pg_database)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM manual_review_queue")
            cur.execute("DELETE FROM ingestion_jobs")
    finally:
        conn.close()
    return {"status": "reset", "incidents_deleted": count,
            "queue_and_jobs_cleared": True}


# ── Cluster groups (add / adjust) ─────────────────────────────────────

@router.get("/groups")
def admin_groups():
    from ai_classification.shared.store import store
    clusters = store.list_clusters()
    out = []
    for c in clusters:
        members = store.list_cluster_members(c["id"])
        out.append({
            "cluster_id": c["id"],
            "name_ar": c["name_ar"],
            "name_en": c.get("name_en") or "",
            "description": c.get("description") or "",
            "status": c["status"],
            "member_count": len(members),
            "members": [{"incident_id": m["incident_id"], "title": m.get("title", "")}
                        for m in members],
        })
    return {"groups": out}


@router.post("/groups")
def admin_group_create(payload: dict):
    """Create a cluster group (optionally with member incident ids)."""
    from ai_classification.shared.store import store
    name_ar = str(payload.get("name_ar", "")).strip()
    if not name_ar:
        raise HTTPException(422, "name_ar is required")
    description = str(payload.get("description", "") or "")
    status = str(payload.get("status", "active"))
    cluster_id = store.generate_id()
    cluster = store.create_cluster(cluster_id, name_ar, description,
                                   status=status)
    if cluster is None:
        raise HTTPException(500, "failed to create cluster")
    for iid in payload.get("member_ids") or []:
        store.add_cluster_member(cluster_id, str(iid))
    return {"status": "ok", "cluster": cluster}


@router.patch("/groups/{cluster_id}")
def admin_group_adjust(cluster_id: str, payload: dict):
    """Adjust name/description/status of a group."""
    from ai_classification.shared.store import store
    c = store.get_cluster(cluster_id)
    if c is None:
        raise HTTPException(404, "cluster not found")
    fields = {}
    for k in ("name_ar", "name_en", "description"):
        if k in payload:
            fields[k] = str(payload[k])
    if "status" in payload:
        store.set_cluster_status(cluster_id, str(payload["status"]))
    if fields:
        store.update_cluster_fields(cluster_id, **fields)
    return {"status": "ok", "cluster": store.get_cluster(cluster_id)}


@router.delete("/groups/{cluster_id}")
def admin_group_delete(cluster_id: str):
    """Delete a group AND its members (members return to the unassigned pool)."""
    from ai_classification.shared.store import store
    if store.get_cluster(cluster_id) is None:
        raise HTTPException(404, "cluster not found")
    ok = store.delete_cluster(cluster_id)
    return {"status": "ok" if ok else "not-found", "deleted": ok}


@router.post("/groups/{cluster_id}/members")
def admin_group_add_member(cluster_id: str, payload: dict):
    from ai_classification.shared.store import store
    iid = str(payload.get("incident_id", "")).strip()
    if not iid:
        raise HTTPException(422, "incident_id is required")
    ok = store.add_cluster_member(cluster_id, iid)
    if not ok:
        raise HTTPException(404, "cluster not found")
    return {"status": "ok", "cluster_id": cluster_id, "incident_id": iid}


@router.delete("/groups/{cluster_id}/members/{incident_id}")
def admin_group_remove_member(cluster_id: str, incident_id: str):
    from ai_classification.shared.store import store
    ok = store.remove_cluster_member(cluster_id, incident_id)
    return {"status": "ok" if ok else "not-found", "deleted": ok}


# ── Run tests (smoke + pytest, in-container subprocess) ───────────────

def _run(cmd: list[str]) -> dict:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_TEST_TIMEOUT_S)
        output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        return {
            "exit_code": proc.returncode,
            "output": output[-_TEST_OUTPUT_CAP:],
            "truncated": len(output) > _TEST_OUTPUT_CAP,
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "output": f"TIMED OUT after {_TEST_TIMEOUT_S}s"}
    except FileNotFoundError as exc:
        return {"exit_code": -2, "output": f"command not found: {exc}"}


@router.post("/tests/smoke")
def admin_run_smoke():
    """Run smoke_test.sh against the live API (in-container)."""
    script = "/app/smoke_test.sh"
    if not os.path.exists(script):
        return {"exit_code": -2, "output": f"smoke_test.sh not found at {script} "
                "(mount it into the container: ./smoke_test.sh:/app/smoke_test.sh:ro)"}
    env = dict(os.environ)
    env["SMOKE_API_URL"] = "http://localhost:8000"
    env["SMOKE_DASH_URL"] = "http://nginx:8082"  # compose service name, not localhost
    try:
        proc = subprocess.run(["bash", script], capture_output=True, text=True,
                              timeout=_TEST_TIMEOUT_S, env=env)
        output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        return {"exit_code": proc.returncode, "output": output[-_TEST_OUTPUT_CAP:],
                "truncated": len(output) > _TEST_OUTPUT_CAP}
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "output": f"TIMED OUT after {_TEST_TIMEOUT_S}s"}
    except FileNotFoundError as exc:
        return {"exit_code": -2, "output": f"command not found: {exc}"}


@router.post("/tests/pytest")
def admin_run_pytest():
    """Run the full pytest suite in-container (needs tests/ mounted)."""
    import shutil
    if shutil.which("pytest") is None and shutil.which("uv") is None:
        return {"exit_code": -2, "output": "pytest not installed in the image"}
    tests_dir = "/app/tests"
    if not os.path.isdir(tests_dir):
        return {"exit_code": -2, "output": f"tests dir not found at {tests_dir} "
                "(mount it: ./tests:/app/tests:ro)"}
    env = dict(os.environ)
    env["PG_DATABASE"] = "ai_incidents_test"
    env["INTEGRATION_TOKEN"] = env.get("INTEGRATION_API_TOKEN", "test-token")
    env["INTEGRATION_WORKER_ENABLED"] = "0"
    if shutil.which("uv"):
        cmd = ["uv", "run", "pytest", "tests/", "-q"]
    else:
        cmd = ["python", "-m", "pytest", "tests/", "-q"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_TEST_TIMEOUT_S, cwd="/app", env=env)
        output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        return {"exit_code": proc.returncode, "output": output[-_TEST_OUTPUT_CAP:],
                "truncated": len(output) > _TEST_OUTPUT_CAP}
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "output": f"TIMED OUT after {_TEST_TIMEOUT_S}s"}
