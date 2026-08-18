# Classify Service Contract

Pipeline position: `20_classify` — LLM classification + persist orchestration.

## Entry points (public API)
- `classify(title, description) -> ClassificationResult` — never raises; degrades to
  a low-confidence generic fallback after LLM failure.
- `classify_and_store(...) -> ClassifyResponse` — classify + dedupe + persist.
- `classify_batch(incidents) -> ClassifyBatchResponse`, `content_hash(...)`.
- `PROMPT_VERSION = "2026-08-v2"` — identity of the frozen system prompt, recorded on
  persisted classifications by the seams pipeline (provenance). Bump when prompt text changes.

## Classification cascade (settings.cascade_classification, default true)
Coarse-to-fine, each stage returns the FULL ClassificationResult JSON with only that
stage's field constrained to a short option list:
1. **System** — deterministic keyword resolution first (0 LLM calls); LLM fallback over
   the 4 `AffectedSystem` values; lenient stage-1-only parse (provisional service coerced,
   truncated-JSON recovery via regex).
2. **Service** — LLM over ONLY the resolved system's service list.
3. **Offering** — LLM over ONLY the service's offerings, copied verbatim; one retry with
   the validation error; deterministic repair to the first valid offering (confidence low).
   Empty/single-offering lists skip the LLM call.

## Frozen prompts & taxonomy validator
- `_SYSTEM_PROMPT` (legacy single-shot) built once at import; `_CASCADE_JSON_SCHEMA` is the
  shared stage contract; `FEW_SHOT_EXAMPLES` are frozen. No prompt text is edited at runtime.
- Strict validation via `ClassificationResult` (pydantic): service must belong to the chosen
  system (`_check_service_in_system`); failure_mode must be a real FM-XXX code (FM-000 = none).

## Depends on
`shared.config` (cascade gate, llm_model), `shared.store` (dedupe/persist), `domain.models`,
`domain.taxonomy`, `api.schemas`, `core.failure_modes` (FM taxonomy).
