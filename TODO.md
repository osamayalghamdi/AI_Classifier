# AI Incident Classifier — TODO

Management paused Phase 2–3 (clustering, reports, LLM re-ranking) to focus on Phase 1
(classification quality). That work is preserved untouched on the `phases-2-3` branch.
This file now tracks Phase 1 only. See [ROADMAP.md](ROADMAP.md) for full status and
the long-range enterprise plan.

---

## ✅ Phase 1 — Done

- [x] Fix `worst_severity` — updates when a worse incident joins a cluster *(lives on `phases-2-3`)*
- [x] Fix report date filter *(lives on `phases-2-3`)*
- [x] Fix severity ranking fallback *(lives on `phases-2-3`)*
- [x] Service layer extraction (`main.py` → `service.py`)
- [x] OCR support with EasyOCR
- [x] OpenRouter API support as alternative to local Ollama
- [x] Repurpose similarity search as live duplicate detection — `find_similar()` now
      only matches against `status = active` incidents
- [x] Add `active` / `resolved` incident status + `POST /incidents/{id}/resolve`
- [x] Strip clustering, reports, and LLM re-ranking off `main` (branched to `phases-2-3`)

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

---

## ☐ Phase 1 — Dedup follow-ups

Small, contained items that improve the duplicate-detection feature already shipped.
Not blocking, but cheap and directly useful to the call center using it today.

- [ ] Surface `similar_open_incidents` more prominently in the UI if the accuracy run
      shows triagers are missing it (currently a banner + list under the result)
- [ ] Consider scoping duplicate search to the same `affected_system` — currently
      global across all active incidents, which is more permissive than necessary

---

## Paused — Phase 2–3 (on `phases-2-3` branch, do not resume without confirming)

Clustering, `/reports/*`, LLM re-ranking of related incidents, and everything in
ROADMAP.md's Provisional/Needs-confirmation sections. All code and tests for this are
intact on that branch. Resume by merging or cherry-picking once management greenlights
it — don't rebuild from scratch.
