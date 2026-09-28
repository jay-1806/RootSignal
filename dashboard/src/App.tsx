import { type FormEvent, useCallback, useEffect, useState } from "react";
import { api, type Health, type Investigation, type Severity } from "./api";

const REFRESH_MS = 3000;
const SERVICES = ["gateway", "checkout", "inventory"];

const pct = (x: number) => `${Math.round(x * 100)}%`;

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [investigations, setInvestigations] = useState<Investigation[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [h, list] = await Promise.all([api.health(), api.listInvestigations()]);
      setHealth(h);
      setInvestigations(list);
      setError(null);
    } catch (e) {
      setError(`Backend unreachable: ${(e as Error).message}`);
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const current = investigations.find((i) => i.id === selected) ?? null;

  return (
    <main>
      <header>
        <h1>RootSignal</h1>
        <HealthBadge health={health} />
      </header>

      {error && <p className="error">{error}</p>}

      <NewInvestigationForm onCreated={refresh} />

      <section>
        <h2>Investigations</h2>
        {investigations.length === 0 ? (
          <p className="muted">
            No investigations yet. Create one above, or enable a fault at localhost:8090/docs and
            wait for the alert.
          </p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Alert</th>
                <th>Service</th>
                <th>Severity</th>
                <th>State</th>
                <th>Root cause</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {investigations.map((inv) => (
                <tr
                  key={inv.id}
                  className={`clickable ${inv.id === selected ? "selected" : ""}`}
                  onClick={() => setSelected(inv.id === selected ? null : inv.id)}
                >
                  <td>
                    {inv.title}
                    <div className="muted small">{inv.source}</div>
                  </td>
                  <td>{inv.service}</td>
                  <td>
                    <span className={`pill sev-${inv.severity}`}>{inv.severity}</span>
                  </td>
                  <td>
                    <span className={`pill state-${inv.state}`}>{inv.state}</span>
                  </td>
                  <td>
                    {inv.rca ? (
                      <>
                        {inv.rca.root_cause}{" "}
                        <span className={`pill ${inv.rca.conclusive ? "ok" : ""}`}>
                          {pct(inv.rca.confidence)}
                        </span>
                      </>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td className="muted">{new Date(inv.created_at).toLocaleTimeString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {current && <Detail inv={current} onChanged={refresh} />}
    </main>
  );
}

function Detail({ inv, onChanged }: { inv: Investigation; onChanged: () => void }) {
  const rca = inv.rca;
  const canResolve = inv.state === "concluded" || inv.state === "awaiting_approval";

  async function resolve(correct: boolean) {
    await api.resolve(inv.id, correct);
    onChanged();
  }

  return (
    <section className="detail">
      <h2>
        {inv.title} · {inv.service}
      </h2>

      {rca && (
        <div className="card">
          <h3>
            Root cause: {rca.root_cause}{" "}
            <span className={`pill ${rca.conclusive ? "ok" : "sev-medium"}`}>
              {pct(rca.confidence)} {rca.conclusive ? "conclusive" : "inconclusive"}
            </span>
          </h3>
          <p>{rca.summary}</p>
          <p className="muted">{rca.reasoning}</p>
          <ul className="facts">
            {rca.started_at && <li>Started: {new Date(rca.started_at).toLocaleTimeString()}</li>}
            {rca.triggering_change && <li>Triggering change: {rca.triggering_change}</li>}
            <li>Affected: {rca.affected_services.join(", ")}</li>
            <li>
              Written by: {rca.written_by} · rounds: {inv.iterations} · LLM calls:{" "}
              {inv.llm_usage.calls} (${inv.llm_usage.cost_usd.toFixed(4)})
            </li>
          </ul>
          {rca.recommended_action && (
            <p className="action">
              Proposed fix: <b>{rca.recommended_action.type}</b> on{" "}
              {rca.recommended_action.service}{" "}
              {Object.entries(rca.recommended_action.params)
                .map(([k, v]) => `${k}=${v}`)
                .join(" ")}{" "}
              <span className="muted">— {rca.recommended_action.reason}. Not executed.</span>
            </p>
          )}
          {canResolve && (
            <div className="buttons">
              <button onClick={() => resolve(true)}>Confirm RCA (save to memory)</button>
              <button className="secondary" onClick={() => resolve(false)}>
                RCA is wrong
              </button>
            </div>
          )}
        </div>
      )}

      <h3>Hypotheses</h3>
      {inv.hypotheses.length === 0 && <p className="muted">None yet…</p>}
      {inv.hypotheses.map((h) => (
        <details key={h.id} className="card" open={h.id === inv.hypotheses[0]?.id}>
          <summary>
            <span className="bar" style={{ width: pct(h.confidence) }} />
            <b>{pct(h.confidence)}</b> {h.statement}{" "}
            <span className="muted small">
              ({h.category}, round {h.iteration}, by {h.source})
            </span>
          </summary>
          <ul className="evidence">
            {h.evidence.map((e, i) => (
              <li key={i} className={e.supports ? "for" : "against"} title={e.query}>
                {e.supports ? "✔" : "✘"} [{e.kind} · w={e.weight.toFixed(2)}] {e.summary}
              </li>
            ))}
            {h.evidence.length === 0 && <li className="muted">no usable evidence</li>}
          </ul>
        </details>
      ))}

      <h3>Timeline</h3>
      <ol className="timeline">
        {inv.history.map((s, i) => (
          <li key={i}>
            <span className="muted">{new Date(s.at).toLocaleTimeString()}</span>{" "}
            <b>{s.to_state}</b> {s.note}
          </li>
        ))}
      </ol>
    </section>
  );
}

function HealthBadge({ health }: { health: Health | null }) {
  if (!health) return <span className="pill">connecting…</span>;
  return (
    <span className={`pill ${health.status === "ok" ? "ok" : "sev-critical"}`}>
      v{health.version} · db {health.database} · reasoner {health.reasoner}
    </span>
  );
}

function NewInvestigationForm({ onCreated }: { onCreated: () => void }) {
  const [title, setTitle] = useState("");
  const [service, setService] = useState("checkout");
  const [severity, setSeverity] = useState<Severity>("high");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api.createInvestigation({ title, service, severity, source: "dashboard" });
      setTitle("");
      onCreated();
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit}>
      <input
        placeholder="Alert title, e.g. High error rate on checkout"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        required
      />
      <select value={service} onChange={(e) => setService(e.target.value)}>
        {SERVICES.map((s) => (
          <option key={s}>{s}</option>
        ))}
      </select>
      <select value={severity} onChange={(e) => setSeverity(e.target.value as Severity)}>
        <option value="critical">critical</option>
        <option value="high">high</option>
        <option value="medium">medium</option>
        <option value="low">low</option>
      </select>
      <button disabled={busy}>{busy ? "Starting…" : "Investigate"}</button>
    </form>
  );
}
