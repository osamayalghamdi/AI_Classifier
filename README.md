# AI Incident Classifier

<img width="2600" height="1235" alt="architecture-flow" src="https://github.com/user-attachments/assets/7a7826db-2b64-4f22-8de9-6c2108c67a12" />

LLM-powered incident classification with duplicate detection and graph-based grouping. Submit a ticket, get back structured labels + similar open incidents + cluster context.

---

## Stack

| Layer | Tech |
|-------|------|
| API | FastAPI + Uvicorn |
| LLM | LiteLLM (provider-agnostic) — current: Qwen3.6-35B-A3B via OpenRouter (swappable to local Qwen2.5:7b via Ollama) |
| Embeddings | sentence-transformers (all-MiniLM-L6-v2 / bge-m3) |
| Storage | PostgreSQL + pgvector (HNSW ANN index) |
| OCR | EasyOCR (English + Arabic) |
| Frontend | Vanilla JS (no build step) |
| Sync | Background thread polls external ticketing API |

---

## API

| Method | Path | Description |
|--------|------|-------------|
| POST | `/classify` | Classify + find duplicates |
| GET | `/classify` | Same via query params |
| POST | `/classify/batch` | Batch classify |
| GET | `/incidents` | List incidents (?status=active) |
| GET | `/incidents/{id}` | Get one |
| POST | `/incidents/{id}/resolve` | Mark resolved |
| GET | `/api/reports/{period}` | Clustered report |
| GET | `/health` | Service status |
| POST | `/reset` | Delete all |

---

## Dashboard — Three Lenses

Live at `frontend/dashboard/` (standalone, `python3 -m http.server 8085`).

| Role | Question | Default scope |
|------|----------|--------------|
| **Engineer** | "What are my tickets?" | Their assign_group, grouped by root cause |
| **Shift Lead** | "What's on fire for my team?" | Team clusters ranked by severity × growth |
| **Manager** | "How are we doing?" | Cross-team trends, recurring patterns, deflection |

Features:
- **Group filter** — My Group / All / specific team
- **Status-split clusters** — each cluster splits into Active / Escalated / Pending / Third-party / Verify / Resolved sections with colored borders
- **Grouped/Flat toggle** — cluster view or raw ticket list
- **Search + severity filter**
- **Shared cluster badges** — gold tag when a root cause spans teams
- **Simulated clustering cycle** — refreshes every 30s, shows timer

Runs in **mock mode** by default (82 tickets, 16 clusters, 4 teams). Toggle to live mode via the top-bar button.

---

## Classification Taxonomy

9 systems, each with its own services:

| System | Example Services |
|--------|-----------------|
| CRM | Sales, Customer Portal, Lead Management, Reporting |
| ERP | Inventory, Procurement, HR, Finance |
| Payment Gateway | Checkout, Refunds, Fraud Detection, Billing |
| Infrastructure | Compute, Storage, Load Balancer, DNS |
| Network | VPN, Firewall, CDN, DNS Resolution |
| Security | IAM, SIEM, DLP, Endpoint Protection |
| Email | SMTP Relay, Spam Filter, Mailbox Store |
| Data Pipeline | ETL Jobs, Streaming, Data Warehouse |
| Other | General |

Plus: 4 incident types (Spike / Degradation / Unavailability / Outage), 4 severities, 4 urgencies, 9 root-cause categories.

---

## Project Layout

```
AI_Classifier/
├── ai_classification/
│   ├── main.py              # FastAPI entry
│   ├── config.py             # Env-based settings
│   ├── sync.py               # Background ticketing sync
│   ├── api/
│   │   ├── routes.py         # HTTP endpoints
│   │   └── schemas.py        # Request/response schemas
│   ├── core/
│   │   ├── classifier.py     # LLM classification (LiteLLM)
│   │   ├── store.py          # PostgreSQL + pgvector
│   │   └── grouping.py       # Graph clustering + LLM validation
│   └── domain/
│       ├── models.py         # Pydantic models
│       └── taxonomy.py       # Enums + services
├── frontend/
│   ├── index.html            # Classify form (port 8082)
│   └── dashboard/            # Three-lens dashboard
│       ├── index.html        # UI shell
│       ├── app.js            # Logic + rendering
│       └── data.js           # Mock data generator
├── ocr/ocr_server.py         # EasyOCR microservice
├── ticketing_simulator/      # Mock ticketing API
├── tests/                    # 42 tests
│   ├── test_classifier.py    # 15 tests
│   ├── test_incident_store.py# 22 tests
│   └── test_service.py       # 5 tests
├── docker-compose.yml        # Full stack
└── pyproject.toml
```

---

## Quick Start

```bash
# Backend
cd ai_classification
pip install -e .
uvicorn main:app --reload --port 8000

# Dashboard (no build)
cd frontend/dashboard
python3 -m http.server 8085
open http://localhost:8085
```

---

## Status Lifecycle

```
Active → Escalated → Third-party → Verify → Resolved
```

Each status has its own colored section inside cluster cards. Add new statuses in `STATUS_COLORS` in `app.js`.

---

## Key Files

| File | What |
|------|------|
| `frontend/dashboard/app.js` | Dashboard logic: roles, filters, status colors |
| `frontend/dashboard/data.js` | Mock data generator (deterministic) |
| `ai_classification/core/classifier.py` | LLM classification + retry |
| `ai_classification/core/store.py` | PostgreSQL + pgvector store |
| `ai_classification/core/grouping.py` | Graph clustering + LLM validator |
| `ai_classification/domain/taxonomy.py` | Classification enums |
| `NOTES.md` | Full design rationale |
