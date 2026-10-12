#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# AI_Classifier — ALL ENDPOINTS as curl commands
# Edit BASE and TOKEN below, then copy-paste any command.
# Every integration (/api/v1/*) call needs the Bearer token.
# ─────────────────────────────────────────────────────────────────────────────
BASE="${BASE:-http://localhost:8000}"
TOKEN="${TOKEN:-test-token}"   # = INTEGRATION_API_TOKEN from .env

echo "BASE=$BASE  TOKEN=$TOKEN"
echo

# ── 1. Health & status ──────────────────────────────────────────────────────
echo "# 1. Health & status"
echo "curl -s $BASE/health"
echo "curl -s $BASE/ready"
echo "curl -s $BASE/status"
echo "curl -s $BASE/docs"          # Swagger UI (open in browser)
echo

# ── 2. LLM & full system test ─────────────────────────────────────────────
echo "# 2. LLM & full system test"
echo "# ONE CALL — runs the whole battery (db/embedding/llm/classify/similar/clusters):"
echo "curl -s $BASE/test/all"
echo "# /test/llm is auth-gated (spends LLM tokens):"
echo "curl -s '$BASE/test/llm?question=What%20is%20your%20name%3F&max_tokens=200' -H \"Authorization: Bearer $TOKEN\""
echo "curl -s -X POST $BASE/test/llm -H \"Authorization: Bearer $TOKEN\" -H 'Content-Type: application/json' -d '{\"question\": \"Say hello in Arabic in one line.\"}'"
echo

# ── 3. Classification ───────────────────────────────────────────────────────
echo "# 3. Classification"
echo "curl -s -X POST $BASE/classify -H 'Content-Type: application/json' -d '{\"title\": \"Rawdah permit booking fails\", \"description\": \"error on the done button\"}'"
echo "curl -s -X POST $BASE/classify/batch -H 'Content-Type: application/json' -d '{\"items\": [{\"title\": \"Rawdah permit fails\", \"description\": \"error\"}, {\"title\": \"Tax billing blocked\", \"description\": \"users blocked\"}]}'"
echo

# ── 4. Incidents ────────────────────────────────────────────────────────────
echo "# 4. Incidents"
echo "curl -s $BASE/incidents"
echo "curl -s '$BASE/incidents?status=active'"
echo "curl -s $BASE/incidents/REPLACE_WITH_ID"
echo "curl -s -X POST $BASE/incidents/REPLACE_WITH_ID/resolve"
echo

# ── 5. Reports & clusters ───────────────────────────────────────────────────
echo "# 5. Reports & clusters"
echo "curl -s $BASE/api/reports/daily"
echo "curl -s $BASE/reports/daily"
echo

# ── 6. Data management ──────────────────────────────────────────────────────
echo "# 6. Data management"
echo "curl -s -X POST $BASE/import -H 'Content-Type: application/json' -d '{\"incidents\": [{\"title\": \"Example\", \"description\": \"example\"}]}'"
echo "# CAREFUL — deletes everything (auth-gated):"
echo "curl -s -X POST $BASE/reset -H \"Authorization: Bearer $TOKEN\""
echo

# ── 7. Integration API (auth) ───────────────────────────────────────────────
echo "# 7. Integration API (Bearer token)"
echo "curl -s -X POST $BASE/api/v1/incidents -H \"Authorization: Bearer $TOKEN\" -H 'Content-Type: application/json' -d '{\"source_reference\": \"TKT-1001\", \"title\": \"Rawdah permit fails\", \"description\": \"error on done button\"}'"
echo "curl -s $BASE/api/v1/incidents/TKT-1001 -H \"Authorization: Bearer $TOKEN\""
echo "curl -s -X POST $BASE/api/v1/incidents/dry-run -H \"Authorization: Bearer $TOKEN\" -H 'Content-Type: application/json' -d '{\"source_reference\": \"DRY-1\", \"title\": \"Dry run\", \"description\": \"persists nothing\"}'"
echo "curl -s -X POST $BASE/api/v1/backfill -H \"Authorization: Bearer $TOKEN\" -H 'Content-Type: application/json' -d '{\"incidents\": [{\"source_reference\": \"BF-1\", \"title\": \"Backfill one\", \"description\": \"desc\"}]}'"
echo "curl -s $BASE/api/v1/jobs -H \"Authorization: Bearer $TOKEN\""
echo "curl -s -X POST '$BASE/api/v1/worker/tick?limit=10' -H \"Authorization: Bearer $TOKEN\""
echo "# Expect 401 without the token:"
echo "curl -s $BASE/api/v1/incidents/TKT-1001"
