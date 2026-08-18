# Ingest Service Contract

Pipeline position: `50_api` — FastAPI HTTP layer, bulk import, service monitoring.

## routes.py — app endpoints
- `GET /health` · `GET/POST /test/llm` · `GET /test/all` (full-system battery)
- `POST /classify`, `GET /classify`, `POST /classify/batch`
- `POST /incidents/{id}/resolve`, `GET /incidents`, `GET /incidents/{id}`
- `GET /api/reports/{period}` + `GET /reports/{period}` (frontend compat), `GET /review-queue`
- `POST /import/{filename}` (.json only), `POST /import` (body), `POST /reset`
- Structured errors: 422 `INVALID_PAYLOAD` (app-level RequestValidationError handler with
  `fields` list) and `{"error": {...}}` bodies for HTTPException; CORS open.

## integration_routes.py — E1–E9 integration API
- Bearer token (`settings.integration_token`) required on every endpoint EXCEPT `/health`,
  `/ready` (E4), `/status` (liveness/readiness exempt by design); missing/empty config → 401.
- `POST /api/v1/incidents` (E1, 202 async) · `GET /api/v1/incidents/{reference}` (E2)
  · `POST /api/v1/incidents/dry-run` (E5, sync, side-effect-free) · `POST /api/v1/backfill`
  (E3, ≤200) · `GET /api/v1/jobs` · `POST /api/v1/worker/tick`
- Stable error envelope: `{"error": {"code", "message", "reference"?}}` via
  `services.jobs.integration.schemas.Err`/`error_body`.

## import_service.py — bulk intake
- File (`/import/{filename}`) or body (`/import`); DisplayLabel/Description → title/description
  via `settings.ticket_title_fields`/`ticket_description_fields`; feeds `classify_batch`.

## status_monitor.py
- Background daemon probing db/embedding/llm; LOUD logging on state change; snapshot at `GET /status`.

## Depends on
`shared.config`, `shared.store` (incl. app lifespan), `services.classify.classifier`,
`services.jobs.integration` (enqueue/get_job/worker_tick), `api.schemas` (shared payloads).
