import { type FormEvent, useCallback, useEffect, useState } from "react";
import { api, type Health, type Investigation, type Severity } from "./api";

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [investigations, setInvestigations] = useState<Investigation[]>([]);
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
  }, [refresh]);

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
          <p className="muted">No investigations yet. Create one above.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Title</th>
                <th>Service</th>
                <th>Severity</th>
                <th>State</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {investigations.map((inv) => (
                <tr key={inv.id}>
                  <td>{inv.title}</td>
                  <td>{inv.service}</td>
                  <td>
                    <span className={`pill sev-${inv.severity}`}>{inv.severity}</span>
                  </td>
                  <td>
                    <span className="pill">{inv.state}</span>
                  </td>
                  <td className="muted">{new Date(inv.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </main>
  );
}

function HealthBadge({ health }: { health: Health | null }) {
  if (!health) return <span className="pill">connecting…</span>;
  return (
    <span className={`pill ${health.status === "ok" ? "ok" : "sev-critical"}`}>
      v{health.version} · db {health.database} · llm {health.llm_provider}
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
        placeholder="Alert title, e.g. High p99 latency on checkout"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        required
      />
      <input value={service} onChange={(e) => setService(e.target.value)} required />
      <select value={severity} onChange={(e) => setSeverity(e.target.value as Severity)}>
        <option value="critical">critical</option>
        <option value="high">high</option>
        <option value="medium">medium</option>
        <option value="low">low</option>
      </select>
      <button disabled={busy}>{busy ? "Creating…" : "Start investigation"}</button>
    </form>
  );
}
