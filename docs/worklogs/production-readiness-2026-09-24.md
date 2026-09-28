# Production readiness — cluster/classifier fixes (2026-09-24)

Companion to `cluster-v5-audit-thrash.md` and `result-review-2026-09-24.md`.
This is the "is it safe to run this in production?" checklist for the changes
made in that session, with the checks that were actually run.

## What changed

| Area | File | Nature |
|---|---|---|
| Canonical statements no longer have ids/error codes/dates stripped | `services/classify/parsing.py` | data-quality fix |
| Near-miss service strings snap onto the real taxonomy option; the resolved system is never replaced by "Other" | `services/classify/{parsing,cascade}.py` | robustness fix |
| Classifier prompt v3.5: Haj/Umrah evidence rule, unmarked Nusuk → Haj, Rawdah-group rule, triage counter-examples | `services/classify/prompts.py`, `classifier.py` | prompt change |
| Deterministic Rawdah(group) system resolution | `services/classify/cascade.py` | new rule, pre-LLM |
| Audit sees each member's service; generic/abstained labels are explicitly NOT evidence; conservative-removal rules | `services/cluster/persistent.py` | prompt change |
| Audit verdicts are binding (veto ledger), human pins, no naming of empty clusters, system reconciliation, service-consistency guard on minting | `services/cluster/persistent.py`, `shared/store_clusters.py` | behaviour changes |
| Concurrent mint no longer raises (ON CONFLICT DO NOTHING) | `shared/store_clusters.py` | race hardening |
| `/test/all` now fails on cluster invariant violations (cross-system members, empty active rows) | `api/diagnostics.py` | monitoring |
| The system the TICKETING system supplies now reaches the classifier end-to-end (`affected_system` in the ingest contract → job → `Incident` → pinned system) and is honoured on BOTH classifier paths | `services/jobs/integration/{schemas,__init__}.py`, `api/integration.py`, `seams/local_source.py`, `services/classify/{classifier,cascade}.py` | new capability |
| A pinned system can no longer carry a foreign service ("CRM + a Nusuk Masar Haj service") | `services/classify/cascade.py` | consistency fix |
| **Deployment hardening (the two open items from the 2026-09-26 review)** — compose now binds DB/API/OCR to `127.0.0.1` by default (only nginx:8082 is public), adds OCR env/healthcheck, per-service CPU/mem limits and `no-new-privileges`; a separate `ADMIN_TOKEN` guards `/admin/*` (falls back to the integration token with a loud warning when unset). Helm chart carries the same `securityContext` and the `ADMIN_TOKEN` secret key. | `docker-compose.yml`, `ai-classifier/{values.yaml,templates/*}`, `api/{auth,admin}.py`, `shared/config.py`, `.env.example` | hardening |

**No schema change, no new dependency, no config/env change.** `store.setup()`
DDL is unchanged and idempotent; `prompt_version` is only recorded, never
filtered on.

## Deployment

* The API runs uvicorn **without** `--reload`: a restart (or
  `docker compose up -d --build api`) is required to pick the code up. Done in
  this environment; verified the running process started after the last edit.
* Nothing to migrate: existing rows keep working. Rows classified by an older
  prompt version stay valid; provenance is per row.
* Rollback: revert the commit and restart. The data repair (re-classifications,
  cluster re-arrangements) is plain row data and does not break the older code
  — reverted code simply stops producing the new decisions.

## Verification performed

| Check | Result |
|---|---|
| `pytest tests` (full suite) | 299+ passed, 6 xfailed, 1 xpassed |
| Live battery `GET /test/all` | all 6 checks ok (db / embedding / llm / classify / similar / clusters incl. the new invariant check) |
| Classifier stability, second independent sample over all 91 tickets | 0 failures, 0 exceptions, system agreement 96%, kind 99%, service 76% |
| Cluster invariants in production | 0 cross-system members, 0 empty active clusters, 0 duplicate clusters |
| Intake system pinning (`affected_system` in the payload) | verified live: pins CRM / Haj correctly, an invalid value falls back to LLM resolution with a warning |
| `docker compose config` | valid |
| Container restart cycle | done twice; startup sweep runs clean, state stable afterwards |

## Monitoring (what to look at)

* `GET /test/all` — the `clusters` check now reports `N incidents, M clusters,
  K active rows` and **errors** if a cluster holds a cross-system member or an
  active row has no members. A red battery means a wrong dashboard.
* `assignment_log` — every decision is one row: `assign`, `none_fit`,
  `audit_remove`, `audit_keep`, `audit_remove_overruled` (a human pin kept over
  the audit), `system_evict`, `demoted`. `model_version='human-review'` marks
  operator/fix actions. This is the audit trail for "why did this ticket move?".
* Logs: `Flow A … vetoed by a prior audit`, `System reconcile: evicted …`,
  `Flow C … removed …`, `Flow B: group … skipped — members sit on different
  services`.

## Residual risks (with the recommended action)

1. **A ticket whose text never names the system is ambiguous** (the "reports
   page (صفحة البلاغات) is broken" family: near-identical Arabic text, one copy
   in CRM, one in Nusuk Masar Haj). Measured system agreement is 96%, and these
   four tickets are the entire disagreement set. The pipeline cannot know the
   answer because SMAX supplies no queue/system metadata.
   **WIRED (2026-09-24):** the ticketing system's own answer now flows through
   the whole chain and PINS the system — `affected_system` is part of the
   ingest contract (`POST /api/v1/incidents`, batch and `/incidents/dry-run`),
   carried into the job, passed to `classify(affected_system=…)`, honoured on
   both the cascade and the single-shot path, and the classifier still
   validates it (an unknown value falls back with a warning instead of
   failing). The SMAX connector reads it from the payload
   (`affected_system` / `affectedSystem` / `AffectedSystem` / `system`) and
   sends it on both submit paths; the local/DB source passes it through too.
   **Remaining action:** map the SMAX queue field into that payload key.
2. **SMAX truncates descriptions at exactly 200 characters** (all 91 tickets are
   ≤200 chars, 32 are exactly 200). Classification and clustering only ever see
   that much text. **Action:** request the full body field upstream; nothing in
   the classifier can recover it.
3. **Cluster granularity is an LLM judgement.** Two Rawdah problems ("no slots
   available" vs "permit issuance errors") are kept apart; a stricter reviewer
   might merge or split a few 2-member clusters. Decisions are logged per member
   and human pins are honoured, so a disagreement is a visible, reversible
   state — not silent churn.
4. **Sweep cost.** The 15-minute sweep re-runs Flow A over the unassigned pool
   (19 tickets today). Vetoed candidates short-circuit before the LLM call, but
   the pool is still re-tried every cycle — the pre-existing design
   (`REPOOL_INTERVAL`), worth revisiting only if the pool grows large.
