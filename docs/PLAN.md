# RootSignal — Build Plan

**Goal:** An AI SRE agent that looks into production incidents on its own. It gathers metrics, logs and recent changes, forms hypotheses, scores the evidence, and writes a root-cause analysis with a suggested fix. A human must approve any fix before it runs.

**Budget:** 10 days × 6–8 hrs. **Stack:** Python 3.12 · FastAPI · Postgres + pgvector · React + TS · Prometheus + Loki + Grafana · Gemini / Grok · Docker Compose.

## Scope

| In scope | Out of scope (future work) |
|---|---|
| Investigation engine (state machine) | AWS CloudWatch, Datadog, PagerDuty |
| Evidence-based confidence scores | Kubernetes |
| Gemini + Grok + Mock LLM providers | Distributed tracing (Tempo) |
| Prometheus + Loki telemetry | Auth / multi-tenant / RBAC |
| Demo microservices + fault injection | Auto-fix without human approval |
| Incident memory (pgvector) | Cloud deployment |
| Dashboard, CLI, MCP server | |
| Human-approved fixes + audit log | |
| Evaluation harness | |

## Phases

### Phase 1 — Foundation (Days 1–2)
- [x] Monorepo, `.env.example`, plan
- [x] FastAPI app, config, `/health`
- [x] Domain models + investigation state machine
- [x] Provider interfaces + `MockLLMProvider`
- [x] DB layer + investigations API
- [x] Tests + lint
- [x] Dashboard shell
- [x] Docker Compose + CI

### Phase 2 — Demo system + telemetry (Days 2–4)
- [x] 3 demo services: `gateway → checkout → inventory` + Postgres/Redis
- [x] Structured JSON logs → Loki; `/metrics` → Prometheus; Grafana dashboard
- [x] Fault-control service with 4 scenarios: bad deploy, DB pool exhaustion, latency spike, memory leak
- [x] Load generator
- [x] `PrometheusLokiTelemetryProvider` + `/api/telemetry/*` endpoints
- [x] Prometheus alert rules (Alertmanager hookup is Phase 3)

### Phase 3 — The brain (Days 4–7)
- [ ] Gemini + Grok providers (structured Pydantic output, fallback, cost/latency tracking)
- [ ] Orchestrator: runs the state machine end-to-end, saving every step
- [ ] Evidence scoring → computed confidence, refinement loop
- [ ] Change correlation (deploy version / GitHub commits)
- [ ] Incident memory: embeddings + similar-incident search
- [ ] Alertmanager webhook → auto-start investigation

### Phase 4 — Interfaces + remediation (Days 7–9)
- [ ] Dashboard: incident list, timeline, hypotheses, evidence, RCA, approve button
- [ ] CLI `rootsignal` (typer): investigate / status / approve
- [ ] MCP server: ~5 tools (list incidents, investigate, get RCA, query telemetry, propose fix)
- [ ] Remediation: allowlisted actions (restart, rollback, toggle flag) + audit log

### Phase 5 — Proof + polish (Day 10)
- [ ] Eval harness: run each fault scenario and measure RCA accuracy, time and cost
- [ ] README with architecture diagram, demo GIF, and real eval numbers

## Design rules
1. **State machine, not free-roaming agents.** The LLM only plans and reasons. Code handles control flow, queries, and actions.
2. **Confidence is computed from evidence**, not reported by the LLM.
3. **Every external system sits behind an interface.** That keeps tests easy and makes future providers credible.
4. **Read-only by default.** Fixes come from an allowlist and require approval. No LLM-generated shell commands.
5. **Mock LLM for dev and CI.** Spend the free Gemini/Grok tokens only on real demos.
