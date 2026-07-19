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

## Test Data

```bash
# Reset first
curl -s -X POST http://127.0.0.1:8000/reset

# Import batch 1
curl -s -X POST http://127.0.0.1:8000/import/test_incidents.json

# Import batch 2 (the related ones)
curl -s -X POST http://127.0.0.1:8000/import/test_incidents_batch2.json

# Check results
curl -s http://127.0.0.1:8000/reports/daily
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

---

## Live Logging

Every pipeline step logs to stdout with timestamps. Commands to watch live:

```bash
# 1. Tail backend logs (if running with log file)
tail -f /tmp/ai_classifier.log

# 2. Directly follow the uvicorn process output
#    Find the PID first, then strace/follow
ps aux | grep uvicorn
# then: journalctl _PID=<PID> --follow  (if systemd)

# 3. Or restart with tee to capture + watch live
cd ~/projects/AI_Classifier
uv run uvicorn ai_classification.api.routes:app --host 0.0.0.0 --port 8000 2>&1 | tee /tmp/ai_classifier.log
# In another terminal: tail -f /tmp/ai_classifier.log
```

### What each log level tells you

| Level | What you see |
|-------|-------------|
| `INFO` | API calls, classifications, clusters found, similarity matches |
| `WARN` | Classification failures, reset actions, oversized groups dropped |
| `DEBUG` | Embedding generation, similarity scores per match, pruned incidents |

### Step-by-step log example

```
# 1. API entry
INFO  POST /classify — title='Login SMS code not arriving', group='App Support', priority=critical

# 2. LLM classification
INFO  Classifying — title='Login SMS code not arriving'
LiteLLM completion() model= qwen/qwen3.6-35b-a3b; provider = openrouter
INFO  Classification succeeded — system=Nusuk Application, severity=Critical, confidence=high

# 3. Embedding + similarity search
DEBUG Embedding generated — input=142 chars, dim=1024
INFO  Similarity search — threshold=0.80, matches=2
DEBUG   Match: abc123 — 85.3% — Login OTP expired before submission

# 4. Save to DB
INFO  Classify result — id=abc123, system=Nusuk Application, service=Login, severity=Critical, dupes=2
DEBUG Canonical: Nusuk Application/Login: SMS OTP not delivered after 5 resend attempts; 2FA blocked.
INFO  Saved incident abc123 — system=Nusuk Application, severity=Critical

# 5. Clustering (on report fetch or background rebuild)
INFO  Cluster candidate — 3 incidents, density=0.67, sending to LLM validator
INFO  Validator result: coherent=True, keep=3, remove=0, name='Login SMS Delivery Failure'
INFO  Cluster accepted — name='Login SMS Delivery Failure', system=Nusuk Application, count=3, pruned=0
INFO  Reports daily: 10 incidents, 2 clusters, 5 subsystems
```
