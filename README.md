# RootSignal

An AI SRE agent that investigates production incidents. It forms hypotheses, queries metrics, logs and recent changes to test them, and produces a root-cause analysis. Each claim in the analysis links to the evidence behind it, and confidence scores are computed from that evidence. Fixes are only proposed, and a human must approve one before it runs.

> **Status:** Phases 1–3 are done: the foundation, the demo system with telemetry, and the investigation engine. See [docs/PLAN.md](docs/PLAN.md) for the full 10-day roadmap.

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

## How an investigation works

1. **Trigger.** A Prometheus alert fires, Alertmanager calls `/api/webhooks/alertmanager`, and an investigation starts. Repeat alerts are deduplicated. You can also start one from the dashboard or `POST /api/investigations`.
2. **Gather context.** Triage checks run on the alerting service and everything it depends on: errors, latency, memory, restarts, version changes, failing calls to dependencies, and recent deploys. Similar incidents that a human confirmed earlier are pulled from memory (pgvector).
3. **Hypothesize.** The reasoner proposes causes (category + service) and picks checks to test them from a fixed **catalog of 11 read-only checks**. It never writes PromQL/LogQL. Every hypothesis also gets its category's standard playbook checks.
4. **Query.** Code builds each query from a template and runs it read-only, 4 at a time, and each query runs only once.
5. **Score.** Each result counts for or against a hypothesis, weighted by how reliable the check is and how strong the signal was. Confidence is computed from that evidence, never taken from the LLM.
6. **Refine.** If no hypothesis reaches `CONCLUDE_CONFIDENCE` (0.7), the reasoner proposes new ones with the evidence so far in view (up to `MAX_ITERATIONS`).
7. **Conclude.** The RCA includes the root cause, confidence, start time, affected services, triggering change, the strongest evidence, alternatives, and a proposed fix from the allowlist (rollback, restart). A fix is proposed only when the conclusion is conclusive. **Nothing is executed yet.**
8. **Learn.** A human confirms or corrects the RCA (`/resolve`). Confirmed RCAs are stored in incident memory and count as evidence in later investigations.

**Reasoners.** `LLM_PROVIDER=mock` uses a deterministic **rule-based reasoner** that needs no tokens. With `gemini` or `grok`, the LLM proposes hypotheses and writes the narrative. It only sees redacted summaries, and anything it proposes that isn't valid is dropped. If every LLM fails (quota, timeout, bad output), the investigation continues with the rule-based reasoner. Every LLM call is logged with its tokens, latency and cost.

**Using your API keys.** Put your key(s) in `.env`, set `LLM_PROVIDER=gemini` (or `grok`), then check the setup with `curl.exe -X POST localhost:8000/api/llm/test`. An investigation makes 2–3 LLM calls, or up to 4 if it refines.

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
| Alertmanager | http://localhost:9093 |
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

**End-to-end demo:** enable a fault. After about 1–2 minutes (alert `for: 30s`, plus Alertmanager's `group_wait`), an investigation appears in the dashboard on its own. Click it to see the RCA, the hypotheses with their evidence, and the timeline. For a quicker test, start one yourself from the dashboard form once the Grafana graphs show the fault.

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
  core/          domain models, state machine, service topology
  engine/        orchestrator, check catalog, playbooks, scoring, reasoners, memory
  providers/     LLM (gemini/grok/mock), prometheus+loki, fault-control changes, alertmanager
  db/            SQLAlchemy models + session
  migrations/    Alembic migrations (run automatically on startup)
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
| GET | `/api/health` | Service, database, LLM and reasoner status |
| POST | `/api/investigations?run=true` | Create an investigation from an alert and start it |
| GET | `/api/investigations?state=` | List investigations |
| GET | `/api/investigations/{id}` | Full investigation: history, hypotheses + evidence, RCA, LLM usage |
| POST | `/api/investigations/{id}/run` | Start one created with `run=false` |
| POST | `/api/investigations/{id}/resolve` | Human verdict: confirm/correct (saved to memory) or reject |
| POST | `/api/webhooks/alertmanager` | Alertmanager webhook (optional Bearer token) |
| GET | `/api/memory`, `/api/memory/similar?q=` | Incident memory |
| POST | `/api/llm/test` | Check the configured LLM key/model (a few tokens) |
| GET | `/api/telemetry/services` | Services that have metrics |
| GET | `/api/telemetry/metrics?query=&minutes=&step=` | PromQL range query |
| GET | `/api/telemetry/logs?query=&minutes=&limit=` | LogQL query (newest lines, in time order) |
