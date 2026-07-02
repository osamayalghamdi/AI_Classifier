# AI Incident Classifier — Phase 1 TODO

**Goal of Phase 1:** Prove the current system works on real data. Keep it simple. Don't add new features yet.

**Timeline:** 2 weeks

---

## Week 1 — Fix bugs + get real data

### Fix 3 small bugs (1 day)
- [x] **Fix `worst_severity`** — update it when a worse incident joins a cluster (right now it's frozen at creation)
- [x] **Fix report date filter** — count incidents by `incidents.created_at`, not `clusters.updated_at` (right now "today" can show old incidents)
- [x] **Fix severity ranking in fallback** — use `SEVERITY_RANK`, not alphabetical `max()` (right now "Minor" beats "Critical")

*These are fast. You can fix them in a day with Claude Code.*

### Get real incidents (2–3 days)
- [ ] Export 100–200 real historical incidents (they already have categories + solutions)
- [ ] Save them to a file (JSON or CSV)
- [ ] Make sure each has: title, description, system, and the existing label

---

## Week 2 — Test accuracy

### Run the model (2 days)
- [ ] Run the classifier on all the real incidents
- [ ] Test with **Qwen 35B** (the production model) — not the laptop 7B
- [ ] Save what the AI predicted vs. the existing label

### Measure (2 days)
- [ ] Count how many the AI got right (per field: system, severity, type)
- [ ] Note which fields are strong, which are weak
- [ ] Check Arabic incidents separately (do they classify worse than English?)

### Report (1 day)
- [ ] Write a 1-page summary: "AI got X% right. Strong: [...]. Weak: [...]."
- [ ] Decide with manager: good enough to continue? (target: 80%+)

---

## What you need from the manager / infra team
- [ ] Access to the historical incidents (the 10,000 you have)
- [ ] Access to test Qwen 35B (rent an A100 GPU, or pay-per-call API for testing)

---

## NOT in Phase 1 (save for later)
These are real, but **don't touch them yet** — they're Phase 2+:
- Vector index for scale (FAISS / pgvector) — only matters past a few thousand live incidents
- Auth + rate limiting — needed before real users, not for testing
- Move SQLite → Postgres
- Human correction feedback loop
- Fine-tuning on your 10,000 incidents
- Chatbot for employees

---

## Phase 2 — LLM Re-ranking for Related Incidents

### What & Why
Right now `find_similar()` uses cosine similarity on embeddings to return the top 5 related incidents. This is fast but purely mathematical — it misses semantic nuance like the same root cause described in different words, or Arabic vs. English equivalents of the same incident.

The idea: keep the embedding step as a fast pre-filter to grab a candidate pool (up to 100), then let the LLM read those candidates and pick the truly most similar ones. The LLM output maps back to the exact same `list[SimilarMatch]` format — nothing else in the pipeline changes.

### Two-stage pipeline

```
new incident
     │
     ▼
[Stage 1 — embedding pre-filter]
  cosine similarity (existing code, no threshold)
  → top 100 candidates  (fast, no LLM)
     │
     ▼
[Stage 2 — LLM re-rank]
  pass: new incident + 100 candidates (id, title, description, classification)
  LLM returns: top 5 {incident_id, similarity_percentage, reasoning}
     │
     ▼
list[SimilarMatch]  ← same format as today
```

### Implementation plan

- [ ] **`config.py`** — add two settings:
  - `use_llm_reranking: bool = bool(getenv("USE_LLM_RERANKING", ""))` (off by default)
  - `llm_rerank_candidates: int = int(getenv("LLM_RERANK_CANDIDATES", "100"))`

- [ ] **`incident_store.py`** — add `find_candidates(text, *, top_n=100) -> list[dict]`:
  - Same cosine loop as `find_similar()` but **no threshold**, just top-N
  - Returns richer dicts: `{id, title, description, classification_json, cosine_score}`

- [ ] **`classifier.py`** — add `llm_rerank_similar(query_title, query_description, candidates) -> list[dict]`:
  - Builds a prompt: new incident details + numbered list of candidates
  - LLM must return JSON array: `[{"id": "...", "similarity": 87, "reasoning": "..."}, ...]`
  - Parse + validate the response (handle markdown fences, retry once on parse failure)
  - Returns at most 5 entries, similarity as integer percentage (0–100)

- [ ] **`incident_store.py`** — add `find_similar_llm_reranked(text, *, extracted_text="", classification=None) -> list[SimilarMatch]`:
  - Calls `find_candidates()` to get up to 100
  - Calls `llm_rerank_similar()` from classifier
  - Looks up each returned ID to get `title` and `ClassificationResult`
  - Converts percentage → float (e.g. 87 → 0.87) and returns `list[SimilarMatch]`
  - Falls back to empty list if LLM fails (never crashes the classify flow)

- [ ] **`main.py`** — in `_classify_and_store()`, swap in the new method when the flag is on:
  ```python
  if settings.use_llm_reranking:
      matches = store.find_similar_llm_reranked(text, ...)
  else:
      matches = store.find_similar(text, ...)
  ```

### LLM prompt sketch

**System:**
```
You are an incident similarity analyst.
Given a new incident and a numbered list of historical incidents,
identify the top 5 most semantically similar ones.

Return ONLY a JSON array, no extra text:
[
  {"id": "<incident_id>", "similarity": <0-100>, "reasoning": "<one line>"},
  ...
]
Rank by similarity descending. Return at most 5 items.
```

**User:**
```
New incident:
  Title: <title>
  Description: <description>
  System: <affected_system> / <service>

Historical incidents:
1. [id=abc123] Title: "..." | System: ... | Description: "..."
2. [id=def456] ...
...
```

### Key constraints
- Max 100 candidates to LLM — keeps prompt under ~12k tokens even for long descriptions
- Similarity returned as integer 0–100 from LLM, converted to float 0.0–1.0 in `SimilarMatch`
- Feature flag `USE_LLM_RERANKING=1` controls it; default is off (existing cosine path unchanged)
- The entire stage-2 is wrapped in try/except — any LLM failure returns empty list (classify still succeeds)
- Do NOT change `find_similar()` or anything that calls it — this is purely additive

---

## One-line answer if the director asks "is it production-ready?"

> "It's a working prototype with the right design. Phase 1 proves accuracy on real data. After that we add auth, move to a proper database, and build a correction loop so it keeps improving. Well-understood steps, not a rebuild."
