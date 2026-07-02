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

## 🚧 Phase 1 — Blocking gate (do this next, in order)

Nothing below this section should get real investment until this comes back. It's the
one number that tells you whether the rest of the roadmap is worth building.

- [ ] Export 100–200 real historical incidents to JSON
- [ ] Run classifier on real data, compare predictions to existing labels
- [ ] Measure accuracy per field (system, severity, type)
- [ ] Check Arabic vs English accuracy separately
- [ ] Write a 1-page summary, decide with manager: 80%+ good enough to continue?

**Minimal logging to support this run** — not a full structured-logging overhaul, just
enough to debug the accuracy pass and see where predictions go wrong:
- [ ] Every classify call: title, model used, latency, success/failure
- [ ] Every LLM call: retry attempts, parse failures

(Full structured logging — token counts, clustering events, log-level config via
env — belongs in Phase 2, once there's an actual production traffic pattern to log.)

---

## ☐ Phase 2 — Provisional (revisit after the accuracy numbers land)

Everything from here down is a reasonable idea, not a committed plan. If accuracy comes
back weak, most of this gets re-scoped or dropped.

### Performance

| Priority | Item | Details |
|---|---|---|
| High | **Fast local mode** | `FAST_CLASSIFY=1` uses local Ollama 7B for instant feedback, 35B for batch runs |
| High | **Async classify** | POST returns immediately with "processing", LLM runs in background, frontend polls |
| Medium | **Embedding-only mode** | `CLASSIFY_MODE=embedding` — skip LLM entirely, return top-N cosine matches (zero LLM latency) |
| Low | **Cache recent classifications** | Short TTL cache for identical title+description |
| Low | **Lazy summarization** | Don't re-summarize clusters on every classify, do it on a schedule |

### OCR

- [ ] **Return structured text** — preserve line breaks, tag `[Arabic]` / `[English]` per block
- [ ] **Arabic OCR quality** — benchmark easyocr vs surya-ocr on Arabic screenshots
- [ ] **Auto-detect language** — don't hardcode, detect from the image
- [ ] **Confidence score** — return confidence per line so LLM can weigh unreliable text lower

### Enriched incident schema

- [ ] **Supersedes / duplicates** — link incidents that supersede or duplicate each other
- [ ] **Solution / resolution** — `resolved_at` + resolution text
- [ ] **Escalations** — which team? at what level?
- [ ] **Assignee / owner** — who was assigned
- [ ] **Tickets / tasks** — links to Jira or related tasks
- [ ] **Comments / timeline** — key events during the incident lifecycle

### Reports

- [ ] **Order clusters by date** — incidents sorted chronologically within each report
- [ ] **Report templates** — daily / weekly / monthly with configurable grouping
- [ ] **Dynamic accuracy in reports** — reports show which classifications were refined since last run ("3 incidents re-classified from Major → Critical")

---

## ❓ Needs confirmation before building — not yet scoped as real work

These aren't rejected, just unproven. Don't start either without confirming the
underlying need first — they're sized like their own mini-projects, not sub-bullets.

- **Re-classification of active incidents** — background worker re-classifies every
  active incident every N minutes with accumulated context (past classification +
  new related incidents), tracks re-classification history, re-forms clusters on the
  refined labels, weights recent incidents higher in similarity, shows "re-classified
  5 min ago" in the UI. Real feature, real scope (scheduler, history table, cluster
  re-forming logic). **Is there an observed case where incidents are actually
  misclassified early and corrected later, or is this speculative?** If nobody's hit
  that problem yet, it can wait.
- **Weighted feature control** (`FEATURE_WEIGHT_TITLE=2.0`, etc.) — configurable
  per-field weighting for similarity + classification. Premature without accuracy
  data showing *which* field is actually dragging predictions down. Add this only
  after Phase 1 measurement identifies a specific field that needs it — don't build
  the knob before you know which way to turn it.

---

## Future

| Item | When |
|---|---|
| Auth + rate limiting | Before real users |
| SQLite → PostgreSQL | When you need HA or concurrent writers |
| Vector index (FAISS / pgvector) | Past a few thousand live incidents |
| Human correction feedback loop | After Phase 1 validation |
| Fine-tuning on your 10,000 incidents | After you have labeled data |
| Chatbot for employees | Later |

See [ROADMAP.md](ROADMAP.md) for the full enterprise plan (architecture, security,
data layer, reliability, human-in-the-loop, UX) — this table is the short version.
