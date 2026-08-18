#!/usr/bin/env bash
# D4 canary — validate the configured LLM (company/ELM) on the frozen
# 34-pair dev slice BEFORE any live traffic flows through it.
#
# Usage:  ./scripts/canary.sh
# Reads LLM_* from .env (explicitly, not relying on CWD-relative load_dotenv).
#
# Expected: 22/22 wrong pairs -> NO, 5/5 correct -> YES
# (8 passed, ~6 xfailed = documented flaky pairs, 1 xpassed).
# If the strict wrong->NO direction breaks: STOP, report. Do NOT tune
# prompts or thresholds to make it green.

set -euo pipefail
cd "$(dirname "$0")/.."   # repo root

# Load LLM config explicitly — never the silent ollama default.
if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

echo "==> Canary target: model=${LLM_MODEL:-<UNSET>} base=${LLM_API_BASE:-<provider default>}"
if [[ -z "${LLM_MODEL:-}" ]]; then
  echo "ERROR: LLM_MODEL is not set in .env — refusing to run canary with an implicit default." >&2
  exit 1
fi
if [[ "${LLM_MODEL}" == ollama/* ]]; then
  echo "ERROR: LLM_MODEL resolves to a local ollama model — the D4 gate requires the configured (company) endpoint." >&2
  exit 1
fi

exec uv run pytest tests/test_pairwise_canary.py -v "$@"
