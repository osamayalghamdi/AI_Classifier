# STATUS.md — feat/suboffering-clustering

Branch: `feat/suboffering-clustering` (base `aca06a3`, = demo-ready, which is UNTOUCHABLE)
Manager: Agent M. Updated by manager only.

## Timeline / gates

| Phase | Worker | Branch | Status | Gate numbers |
|---|---|---|---|---|
| P1 CLEAN | W1 | w1-clean → merged `885b0b3` | ✅ DONE | C1 70/70 · C2 14 endpoints identical · C3 3 deletions 3-way evidence · C4 pure moves · C5 suspect-kept delivered |
| P2-prep canary | W2-prep | w2-prep → merged `855ec3a` | ✅ DONE | Canary: 27 strict (22 wrong→NO + 5 correct) + 7 known-flaky (transport-approval ×4, reports-page ×3) |
| P2 BUILD (engine) | W2 | w2-engine (wt_w2b) | 🔄 IN PROGRESS | B1–B5: pending |
| P2 BUILD (offering-000) | W3 | w3-offering000 | ⏸ after W2 engine exists | F1–F4: pending |

## Key decisions
- Canary flaky set accepted (manager decision a): 7 pairs xfail with documented reasons; wrong→NO stays hard (22/22). Graduation rule: 3 consecutive passes → back to strict.
- C2 "diff empty" interpreted honestly as change-set ⊆ noise floor (LLM nondeterminism), all 14 status codes identical.
- Phase-2 embeddings+validator path in grouping.py: NOT dead code (live callers) — kept, flagged as P2-design decision.

## Frozen parameters (verbatim, do not re-tune)
- Verifier prompt v3: same failing ACTION + same SERVICE for YES; category overlap insufficient; verdict + one-line reason. (STRICT_PROMPT_V3 in tests/test_pairwise_canary.py)
- Embeddings: bge-m3 on pure `title + "\n" + description` ONLY.
- floor 0.40, top_n 10, tie-break (sim DESC, id ASC), auto-accept ≥0.90 cap-exempt.
- Purity floor: mean sim <0.45 OR >6 known codes → NEEDS_REVIEW.
- Oversize guard: >20 members → re-verify weakest 25% edges.
- Drift: max_tokens=2000, chunk >25, retry-once-then-FLAG.
- Cache key includes prompt_version.
- LLM: OpenRouter qwen3.6 temp 0.0. ENV LESSON: export LLM_MODEL/LLM_API_KEY from repo .env before scratch runs (load_dotenv CWD-relative → silent Ollama fallback).
- W2 finding (empirical): prompt v3 recall-instability class = same-service/different-action (transport-approval + reports-page families). Human review gate MANDATORY for those — never auto-approve.

## Blocked / decisions needed
- (none currently)

## Notes
- Offering distribution (92-ticket dataset, first segment of service): pilgrim groups 22, System/Application 14, inquiry 10, contracts 8, Between cities 8, suggestion 7, Registration 3, camps 2, Financial 2, OFFERING-000 = 7. Three offerings would trigger N=10 batch immediately.
- Canary maintenance: PAIRWISE_CANARY_SKIP=1 disables; graduation needs 3 consecutive passes.
