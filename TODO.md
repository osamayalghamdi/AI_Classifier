# AI Incident Classifier — Phase 1 TODO

**Goal of Phase 1:** Prove the current system works on real data. Keep it simple. Don't add new features yet.

**Timeline:** 2 weeks

---

## Week 1 — Fix bugs + get real data

### Fix 3 small bugs (1 day)
- [ ] **Fix `worst_severity`** — update it when a worse incident joins a cluster (right now it's frozen at creation)
- [ ] **Fix report date filter** — count incidents by `incidents.created_at`, not `clusters.updated_at` (right now "today" can show old incidents)
- [ ] **Fix severity ranking in fallback** — use `_SEVERITY_RANK`, not alphabetical `max()` (right now "Minor" beats "Critical")

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
- LLM re-ranking for related incidents
- Fine-tuning on your 10,000 incidents
- Chatbot for employees

---

## One-line answer if the director asks "is it production-ready?"

> "It's a working prototype with the right design. Phase 1 proves accuracy on real data. After that we add auth, move to a proper database, and build a correction loop so it keeps improving. Well-understood steps, not a rebuild."
