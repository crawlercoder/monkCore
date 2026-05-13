import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  Org,
  Repo,
  listOrgs,
  listReposForOrg,
  processRepo,
} from "../api";
import { ErrorBanner } from "../components/ErrorBanner";
import { StatusPill } from "../components/StatusPill";

// Page 3: list repos for the selected org. Auto-polls every 3s so the
// PENDING → CLONING → READY transition lights up without a manual
// refresh. Per-repo "Process" button triggers the worker synchronously.

const POLL_MS = 3000;

export function RepoList() {
  const [orgs, setOrgs] = useState<Org[] | null>(null);
  const [orgId, setOrgId] = useState("");

  const [repos, setRepos] = useState<Repo[] | null>(null);
  const [listError, setListError] = useState<ApiError | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  // Per-repo busy indicator for the Process button.
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [rowErrors, setRowErrors] = useState<Record<string, ApiError>>({});

  // Load orgs once; seed orgId from localStorage or first org.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const rows = await listOrgs();
        if (cancelled) return;
        setOrgs(rows);
        const last = localStorage.getItem("last_org_id");
        const pick = rows.find((o) => o.org_id === last)?.org_id ?? rows[0]?.org_id ?? "";
        setOrgId(pick);
      } catch (e) {
        if (!cancelled) setListError(e as ApiError);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Fetch repos for the selected org. We keep a ref to the latest
  // loader so the poll interval always calls the right one without
  // closing over stale state.
  const loadRepos = useCallback(async () => {
    if (!orgId) return;
    setRefreshing(true);
    try {
      const rows = await listReposForOrg(orgId);
      setRepos(rows);
      setListError(null);
    } catch (e) {
      setListError(e as ApiError);
    } finally {
      setRefreshing(false);
    }
  }, [orgId]);

  const loadRef = useRef(loadRepos);
  loadRef.current = loadRepos;

  useEffect(() => {
    if (!orgId) return;
    loadRef.current();
    // Interval re-fires every POLL_MS; React's useCallback deps make
    // loadRepos stable within a given orgId, so this is safe.
    const id = window.setInterval(() => {
      loadRef.current();
    }, POLL_MS);
    return () => window.clearInterval(id);
  }, [orgId]);

  async function onProcess(repoId: string) {
    setBusy((b) => ({ ...b, [repoId]: true }));
    setRowErrors((r) => {
      const { [repoId]: _, ...rest } = r;
      return rest;
    });
    try {
      const updated = await processRepo(repoId);
      setRepos((rows) =>
        rows ? rows.map((r) => (r.repo_id === repoId ? updated : r)) : rows,
      );
    } catch (e) {
      setRowErrors((r) => ({ ...r, [repoId]: e as ApiError }));
    } finally {
      setBusy((b) => {
        const { [repoId]: _, ...rest } = b;
        return rest;
      });
      // Whatever happened, refresh to show authoritative state.
      loadRef.current();
    }
  }

  return (
    <section className="card">
      <header className="card-header">
        <div className="row-between">
          <div>
            <h1>Repositories</h1>
            <p className="muted">
              Auto-refreshes every {Math.round(POLL_MS / 1000)}s. Click
              Process to kick ingestion.
            </p>
          </div>
          <div className="actions">
            <button
              type="button"
              className="btn"
              onClick={loadRepos}
              disabled={refreshing || !orgId}
            >
              {refreshing ? "Refreshing…" : "Refresh"}
            </button>
          </div>
        </div>

        {orgs && orgs.length > 0 && (
          <label className="field field-inline">
            <span className="field-label">Organization</span>
            <select
              value={orgId}
              onChange={(e) => setOrgId(e.target.value)}
            >
              {orgs.map((o) => (
                <option key={o.org_id} value={o.org_id}>
                  {o.name} ({o.org_id})
                </option>
              ))}
            </select>
          </label>
        )}
      </header>

      <ErrorBanner error={listError} />

      {!orgs && !listError && <div className="muted">Loading orgs…</div>}
      {orgs && orgs.length === 0 && (
        <div className="empty">
          No orgs yet. Create one on the <strong>Org Setup</strong> page.
        </div>
      )}
      {orgs && orgs.length > 0 && repos === null && !listError && (
        <div className="muted">Loading repos…</div>
      )}
      {repos && repos.length === 0 && (
        <div className="empty">
          No repos for this org yet. Register one on{" "}
          <strong>Repo Setup</strong>.
        </div>
      )}

      {repos && repos.length > 0 && (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Repo</th>
                <th>Branch</th>
                <th>Status</th>
                <th>Updated</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {repos.map((r) => (
                <tr key={r.repo_id}>
                  <td>
                    <div className="repo-url">{r.repo_url}</div>
                    <div className="small muted">
                      <code>{r.repo_id}</code>
                    </div>
                  </td>
                  <td>
                    <code>{r.branch}</code>
                  </td>
                  <td>
                    <StatusPill status={r.status} />
                  </td>
                  <td className="small muted">{formatTime(r.updated_at)}</td>
                  <td className="actions-cell">
                    <button
                      type="button"
                      className="btn btn-small"
                      onClick={() => onProcess(r.repo_id)}
                      disabled={
                        !!busy[r.repo_id] || r.status === "CLONING"
                      }
                      title={
                        r.status === "CLONING"
                          ? "Already being processed"
                          : "Trigger ingestion synchronously"
                      }
                    >
                      {busy[r.repo_id] ? "Processing…" : "Process"}
                    </button>
                    {rowErrors[r.repo_id] && (
                      <div className="small error-text">
                        {rowErrors[r.repo_id].message}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString();
  } catch {
    return iso;
  }
}
