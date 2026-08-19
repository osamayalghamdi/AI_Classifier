# Classifier v3 — Triage, Abstention, Verification (2026-08-19)

Branch: `v3-integrate` (base `feat/classifier-v3` @ 84f33ee + foundation `43f21ba`).
Workstreams merged in dependency order: v3-store → v3-classifier → v3-review, then
payload-pinned `affected_system` + recovery/heal column-consistency fixes.

## What was built

| Piece | Where | Behavior |
|---|---|---|
| Stage 0 triage | `services/classify/classifier.py` | 1 LLM call; 7 kinds; real-data examples; truncation recovery; failure → incident (conservative) |
| Kind routing | same | incident/service_request → full cascade + verification; other kinds → system+service only (bare key, incident fields null, no verification) |
| Stage-3 abstention | same | `NONE_OF_THE_ABOVE` → `<Service>.OFFERING-GAP` sentinel + `taxonomy_gaps` row + `suggested_offering`; repair-to-first-offering DELETED |
| Honest failure | same | fallback → `incident_type/severity/urgency = null`, `classification_status='failed'`, E5 marker kept |
| Stage 4 verification | same | 1 fresh auditor call (ticket + verdict only); corrections through strict validator; invalid → discarded + logged; service correction re-runs stage 3 |
| classification_log | `shared/store.py` | every LLM decision: incident_ref, stage, prompt_version, model, raw verdict, timestamp |
| taxonomy_gaps | `shared/store.py` | count-aggregated (service, suggested_offering) + incident refs |
| Cluster input filter | `services/cluster/grouping.py` | `ticket_kind in (incident, service_request)` for clusters AND rollup |
| Recovery/heal | `services/jobs/recovery.py`, `heal.py` | key on `classification_status='failed'`; writes stay column-consistent |
| Review UI | `frontend/dashboard/review.html` + `GET /taxonomy-gaps` | gaps column next to proposals |
| Payload system pin | classifier + seams + API + import | `affected_system` from ticketing payload validated + pinned (stage-1 skipped); invalid → LLM fallback |
| Self-consistency | classifier | `CLASSIFY_SELF_CONSISTENCY` wired, default OFF |
| Migration | `scripts/migrate_classifier_v3.py` | reversible up/down (applied live) |
| Reclassify | `scripts/reclassify_v3.py` | full v3 sweep, `--dry-run`, `--limit`, `--offset` (parallel) |

## Gates

| Gate | Result |
|---|---|
| Baseline `pytest tests/ -v` (refactor/deploy-ready) | **172 passed, 4 xfailed, 3 xpassed** |
| Full suite (integrated, canary skipped) | **209+ passed, 15 skipped** (final run: see suite log) |
| Frozen canary (verbatim, no tuning) | **8 passed, 5 xfailed, 2 xpassed** — 22/22 wrong→NO strict direction intact |
| `audit_offerings.py` | **0 violations** (OFFERING-GAP sentinel whitelisted) |
| Migration up on live DB | columns `ticket_kind`, `classification_status` + tables `taxonomy_gaps`, `classification_log` |

## Reclassification of the 99 live tickets (real run, 4 parallel workers)

- 99/99 processed; **kind distribution**: incident 81, inquiry 6, test 3, administrative 3,
  service_request 2, feature_request 2, content_thin 2.
- **Spec tickets — ALL MATCH**: `x`/`y` → content_thin; `إغلاق بلاغ` ×2 → administrative;
  Final gate test / Service restructure test / Review test ticket → test;
  KPI indicator proposal → feature_request. All with `incident_type=null`.
- **Service Unavailability stored: 13 → 4** (the evaluation family broke out).
- **Taxonomy gaps surfaced (6)**: `System/Application - NMH → 'UI/Feature Visibility'` (758551b12059,
  an evaluation ticket — the abstention fired), `System/Application - NMH → (unspecified)`,
  `Istiqbal - NMH → 'Rawdha Appointment'` ×2, Invoicing → (unspecified), General → (unspecified).
- **Verifier**: 74 audits (25 correctly skipped: non-incident kinds + failed). Verdicts logged;
  sampled reasons coherent. Correction rate: low (corrections mostly null).
- **Honest failures: 2** (`505bd3dc7f81` evaluation ticket, `f06b641facf9` bare-city inquiry) —
  `classification_status='failed'`, null incident fields, marker intact. Recovery re-ran them;
  retryable.
- **classification_log: 1067 rows, 99/99 distinct incident refs** (triage 260 / stage1 255 /
  stage2 252 / stage3 103 / verification 197 — includes dry-run + sequential + parallel runs).

## Clustering before/after (offering grouping, same code path)

| | Before | After (v3 filter) |
|---|---|---|
| Clustering input | 99 incidents | **83** (99 − 16 non-incident kinds, exactly = incident 81 + service_request 2) |
| Clusters | 9 | **7** |
| Evaluation-family cluster | 9 members | **2** (remaining mislabeled) |

## Live round-trip (fresh :8001 instance, v3 code, live DB, probes deleted after)

- POST `Final gate test`-style ticket → `ticket_kind=test`, incident fields null, routed (triage+stage1+stage2
  logged, no verification), **absent from clusters** (report total 83).
- POST Rawdah-permit ticket → `incident`, `pilgrim groups and issue permit - NMH.Issue Permits`,
  confidence high — same as pre-v3; log trail: triage → stage1 → stage2 → stage3 → **verification
  verdict=approve**.

## Payload-pinned affected_system (ticketing system will send it)

`POST /classify`, import mapping, seams `Incident` and the E1 chain now accept `affected_system`.
Valid value → pinned (stage-1 resolution skipped; cascade drops to 3-4 calls); invalid → logged +
normal LLM resolution. Never invented. Tested: pin skips stage-1 LLM call, invalid falls back,
routed kinds honor the pin.

## Operational notes / findings

1. **Stale-process trap, live again**: an orphaned root `uvicorn :8000` (cwd deleted, old-era code
   with the heal loop) was re-classifying fallback-marked rows every 10 min, overwriting
   `classification_json` with v2 results while leaving the new v3 columns untouched (column/JSON
   desync on 2 rows). Killed (sudo, process was orphaned; docker container now serves :8000).
   Lesson: any old-code process that writes `classification_json` must be restarted with v3 code
   or the columns desync — recovery/heal now write columns+JSON atomically.
2. **Race on overlapping sweeps**: the sequential + parallel sweeps hit the same rows
   (non-deterministic LLM) — harmless last-writer-wins, but run parallel slices disjointly
   (`--offset`) or sequentially for reproducible results.
3. **Parallel agent**: the v2 persistent-clustering workstream (3 commits on `feat/classifier-v3`
   in the main checkout, built on 84f33ee without the v3 foundation) and this branch both touch
   `store.py`/`routes.py`/`classifier.py`/`review.html` — merge needs conflict resolution once
   both workstreams are stable; their Flow A hook in `classify_and_store` and the v3 pipeline
   coexist (hook is additive).
