#!/usr/bin/env bash
# Export the chart's environment for a HOST-side run of the API.
#
# There is no .env in this project: the configuration lives in
# deploy/helm/ai-classifier/values.yaml (`backend.env` + `database.*`). This
# script reads those values and emits shell exports, so a host run and a pod
# run share one source — nothing to keep in sync by hand.
#
#   source scripts/env-from-chart.sh        # into the current shell
#   ./scripts/env-from-chart.sh --show      # print, export nothing
#   eval "$(./scripts/env-from-chart.sh)"   # inside another script
#
# Secrets are NOT here (they live in the K8s Secret). To get them into a shell
# without printing them, go through a file you delete immediately:
#   kubectl -n ai-classifier get secret ai-classifier-secrets \
#     -o go-template='{{range $k,$v := .data}}{{printf "export %s=%s\n" $k ($v|base64decode)}}{{end}}' > /tmp/ai-secrets.sh
#   source /tmp/ai-secrets.sh && rm -f /tmp/ai-secrets.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VALUES="$HERE/../../deploy/helm/ai-classifier/values.yaml"
[ -f "$VALUES" ] || { echo "env-from-chart: $VALUES not found" >&2; exit 1; }

# A small parser on purpose: no yaml dependency, and it refuses to emit a
# half-parsed config (a silent partial export is worse than an error).
exports="$(python3 - "$VALUES" <<'PY'
import re
import sys

env: dict[str, str] = {}
db: dict[str, str] = {}
section = None

for line in open(sys.argv[1], encoding="utf-8").read().splitlines():
    if re.match(r"^  env:\s*$", line):
        section = "env"
        continue
    if re.match(r"^database:\s*$", line):
        section = "db"
        continue
    if re.match(r"^\S", line):                     # next top-level block
        section = None
        continue
    m = re.match(r"^(\s+)([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*$", line)
    if not m or section is None:
        continue
    indent, key, raw = len(m.group(1)), m.group(2), m.group(3)
    raw = re.sub(r"\s+#.*$", "", raw).strip().strip('"').strip("'")
    if section == "env" and indent >= 4:
        env[key] = raw
    elif section == "db" and indent == 2:
        db[key] = raw

if len(env) < 20:
    sys.exit(f"env-from-chart: parsed only {len(env)} env values — values.yaml changed shape?")

for key, name in (("host", "PG_HOST"), ("port", "PG_PORT"),
                  ("name", "PG_DATABASE"), ("user", "PG_USER")):
    if db.get(key):
        env[name] = db[key]

for key, value in sorted(env.items()):
    print(f"export {key}={value!r}".replace("'", '"'))
PY
)"

if [ "${1:-}" = "--show" ]; then
    printf '%s\n' "$exports"
else
    # shellcheck disable=SC1090
    eval "$exports"
    printf 'env-from-chart: exported %s values from deploy/helm/ai-classifier/values.yaml\n' \
        "$(printf '%s\n' "$exports" | grep -c '^export ')" >&2
fi
