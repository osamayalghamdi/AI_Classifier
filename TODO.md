# AI Incident Classifier — TODO

---

## ✅ Phase 1 — Done

- [x] Fix `worst_severity` — updates when a worse incident joins a cluster
- [x] Fix report date filter — counts by `incidents.created_at`, not `clusters.updated_at`
- [x] Fix severity ranking fallback — uses `SEVERITY_RANK`, not alphabetical `max()`
- [x] Service layer extraction (`main.py` → `service.py`)
- [x] LLM re-ranking for related incidents (embedding pre-filter → LLM picks top 5)
- [x] OCR support with EasyOCR
- [x] OpenRouter API support as alternative to local Ollama

---

## ☐ Phase 1 — Still needed

- [ ] **Add proper logging** — structured logging so you can see what's happening:
  - Every classify call: title, model used, latency, success/failure
  - Every LLM call: prompt length, token count, retry attempts
  - Clustering events: new cluster created, incident added to existing cluster
  - Startup: model loaded, DB connected, embeddings ready
  - Currently there are scattered `_log.debug()` and `print()` calls — unify everything under a single structured logger with consistent format + log level config via env
- [ ] Export 100–200 real historical incidents to JSON
- [ ] Run classifier on real data, compare predictions to existing labels
- [ ] Measure accuracy per field (system, severity, type)
- [ ] Check Arabic vs English accuracy separately

---

## ☐ Phase 2 — Performance

| Priority | Item | Details |
|---|---|---|
| High | **Fast local mode** | `FAST_CLASSIFY=1` uses local Ollama 7B for instant feedback, 35B for batch runs |
| High | **Async classify** | POST returns immediately with "processing", LLM runs in background, frontend polls |
| Medium | **Embedding-only mode** | `CLASSIFY_MODE=embedding` — skip LLM entirely, return top-N cosine matches (zero LLM latency) |
| Low | **Cache recent classifications** | Short TTL cache for identical title+description |
| Low | **Lazy summarization** | Don't re-summarize clusters on every classify, do it on a schedule |

---

## ☐ Phase 2 — Re-classification of active incidents

Every active incident gets re-classified periodically (e.g. every 30 min) so the system improves its view over time as more context accumulates.

- [ ] **Re-classify with history** — when re-classifying, pass the incident's past classifications + all related incidents as context. The LLM sees: "This was classified as X before. Since then, Y similar incidents appeared. Should it still be X?"
- [ ] **Schedule via cron** — `RECLASSIFY_INTERVAL_MINUTES=30` env var. Background worker re-classifies active incidents on schedule.
- [ ] **Track re-classification history** — store each re-classification result + timestamp, keep a running log per incident (not just overwrite)
- [ ] **Report accuracy improves over time** — reports use the latest re-classification, not the first one. Clusters re-form based on the refined labels
- [ ] **Weight recent incidents more** — newer incidents get higher weight in cluster similarity so that "the last 30 min" reflects current reality better
- [ ] **UI hint** — show "Re-classified 5 min ago" next to each incident so users see it's alive

---

## ☐ Phase 2 — OCR

- [ ] **Return structured text** — preserve line breaks, tag `[Arabic]` / `[English]` per block
- [ ] **Arabic OCR quality** — benchmark easyocr vs surya-ocr on Arabic screenshots
- [ ] **Auto-detect language** — don't hardcode, detect from the image
- [ ] **Confidence score** — return confidence per line so LLM can weigh unreliable text lower

---

## ☐ Phase 2 — Enriched incident schema

- [ ] **Supersedes / duplicates** — link incidents that supersede or duplicate each other
- [ ] **Solution / resolution** — `resolved_at` + resolution text
- [ ] **Escalations** — which team? at what level?
- [ ] **Assignee / owner** — who was assigned
- [ ] **Tickets / tasks** — links to Jira or related tasks
- [ ] **Comments / timeline** — key events during the incident lifecycle
- [ ] **Weighted feature control** — env vars to control how much each field weighs in similarity + classification:

```
FEATURE_WEIGHT_TITLE=2.0        # title is 2x more important
FEATURE_WEIGHT_SOLUTION=0.5     # solution text matters less
FEATURE_WEIGHT_ASSIGNEE=0       # ignore assignee entirely
```

---

## ☐ Phase 2 — Reports

- [ ] **Order clusters by date** — incidents sorted chronologically within each report
- [ ] **Report templates** — daily / weekly / monthly with configurable grouping
- [ ] **Dynamic accuracy in reports** — reports show which classifications were refined since last run ("3 incidents re-classified from Major → Critical")

---

## ☐ Future

| Item | When |
|---|---|
| Auth + rate limiting | Before real users |
| SQLite → PostgreSQL | When you need HA or concurrent writers |
| Vector index (FAISS / pgvector) | Past a few thousand live incidents |
| Human correction feedback loop | After Phase 1 validation |
| Fine-tuning on your 10,000 incidents | After you have labeled data |
| Chatbot for employees | Later |
