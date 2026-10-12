#!/usr/bin/env bash
# Send a test ticket into the system by hand — no SMAX, no connector, and no
# token needed when the server runs with AUTH_OPTIONAL=1.
#
#   scripts/send_test_ticket.sh                                  # quick generated ticket
#   scripts/send_test_ticket.sh "RAWDHA ERROR" "sensor 12 failed" # your own text
#   SYNC=1 scripts/send_test_ticket.sh                            # /classify (waits for the result)
#   API_URL=http://other-host:8000 scripts/send_test_ticket.sh
#
# Env: API_URL (default http://127.0.0.1:8000), INTEGRATION_API_TOKEN (optional),
#      SYNC=1 to use the synchronous /classify path instead of async ingest.
set -euo pipefail

API_URL="${API_URL:-http://127.0.0.1:8000}"
TITLE="${1:-Manual test ticket $(date +%H:%M:%S)}"
DESC="${2:-Sent by hand with scripts/send_test_ticket.sh to check ingest while SMAX is not connected.}"
REF="MANUAL-$(date +%s)"

AUTH=()
if [ -n "${INTEGRATION_API_TOKEN:-}" ]; then
  AUTH=(-H "Authorization: Bearer ${INTEGRATION_API_TOKEN}")
fi

json() { python3 -c 'import json,sys; print(json.dumps(dict(zip(sys.argv[1::2], sys.argv[2::2]))))' "$@"; }

if [ "${SYNC:-0}" = "1" ]; then
  echo "→ POST ${API_URL}/classify   (synchronous — returns the classification)"
  curl -sS -o /tmp/ticket_reply.json -w '  HTTP %{http_code}\n' \
    -X POST "${API_URL}/classify" "${AUTH[@]}" -H 'Content-Type: application/json' \
    -d "$(json title "$TITLE" description "$DESC")"
else
  echo "→ POST ${API_URL}/api/v1/incidents   (async ingest — reference ${REF})"
  curl -sS -o /tmp/ticket_reply.json -w '  HTTP %{http_code}\n' \
    -X POST "${API_URL}/api/v1/incidents" "${AUTH[@]}" -H 'Content-Type: application/json' \
    -d "$(json source_reference "$REF" title "$TITLE" description "$DESC")"
  echo "  look it up:  curl -s ${API_URL}/api/v1/incidents/${REF}"
  echo "  (the background worker picks it up — INTEGRATION_WORKER_ENABLED=1)"
fi

python3 -c 'import json;print("  reply:", json.dumps(json.load(open("/tmp/ticket_reply.json")))[:400])' 2>/dev/null \
  || sed 's/^/  reply: /' /tmp/ticket_reply.json
