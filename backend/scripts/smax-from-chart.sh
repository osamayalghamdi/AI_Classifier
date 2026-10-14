#!/usr/bin/env bash
# smax-from-chart.sh — run the SMAX connector with the chart as its config.
#
#   scripts/smax-from-chart.sh --check        # resolved config, tokens masked, no network
#   scripts/smax-from-chart.sh --show         # the same, printing what it would export
#   scripts/smax-from-chart.sh --probe <id>   # one incident: proves login + search filter
#   scripts/smax-from-chart.sh --once [--limit N]
#   scripts/smax-from-chart.sh                # normal operation (polls forever)
#
# Everything except the integration account comes from
# deploy/helm/ai-classifier/values.yaml (`smax.env`). The account — and the
# app's INTEGRATION_API_TOKEN, which must equal the server's — is read straight
# from the Kubernetes Secrets into this process's environment: it is never typed
# on a command line, never printed, never written to a file.
#
#   SMAX_USER / SMAX_PASS   <- smax.secrets.existingSecret
#   INTEGRATION_API_TOKEN   <- backend.existingSecret, exported as CLASSIFIER_API_TOKEN
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$(cd "$HERE/.." && pwd)"
VALUES="$BACKEND/../deploy/helm/ai-classifier/values.yaml"
NS="${NS:-ai-classifier}"
[ -f "$VALUES" ] || { echo "smax-from-chart: $VALUES not found" >&2; exit 1; }

show=0
[ "${1:-}" = "--show" ] && { show=1; shift; }

# Parse `smax.env` (and the secret names) out of values.yaml. A small parser on
# purpose: no yaml dependency, and it refuses a half-parsed config, because a
# silently partial export looks exactly like a working one.
parsed="$(python3 - "$VALUES" <<'PY'
import re
import sys

sections = {"env": {}, "secrets": {}}
section = None
in_smax = False
for line in open(sys.argv[1], encoding="utf-8").read().splitlines():
    if re.match(r"^smax:\s*$", line):
        in_smax = True
        continue
    if in_smax and re.match(r"^\S", line):          # next top-level block
        break
    if not in_smax:
        continue
    if re.match(r"^  (env|secrets):\s*$", line):
        section = line.strip().rstrip(":")
        continue
    m = re.match(r"^\s+([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*$", line)
    if not m or section is None:
        continue
    raw = re.sub(r"\s+#.*$", "", m.group(2)).strip().strip('"').strip("'")
    sections[section][m.group(1)] = raw

if not sections["env"]:
    sys.exit("smax-from-chart: no `smax.env` block in values.yaml")
for key in ("SMAX_BASE", "SMAX_TENANT", "CLASSIFIER_API_URL"):
    if key not in sections["env"]:
        sys.exit(f"smax-from-chart: smax.env.{key} is missing — values.yaml changed shape?")

for key, value in sorted(sections["env"].items()):
    print(f"export {key}={value!r}".replace("'", '"'))
print(f"#secret s={sections['secrets'].get('existingSecret', '')}")
for name in ("existingSecret",):
    if not sections["secrets"].get(name):
        sys.exit("smax-from-chart: smax.secrets.existingSecret is missing")
PY
)" || exit 1

exports="$(printf '%s\n' "$parsed" | grep '^export ' || true)"
smax_secret="$(printf '%s\n' "$parsed" | sed -n 's/^#secret s=//p')"
app_secret="$(python3 - "$VALUES" <<'PY'
import re, sys
for line in open(sys.argv[1], encoding="utf-8").read().splitlines():
    m = re.match(r'^\s+existingSecret:\s*"([^"]+)"', line)
    if m and "ai-classifier-db" not in m.group(1):
        print(m.group(1)); break
PY
)"

secret_export() {   # $1 = secret, $2 = key, $3 = name to export as
    local v
    v="$(kubectl -n "$NS" get secret "$1" -o jsonpath="{.data.$2}" 2>/dev/null | base64 -d 2>/dev/null || true)"
    if [ -z "$v" ]; then
        # Missing is fatal when running, and a note when only showing: --show is
        # what you run BEFORE creating the Secret.
        echo "smax-from-chart: no key $2 in secret $1 (NS=$NS)" >&2
        [ "$show" = "1" ] && { printf 'export %s=%s # MISSING\n' "$3" '""'; return 0; }
        return 1
    fi
    printf 'export %s=%q\n' "$3" "$v"
}

secrets=""
for spec in "SMAX_USER:SMAX_USER" "SMAX_PASS:SMAX_PASS"; do
    secrets+="$(secret_export "$smax_secret" "${spec%%:*}" "${spec##*:}")"$'\n' || exit 1
done
secrets+="$(secret_export "$app_secret" INTEGRATION_API_TOKEN CLASSIFIER_API_TOKEN)"$'\n' || exit 1

if [ "$show" = "1" ]; then
    printf '%s\n' "$exports"
    # Mask the real values, but keep an honest "MISSING" for a key that is not
    # there — a masked value that does not exist looks like a working setup.
    printf '%s\n' "$secrets" | sed -E '/# MISSING/! s/^(export [A-Z_]+=).*/\1"********" (from Secret)/'
    exit 0
fi

echo "smax-from-chart: config from values.yaml (smax.env), account from Secret $smax_secret" >&2
cd "$BACKEND"
# eval, not `env $(...)`: the values are already %q-quoted, and word-splitting a
# password with a space or a quote in it would silently mangle it.
eval "$exports"$'\n'"$secrets"
exec .venv/bin/python -m integrations.smax.main "$@"
