# RootSignal

An AI SRE agent that investigates production incidents. It forms hypotheses, queries metrics, logs and recent changes to test them, and produces a root-cause analysis. Each claim in the analysis links to the evidence behind it, and confidence scores are computed from that evidence. Fixes are only proposed, and a human must approve one before it runs.

> **Status:** Phase 1 (foundation) is complete. See [docs/PLAN.md](docs/PLAN.md) for the full 10-day roadmap.

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
- Dashboard: http://localhost:5173
- API docs: http://localhost:8000/docs

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
services/        demo microservices + fault injection (Phase 2)
docs/PLAN.md     roadmap and checklist
```

## API (Phase 1)

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Service, database, and LLM provider status |
| POST | `/api/investigations` | Start an investigation from an alert |
| GET | `/api/investigations?state=` | List investigations |
| GET | `/api/investigations/{id}` | Investigation with its state history |
