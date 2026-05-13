"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { ErrorBanner } from "@/components/ErrorBanner";
import { JobStatusPill } from "@/components/JobStatusPill";
import { ApiError, JobSummary, Org, listJobsForOrg, listOrgs } from "@/lib/api";

const POLL_MS = 5000;

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function specPreview(spec: string, max = 120): string {
  const t = (spec || "").replace(/\s+/g, " ").trim();
  if (t.length <= max) return t;
  return `${t.slice(0, max)}…`;
}

export default function JobsListPage() {
  const [orgs, setOrgs] = useState<Org[] | null>(null);
  const [orgId, setOrgId] = useState("");

  const [jobs, setJobs] = useState<JobSummary[] | null>(null);
  const [listError, setListError] = useState<ApiError | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const rows = await listOrgs();
        if (cancelled) return;
        setOrgs(rows);
        const last = localStorage.getItem("last_org_id");
        const pick =
          rows.find((o) => o.org_id === last)?.org_id ?? rows[0]?.org_id ?? "";
        setOrgId(pick);
      } catch (e) {
        if (!cancelled) {
          setListError(
            e instanceof ApiError ? e : new ApiError("Could not list orgs", 0, null),
          );
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const loadJobs = useCallback(async () => {
    if (!orgId) return;
    setRefreshing(true);
    try {
      const rows = await listJobsForOrg(orgId, { limit: 200 });
      setJobs(rows);
      setListError(null);
    } catch (e) {
      setListError(
        e instanceof ApiError ? e : new ApiError("Failed to list jobs", 0, null),
      );
    } finally {
      setRefreshing(false);
    }
  }, [orgId]);

  const loadRef = useRef(loadJobs);
  loadRef.current = loadJobs;

  useEffect(() => {
    if (!orgId) return;
    loadRef.current();
    const id = window.setInterval(() => {
      loadRef.current();
    }, POLL_MS);
    return () => window.clearInterval(id);
  }, [orgId]);

  return (
    <main className="page wide">
      <section className="card">
        <header className="card-header">
          <div className="row-between">
            <div>
              <h1>Jobs</h1>
              <p className="muted">
                All jobs for the selected organization (newest first). Refreshes
                every {Math.round(POLL_MS / 1000)}s. Open a row to see live status
                and logs.
              </p>
            </div>
            <div className="actions">
              <button
                type="button"
                className="btn"
                onClick={loadJobs}
                disabled={refreshing || !orgId}
              >
                {refreshing ? "Refreshing…" : "Refresh"}
              </button>
            </div>
          </div>

          {orgs && orgs.length > 0 ? (
            <label className="field field-inline">
              <span className="field-label">Organization</span>
              <select
                value={orgId}
                onChange={(e) => {
                  setOrgId(e.target.value);
                  localStorage.setItem("last_org_id", e.target.value);
                }}
              >
                {orgs.map((o) => (
                  <option key={o.org_id} value={o.org_id}>
                    {o.name} ({o.org_id})
                  </option>
                ))}
              </select>
            </label>
          ) : null}
        </header>

        <ErrorBanner error={listError} />

        {!orgs && !listError ? <div className="muted">Loading orgs…</div> : null}
        {orgs && orgs.length === 0 ? (
          <div className="empty">No orgs yet. Create one on the home page (Onboarding).</div>
        ) : null}
        {orgs && orgs.length > 0 && jobs === null && !listError ? (
          <div className="muted">Loading jobs…</div>
        ) : null}
        {jobs && jobs.length === 0 ? (
          <div className="empty">No jobs for this org yet. Submit a spec from Home (New MR).</div>
        ) : null}

        {jobs && jobs.length > 0 ? (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Spec</th>
                  <th>Created</th>
                  <th>MR</th>
                  <th aria-label="Open detail" />
                </tr>
              </thead>
              <tbody>
                {jobs.map((j) => (
                  <tr key={j.job_id}>
                    <td>
                      <JobStatusPill status={j.status} />
                    </td>
                    <td>
                      <div className="job-spec-preview" title={j.spec || undefined}>
                        {j.spec ? specPreview(j.spec) : "—"}
                      </div>
                      <div className="small muted">
                        <code>{j.job_id}</code>
                      </div>
                    </td>
                    <td className="small muted">{formatTime(j.created_at)}</td>
                    <td className="small">
                      {j.mr_url ? (
                        <a href={j.mr_url} target="_blank" rel="noreferrer noopener">
                          link
                        </a>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="actions-cell">
                      <Link
                        href={`/job/${encodeURIComponent(j.job_id)}`}
                        className="btn btn-small btn-primary"
                      >
                        Open
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
    </main>
  );
}
