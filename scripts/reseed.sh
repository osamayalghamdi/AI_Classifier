#!/bin/bash
# Re-seed the incident store from a JSON ticket file.
#
# Usage:
#   scripts/reseed.sh [API_BASE] [tickets.json]
#
# Flow: POST /reset (wipe the store) -> POST /import (classify + embed each
# ticket via the LLM configured in the environment) -> print counts.
# The import wall-time is printed — embedding is the dominant local cost.
set -euo pipefail

API="${1:-http://localhost:8000}"
FILE="${2:-test_incidents.json}"

if [ ! -f "$FILE" ]; then
    echo "FATAL: tickets file not found: $FILE" >&2
    exit 1
fi

echo "==> Re-seed target: $API"
echo "==> Tickets file:  $FILE ($(python3 -c "import json,sys; print(len(json.load(open('$FILE'))))") tickets)"

echo "==> Wiping store: POST $API/reset"
curl -s -X POST "$API/reset"
echo

echo "==> Importing ..."
START=$(date +%s)
python3 -c "import json,sys; print(json.dumps({'incidents': json.load(open('$FILE'))}))" \
    | curl -s -X POST "$API/import" \
        -H "Content-Type: application/json" \
        --data-binary @- > /tmp/reseed_import_result.json
END=$(date +%s)
echo "==> Import wall time: $((END - START))s"
python3 -c "
import json
d = json.load(open('/tmp/reseed_import_result.json'))
print('import result:', {k: v for k, v in d.items() if k != 'items'})
"

echo "==> Counts after re-seed:"
curl -s "$API/incidents" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('incidents:', len(d))
"
