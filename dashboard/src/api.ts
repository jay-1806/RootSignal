// Types mirror backend/rootsignal/api/schemas.py.

export type Severity = "critical" | "high" | "medium" | "low";

export interface Health {
  status: "ok" | "degraded";
  version: string;
  llm_provider: string;
  database: string;
}

export interface StateChange {
  from_state: string | null;
  to_state: string;
  at: string;
  note: string;
}

export interface Investigation {
  id: string;
  title: string;
  service: string;
  severity: Severity;
  source: string;
  state: string;
  history: StateChange[];
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
};
