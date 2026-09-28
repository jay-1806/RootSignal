// Types mirror backend/rootsignal/api/schemas.py and core/models.py.

export type Severity = "critical" | "high" | "medium" | "low";

export interface Health {
  status: "ok" | "degraded";
  version: string;
  llm_provider: string;
  reasoner: string;
  database: string;
}

export interface StateChange {
  from_state: string | null;
  to_state: string;
  at: string;
  note: string;
}

export interface Evidence {
  kind: string;
  check: string;
  service: string;
  summary: string;
  query: string;
  supports: boolean;
  weight: number;
}

export interface Hypothesis {
  id: string;
  statement: string;
  category: string;
  service: string;
  source: string;
  iteration: number;
  confidence: number;
  evidence: Evidence[];
}

export interface RemediationAction {
  type: string;
  service: string;
  params: Record<string, string>;
  reason: string;
}

export interface RCA {
  root_cause: string;
  category: string;
  service: string;
  confidence: number;
  conclusive: boolean;
  summary: string;
  reasoning: string;
  started_at: string | null;
  affected_services: string[];
  triggering_change: string | null;
  evidence: Evidence[];
  alternatives: { statement: string; confidence: number }[];
  recommended_action: RemediationAction | null;
  written_by: string;
}

export interface Investigation {
  id: string;
  title: string;
  service: string;
  severity: Severity;
  source: string;
  state: string;
  history: StateChange[];
  hypotheses: Hypothesis[];
  rca: RCA | null;
  iterations: number;
  reasoner: string;
  llm_usage: { calls: number; failed_calls: number; input_tokens: number; output_tokens: number; cost_usd: number };
  created_at: string;
}

export interface NewAlert {
  title: string;
  service: string;
  severity: Severity;
  source: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<Health>("/health"),
  listInvestigations: () => request<Investigation[]>("/investigations"),
  createInvestigation: (alert: NewAlert) =>
    request<Investigation>("/investigations", { method: "POST", body: JSON.stringify(alert) }),
  resolve: (id: string, correct: boolean) =>
    request<Investigation>(`/investigations/${id}/resolve`, {
      method: "POST",
      body: JSON.stringify({ correct }),
    }),
};
