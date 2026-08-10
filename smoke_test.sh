#!/usr/bin/env bash
# Pre-demo smoke test — run against the live API.
# Each step prints PASS/FAIL so you can scan the output.
# Expected outputs are in comments beside each curl.
set -euo pipefail

API="http://localhost:8000"
DASH="http://localhost:9080"
PASS=0
FAIL=0

check() {
    local label="$1" expected="$2" actual="$3"
    if echo "$actual" | grep -q "$expected"; then
        echo "  ✅ PASS | $label"
        PASS=$((PASS + 1))
    else
        echo "  ❌ FAIL | $label"
        echo "       expected: $expected"
        echo "       got:      $(echo "$actual" | head -c 120)"
        FAIL=$((FAIL + 1))
    fi
}

echo "═══ SMOKE TEST — AI Classifier Demo ═══"
echo ""

# ── 1. Health ──
echo "── 1. Health ──"
r=$(curl -s "$API/health" --max-time 5)
check "health returns ok" '"status":"ok"' "$r"

# ── 2. List incidents ──
echo "── 2. List incidents ──"
r=$(curl -s "$API/incidents" --max-time 10)
count=$(echo "$r" | python3 -c "import sys,json; print(len(json.loads(sys.stdin.read())))" 2>/dev/null || echo "parsefail")
check "incidents list ($count)" '[0-9]' "$count"

# ── 3. Single classify (no source_ticket_id) ──
echo "── 3. Single classify (no source_ticket_id) ──"
r=$(curl -s -X POST "$API/classify" \
    -H "Content-Type: application/json" \
    -d '{"title":"Smoke test incident","description":"Testing classify endpoint"}' \
    --max-time 120)
check "classify returns incident_id" '"incident_id"' "$r"
check "classify returns classification" '"failure_mode"' "$r"

# ── 4. ID-based dedupe — same ticket ID twice → one incident ──
echo "── 4. ID-dedupe: same ticket_id twice → idempotent ──"
r1=$(curl -s -X POST "$API/classify" \
    -H "Content-Type: application/json" \
    -d '{"title":"Duplicate test","description":"Dedup test","source_ticket_id":"DEMO-001"}' \
    --max-time 120)
id1=$(echo "$r1" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('incident_id',''))" 2>/dev/null)
r2=$(curl -s -X POST "$API/classify" \
    -H "Content-Type: application/json" \
    -d '{"title":"Duplicate test","description":"Dedup test","source_ticket_id":"DEMO-001"}' \
    --max-time 120)
id2=$(echo "$r2" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('incident_id',''))" 2>/dev/null)
if [ "$id1" = "$id2" ] && [ -n "$id1" ]; then
    echo "  ✅ PASS | same ticket_id → same incident_id ($id1)"
    PASS=$((PASS + 1))
else
    echo "  ❌ FAIL | same ticket_id produced different IDs: '$id1' vs '$id2'"
    FAIL=$((FAIL + 1))
fi

# ── 5. ID-based dedupe — different IDs same text → two incidents ──
echo "── 5. ID-dedupe: different IDs same text → two incidents ──"
r3=$(curl -s -X POST "$API/classify" \
    -H "Content-Type: application/json" \
    -d '{"title":"Identical text","description":"Same description for both","source_ticket_id":"DEMO-002"}' \
    --max-time 120)
id3=$(echo "$r3" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('incident_id',''))" 2>/dev/null)
r4=$(curl -s -X POST "$API/classify" \
    -H "Content-Type: application/json" \
    -d '{"title":"Identical text","description":"Same description for both","source_ticket_id":"DEMO-003"}' \
    --max-time 120)
id4=$(echo "$r4" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('incident_id',''))" 2>/dev/null)
if [ -n "$id3" ] && [ -n "$id4" ] && [ "$id3" != "$id4" ]; then
    echo "  ✅ PASS | different IDs → different incident_ids ($id3 ≠ $id4)"
    PASS=$((PASS + 1))
else
    echo "  ❌ FAIL | different IDs produced same or empty incident_id: '$id3' vs '$id4'"
    FAIL=$((FAIL + 1))
fi

# ── 6. Reports endpoint ──
echo "── 6. Reports ──"
r=$(curl -s "$API/api/reports/daily" --max-time 15)
check "reports has total_incidents" '"total_incidents"' "$r"
check "reports has clusters array" '"clusters"' "$r"
check "reports has subsystem_summary" '"subsystem_summary"' "$r"

# ── 7. Frontend-compat alias ──
echo "── 7. Frontend-compat alias (/reports/daily) ──"
r=$(curl -s "$API/reports/daily" --max-time 15)
check "compat alias works" '"total_incidents"' "$r"

# ── 8. Dashboard HTML ──
echo "── 8. Dashboard (9080) ──"
r=$(curl -s -o /dev/null -w "%{http_code} %{size_download}" "$DASH/" --max-time 5)
check "dashboard returns HTML 200+" "200" "$r"

# ── 9. Previously imported incidents still clustered ──
echo "── 9. Previous data intact ──"
r=$(curl -s "$API/incidents?status=active" --max-time 10)
prev_count=$(echo "$r" | python3 -c "import sys,json; print(len(json.loads(sys.stdin.read())))" 2>/dev/null || echo "0")
echo "       Total incidents (active): $prev_count"
check "at least 100 incidents remain" '1[0-9][0-9]' "$prev_count"

# ── Summary ──
echo ""
echo "═══ RESULTS: $PASS passed, $FAIL failed ═══"

# Leave a clean exit code for CI
[ "$FAIL" -eq 0 ]
