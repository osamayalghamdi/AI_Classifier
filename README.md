# AI Incident Classifier

<img width="2600" height="1235" alt="architecture-flow" src="https://github.com/user-attachments/assets/7a7826db-2b64-4f22-8de9-6c2108c67a12" />

An **incident classification + grouping system** for customer support call centers. Bilingual Arabic/English. Feed it a support ticket, get back a structured classification, similar open incidents, and a grouped view of what's actually happening.

> **One shift lead, one dashboard, ten seconds** — see what's on fire, what's recurring, and fix one root cause instead of touching 20 tickets.

---

## Architecture

```
Ticket → LLM classifies → Embedding → Similarity search → Cluster → Dashboard
         (1 call/ticket)   (bge-m3)   (pgvector/SQLite)   (graph)  (3 lenses)
```

| Layer | Tech | Role |
|-------|------|------|
| API | FastAPI + Uvicorn | REST endpoints |
| LLM | Qwen2.5:7b via Ollama (swappable via LiteLLM) | Classification + group validation |
| Embeddings | all-MiniLM-L6-v2 / bge-m3 | Cosine similarity for duplicates |
| Storage | SQLite (→ pgvector planned) | Incidents + vectors |
| Frontend | Vanilla JS (no build step) | 3-role dashboard |

**Key design:** LLM does 2 things only — classify every ticket and validate proposed groups. Everything else (duplicate search, clustering) is math. No LLM calls on page refresh.

---

## Dashboard — Three Lenses

One API (`/api/clusters?scope=me|team|all`), three views:

| Role | Question | Default scope |
|------|----------|--------------|
| **Agent** | "What are my 50 tickets?" | Their assign_group, grouped by root cause |
| **Shift Lead** | "What's on fire for my team?" | Team clusters ranked by severity × growth |
| **Manager** | "How are we doing this week?" | Cross-team trends, recurring patterns, deflection |

Key features in the employee view:
- **Group filter** — My Group / All / specific team
- **Status split** — each cluster expands into Active / Escalated / Pending / Third-party / Verify / Resolved sections, each with colored left border
- **Grouped/Flat toggle** — cluster view or raw ticket list
- **Search + severity filter** — find tickets fast
- **Shared cluster badges** — gold tag when a root cause spans multiple teams
- **Simulated clustering timer** — shows last refresh, auto-refreshes every 30s

---

## Project Structure

```
AI_Classifier/
├── ai_classification/              # Backend (FastAPI + LLM)
│   ├── main.py                     # Entry point
│   ├── config.py                   # Env settings
│   ├── sync.py                     # Ticketing sync worker
│   ├── api/   routes.py, schemas.py
│   ├── core/  classifier.py, store.py, grouping.py
│   └── domain/ models.py, taxonomy.py
│
├── frontend/
│   ├── index.html                  # Classify form (port 8082)
│   └── dashboard/                  # Three-lens dashboard
│       ├── index.html              # UI shell (dark, bilingual, RTL-ready)
│       ├── app.js                  # Logic: roles, filters, clustering
│       └── data.js                 # Mock data: 150+ tickets, 16 clusters
│
├── ocr/                            # EasyOCR microservice (AR/EN)
├── ticketing_simulator/            # Mock ticketing API (276 bilingual tickets)
├── tests/                          # 60 unit + e2e tests
│
├── NOTES.md                        # Design review (authoritative — read this first)
├── ROADMAP.md                      # Enterprise plan
├── pyproject.toml                  # Python deps
└── docker-compose.yml              # Full stack
```

---

## Quick Start

```bash
# Backend
cd ai_classification && pip install -e . && uvicorn main:app --reload --port 8000

# Dashboard (standalone, no build)
cd frontend/dashboard && python3 -m http.server 8085

# Open
open http://localhost:8085
```

The dashboard runs in **mock mode** by default — 150+ tickets, 16 clusters, 4 teams. Click "source: mock" in the top bar to switch to live mode (requires backend).

---

## Status Lifecycle

```
Active → Escalated → Third-party → Verify → Resolved
         ↑               ↑
         Needs help      Waiting on external
```

Each status gets its own colored section in the cluster card. New statuses can be added by extending `STATUS_COLORS` in `app.js`.

---

## Key Files

| File | What |
|------|------|
| `frontend/dashboard/index.html` | Dashboard UI shell |
| `frontend/dashboard/app.js` | All logic: roles, filters, status colors, rendering |
| `frontend/dashboard/data.js` | Mock data generator (deterministic seed) |
| `ai_classification/core/classifier.py` | LLM classification prompt + retry |
| `ai_classification/core/grouping.py` | Graph clustering + LLM validator |
| `NOTES.md` | Full design rationale (start here) |
