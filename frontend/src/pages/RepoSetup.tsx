import { FormEvent, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  ApiError,
  Org,
  Repo,
  RepoRegisterResponse,
  canonicalGitUrl,
  listOrgs,
  listReposForOrg,
  registerRepo,
} from "../api";
import { ErrorBanner } from "../components/ErrorBanner";

// Page 2: register a repo under an existing org (idempotent on URL).
//
// Design notes:
//
// * The backend collapses duplicates by canonical URL under the same
//   org and returns ``reused=true`` + 200 OK. The UI reflects that
//   with a distinct "already registered — reusing id" banner so the
//   operator doesn't assume a fresh ingestion was queued.
//
// * As the user types, we canonicalize the URL client-side and
//   check it against the existing repos for the selected org. If a
//   match is found, we show an inline hint *before* submission. The
//   backend still validates on its own — this is just UX.
export function RepoSetup() {
  const nav = useNavigate();

  const [orgs, setOrgs] = useState<Org[] | null>(null);
  const [loadingOrgs, setLoadingOrgs] = useState(true);
  const [loadError, setLoadError] = useState<ApiError | null>(null);

  const [orgId, setOrgId] = useState("");
  const [repoUrl, setRepoUrl] = useState("");
  const [branch, setBranch] = useState("main");

  // Cached repo list per selected org, for the dup-URL hint.
  const [repos, setRepos] = useState<Repo[]>([]);

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<ApiError | null>(null);
  const [result, setResult] = useState<RepoRegisterResponse | null>(null);

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
        if (!cancelled) setLoadError(e as ApiError);
      } finally {
        if (!cancelled) setLoadingOrgs(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Refresh the cached repo list whenever the user switches orgs. This
  // powers the "you already registered this URL" hint; failure here
  // is non-fatal (the backend still dedupes on submit).
  useEffect(() => {
    if (!orgId) {
      setRepos([]);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const rows = await listReposForOrg(orgId);
        if (!cancelled) setRepos(rows);
      } catch {
        if (!cancelled) setRepos([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [orgId]);

  const duplicate = useMemo<Repo | null>(() => {
    if (!repos.length || !repoUrl.trim()) return null;
    const needle = canonicalGitUrl(repoUrl);
    if (!needle) return null;
    return repos.find((r) => canonicalGitUrl(r.repo_url) === needle) ?? null;
  }, [repos, repoUrl]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!orgId) return;
    setSubmitting(true);
    setSubmitError(null);
    setResult(null);
    try {
      const created = await registerRepo({
        repo_url: repoUrl.trim(),
        org_id: orgId,
        branch: branch.trim() || "main",
      });
      setResult(created);
      localStorage.setItem("last_org_id", created.org_id);
      if (!created.reused) {
        setRepoUrl("");
      }
      // Refresh the cache so further edits see the new row.
      try {
        const rows = await listReposForOrg(orgId);
        setRepos(rows);
      } catch {
        // non-fatal
      }
    } catch (e) {
      setSubmitError(e as ApiError);
    } finally {
      setSubmitting(false);
    }
  }

  const noOrgs = !loadingOrgs && !loadError && orgs && orgs.length === 0;

  return (
    <section className="card">
      <header className="card-header">
        <h1>Register repository</h1>
        <p className="muted">
          Creates a <code>PENDING</code> row in DynamoDB. Ingestion is
          kicked off separately from the Repo List page. Registering
          the same repository URL twice under one org reuses the
          existing <code>repo_id</code> — no re-clone, no re-embed.
        </p>
      </header>

      {loadingOrgs && <div className="muted">Loading orgs…</div>}
      <ErrorBanner error={loadError} />

      {noOrgs && (
        <div className="empty">
          No orgs yet.{" "}
          <button
            type="button"
            className="link"
            onClick={() => nav("/orgs/new")}
          >
            Create one first
          </button>
          .
        </div>
      )}

      {orgs && orgs.length > 0 && (
        <form onSubmit={onSubmit} className="form">
          <label className="field">
            <span className="field-label">Organization</span>
            <select
              value={orgId}
              onChange={(e) => setOrgId(e.target.value)}
              required
              disabled={submitting}
            >
              {orgs.map((o) => (
                <option key={o.org_id} value={o.org_id}>
                  {o.name} ({o.org_id})
                </option>
              ))}
            </select>
          </label>

          <label className="field">
            <span className="field-label">Repository URL</span>
            <input
              type="text"
              value={repoUrl}
              onChange={(e) => setRepoUrl(e.target.value)}
              placeholder="https://gitlab.com/acme/web.git"
              required
              disabled={submitting}
              autoComplete="off"
              spellCheck={false}
            />
            <span className="field-hint">
              HTTPS only — the backend refuses <code>http://</code> and{" "}
              <code>ssh://</code> for token auth.
            </span>
            {duplicate && (
              <span
                className="field-hint"
                style={{ color: "var(--amber)" }}
              >
                This repository is already registered for the selected
                org (<code>{duplicate.repo_id}</code>, status{" "}
                <code>{duplicate.status}</code>). Submitting will
                return the existing <code>repo_id</code> unchanged;
                no re-clone will run.
              </span>
            )}
          </label>

          <label className="field">
            <span className="field-label">Branch</span>
            <input
              type="text"
              value={branch}
              onChange={(e) => setBranch(e.target.value)}
              placeholder="main"
              disabled={submitting}
              autoComplete="off"
              spellCheck={false}
            />
            {duplicate && branch.trim() && branch.trim() !== duplicate.branch && (
              <span className="field-hint" style={{ color: "var(--amber)" }}>
                The existing row tracks branch{" "}
                <code>{duplicate.branch}</code>. Submitting will update
                it to <code>{branch.trim()}</code> but will not
                re-trigger ingestion automatically — use the Repo List
                page to process the repo.
              </span>
            )}
          </label>

          <ErrorBanner error={submitError} />

          <div className="actions">
            <button type="submit" className="btn btn-primary" disabled={submitting}>
              {submitting
                ? duplicate
                  ? "Reusing…"
                  : "Registering…"
                : duplicate
                  ? "Reuse existing repo"
                  : "Register repo"}
            </button>
          </div>
        </form>
      )}

      {result && (
        <div className="result">
          <div className="result-title">
            {result.reused
              ? "Repo already registered — reusing existing id"
              : "Registered"}
          </div>
          <dl className="kv">
            <dt>repo_id</dt>
            <dd>
              <code>{result.repo_id}</code>
            </dd>
            <dt>repo_url</dt>
            <dd>
              <code>{result.repo_url}</code>
            </dd>
            <dt>branch</dt>
            <dd>
              <code>{result.branch}</code>
            </dd>
            <dt>status</dt>
            <dd>
              <code>{result.status}</code>
            </dd>
          </dl>
          {result.reused && (
            <p className="muted small">
              The existing row was returned unchanged. To re-run
              ingestion (for example, after a code push), use{" "}
              <strong>Process</strong> on the Repo List page.
            </p>
          )}
          <div className="actions">
            <button type="button" className="btn" onClick={() => nav("/repos")}>
              View in Repo List →
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
