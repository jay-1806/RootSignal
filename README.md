# RootSignal

An AI SRE agent that investigates production incidents. It forms hypotheses, queries metrics, logs and recent changes to test them, and produces a root-cause analysis. Each claim in the analysis links to the evidence behind it, and confidence scores are computed from that evidence. Fixes are only proposed, and a human must approve one before it runs.

> **Status:** Phases 1–2 are done: the foundation, plus a demo system with fault injection and telemetry. See [docs/PLAN.md](docs/PLAN.md) for the full 10-day roadmap.

## Architecture

```
  Dashboard (React)      CLI (rootsignal)      MCP server
          └──────────────────┬──────────────────┘
                             │ HTTP /api
                     FastAPI backend
                             │
            Investigation engine (state machine)
  RECEIVED → GATHERING_CONTEXT → HYPOTHESIZING → PLANNING → QUERYING → SCORING
                                     ▲                                  │
                                     └──────── refine ◄─────────────────┤
                                                                        ▼
                          CONCLUDED → AWAITING_APPROVAL → REMEDIATING → RESOLVED
                             │
   ┌──────────┬──────────────┼──────────────┬─────────────┐
 LLMProvider  TelemetryProvider  ChangeProvider  IncidentSource  Remediator
 mock/gemini  prometheus+loki    deploys/commits alertmanager    allowlist only
 /grok
                             │
                 Postgres + pgvector (investigations, incident memory, audit)
```

**Design rules**
1. **Code controls the flow; the LLM reasons.** Every state transition is validated and recorded (`core/state_machine.py`).
2. **Confidence is computed from evidence.** It is never taken from the LLM's own answer.
3. **Every external system sits behind an interface** (`providers/`). Tests use fakes, and new backends can be added without changing the engine.
4. **Read-only by default.** Fixes come from a typed allowlist and need human approval. The LLM never generates shell commands.
5. **Mock LLM for dev and CI.** No API keys are needed to run the tests.

## Quick start

```bash
cp .env.example .env          # LLM_PROVIDER=mock works with no keys
docker compose up --build
```

| What | URL |
|---|---|
| RootSignal dashboard | http://localhost:5173 |
| RootSignal API docs | http://localhost:8000/docs |
| Grafana (demo metrics + logs) | http://localhost:3001 (change with `GRAFANA_PORT` in `.env`) |
| Prometheus (alerts under "Alerts") | http://localhost:9090 |
| Fault control (Swagger UI) | http://localhost:8090/docs |
| Demo gateway | http://localhost:8080 |

## Demo system

A small "production" system for RootSignal to investigate. `loadgen` sends it about 5 requests per second.

```
loadgen → gateway → checkout → inventory → Postgres
                                   └──────→ Redis (cache)
```

Each service exposes Prometheus metrics at `/metrics` and writes JSON logs, which are shipped to Loki. Grafana opens directly on the **Demo services overview** dashboard.

### Injecting faults

| Scenario | Target | What you'll see |
|---|---|---|
| `bad_deploy` | checkout | Deploy event `1.8.0 → 1.9.0`, `KeyError: 'discount_code'`, ~40% 5xx, 502s at the gateway |
| `db_pool_exhaustion` | inventory | "timed out acquiring database connection", pool in use = max, 503/502s |
| `latency_spike` | inventory | p95 above 1s, "slow cache response" warnings, **no errors** |
| `memory_leak` | checkout | Memory rising, then OOM-killed at 300 MB and restarted |

Use the Swagger UI at http://localhost:8090/docs, or:

```bash
curl -X POST localhost:8090/scenarios/bad_deploy/enable    # PowerShell: use curl.exe
curl localhost:8090/scenarios                              # what's active
curl localhost:8090/changes                                # deploy / config events
curl -X POST localhost:8090/reset                          # turn everything off
```

Services never log the name of the active scenario. Like a real on-call engineer, RootSignal has to work out the cause from the symptoms.

### Querying telemetry through RootSignal

```bash
curl "localhost:8000/api/telemetry/services"
curl "localhost:8000/api/telemetry/metrics?query=sum by (service) (rate(http_requests_total[1m]))&minutes=5"
curl "localhost:8000/api/telemetry/logs?query={service=\"inventory\",level=\"error\"}&limit=20"
```

### Run without Docker

```bash
# backend (uses SQLite locally)
cd backend
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"
DATABASE_URL=sqlite+aiosqlite:///./dev.db uvicorn rootsignal.main:app --reload

# dashboard (in another terminal)
cd dashboard && npm install && npm run dev
```

### Tests

```bash
cd backend && ruff check . && pytest -q
cd services/demo && ruff check . && pytest -q   # inventory DB tests need INVENTORY_TEST_DB_URL
cd dashboard && npm run build
```

## Layout

```
backend/rootsignal/
  core/          domain models + investigation state machine
  providers/     LLM, telemetry, change, incident-source, remediation interfaces
  db/            SQLAlchemy models + session
  services/      investigation persistence (used by API, CLI, MCP)
  api/           FastAPI routes + response schemas
dashboard/       React + TypeScript UI
services/demo/   gateway, checkout, inventory, fault-control, loadgen (one image)
infra/           Prometheus config + alert rules, Grafana provisioning + dashboard
docs/PLAN.md     roadmap and checklist
```

## API

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Service, database, and LLM provider status |
| POST | `/api/investigations` | Start an investigation from an alert |
| GET | `/api/investigations?state=` | List investigations |
| GET | `/api/investigations/{id}` | Investigation with its state history |
| GET | `/api/telemetry/services` | Services that have metrics |
| GET | `/api/telemetry/metrics?query=&minutes=&step=` | PromQL range query |
| GET | `/api/telemetry/logs?query=&minutes=&limit=` | LogQL query (newest lines, in time order) |
