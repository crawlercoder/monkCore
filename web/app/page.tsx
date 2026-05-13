"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import {
  ApiError,
  Org,
  Repo,
  canonicalGitUrl,
  createJob,
  createOrg,
  listOrgs,
  listReposForOrg,
  normalizeOrgName,
  registerRepo,
} from "@/lib/api";
import { pushRecentJobId, readRecentJobIds } from "@/lib/recent-jobs";

const LAST_ORG_KEY = "last_org_id";

type HomeMode = "mr" | "onboarding";

function formatOrgOptionLabel(o: Org, all: Org[]): string {
  const base = o.name.trim() || o.org_id;
  const n = normalizeOrgName(o.name);
  const sameName = all.filter((x) => normalizeOrgName(x.name) === n).length;
  return sameName > 1 ? `${base} — ${o.org_id}` : base;
}

export default function HomePage() {
  const router = useRouter();

  const [mode, setMode] = useState<HomeMode>("mr");
  const [orgs, setOrgs] = useState<Org[]>([]);
  const [orgsLoading, setOrgsLoading] = useState(true);
  const [orgsError, setOrgsError] = useState<string | null>(null);

  const [recentJobIds, setRecentJobIds] = useState<string[]>([]);
  const [jobGoto, setJobGoto] = useState("");

  const [spec, setSpec] = useState("");
  const [selectedOrgId, setSelectedOrgId] = useState("");
  const [jobSubmitting, setJobSubmitting] = useState(false);
  const [jobError, setJobError] = useState<string | null>(null);

  const [obOrgName, setObOrgName] = useState("");
  const [obToken, setObToken] = useState("");
  const [obOrgSubmitting, setObOrgSubmitting] = useState(false);
  const [obOrgErr, setObOrgErr] = useState<string | null>(null);
  const [obOrgMsg, setObOrgMsg] = useState<string | null>(null);

  const [obRepoOrgId, setObRepoOrgId] = useState("");
  const [obRepoUrl, setObRepoUrl] = useState("");
  const [obBranch, setObBranch] = useState("main");
  const [obReposHint, setObReposHint] = useState<Repo[]>([]);
  const [obRepoSubmitting, setObRepoSubmitting] = useState(false);
  const [obRepoErr, setObRepoErr] = useState<string | null>(null);
  const [obRepoMsg, setObRepoMsg] = useState<string | null>(null);

  const refreshRecentJobs = useCallback(() => {
    setRecentJobIds(readRecentJobIds());
  }, []);

  useEffect(() => {
    refreshRecentJobs();
  }, [refreshRecentJobs]);

  const loadOrgs = useCallback(async () => {
    setOrgsError(null);
    setOrgsLoading(true);
    try {
      const rows = await listOrgs();
      if (!Array.isArray(rows)) {
        setOrgsError("Unexpected response from server for organizations list.");
        setOrgs([]);
        return;
      }
      setOrgs(rows);
      if (typeof window === "undefined") return;
      const last = localStorage.getItem(LAST_ORG_KEY);
      const fromLast = rows.find((o) => o.org_id === last)?.org_id;
      const first = rows[0]?.org_id ?? "";
      const pick = fromLast ?? first;
      setSelectedOrgId((cur) => {
        if (cur && rows.some((o) => o.org_id === cur)) return cur;
        return pick;
      });
      setObRepoOrgId((cur) => {
        if (cur && rows.some((o) => o.org_id === cur)) return cur;
        return fromLast ?? first;
      });
    } catch (e) {
      setOrgsError(e instanceof ApiError ? e.message : "Could not list orgs");
      setOrgs([]);
    } finally {
      setOrgsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadOrgs();
  }, [loadOrgs]);

  const dupNameHint = useMemo(() => {
    if (!orgs.length || !obOrgName.trim()) return null;
    const needle = normalizeOrgName(obOrgName);
    return orgs.find((o) => normalizeOrgName(o.name) === needle) ?? null;
  }, [orgs, obOrgName]);

  useEffect(() => {
    if (!obRepoOrgId) {
      setObReposHint([]);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const r = await listReposForOrg(obRepoOrgId);
        if (!cancelled) setObReposHint(r);
      } catch {
        if (!cancelled) setObReposHint([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [obRepoOrgId]);

  const dupRepoHint = useMemo(() => {
    if (!obReposHint.length || !obRepoUrl.trim()) return null;
    const needle = canonicalGitUrl(obRepoUrl);
    if (!needle) return null;
    return obReposHint.find((r) => canonicalGitUrl(r.repo_url) === needle) ?? null;
  }, [obReposHint, obRepoUrl]);

  const canJob = Boolean(
    selectedOrgId && spec.trim() && !jobSubmitting && !orgsLoading,
  );
  const canObOrg = Boolean(obOrgName.trim() && obToken.trim() && !obOrgSubmitting);
  const canObRepo = Boolean(obRepoOrgId && obRepoUrl.trim() && !obRepoSubmitting);

  const onJob = async (e: FormEvent) => {
    e.preventDefault();
    if (!canJob) return;
    setJobSubmitting(true);
    setJobError(null);
    try {
      const res = await createJob({ org_id: selectedOrgId, spec: spec.trim() });
      if (!res.job_id) throw new Error("No job_id in response");
      localStorage.setItem(LAST_ORG_KEY, selectedOrgId);
      pushRecentJobId(res.job_id);
      setRecentJobIds(readRecentJobIds());
      router.push(`/job/${encodeURIComponent(res.job_id)}`);
    } catch (err) {
      setJobError(
        err instanceof ApiError
          ? `${err.status}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "request failed",
      );
      setJobSubmitting(false);
    }
  };

  const onGotoJob = (e: FormEvent) => {
    e.preventDefault();
    const id = jobGoto.trim();
    if (!id) return;
    router.push(`/job/${encodeURIComponent(id)}`);
  };

  const onObCreateOrg = async (e: FormEvent) => {
    e.preventDefault();
    if (!canObOrg) return;
    setObOrgSubmitting(true);
    setObOrgErr(null);
    setObOrgMsg(null);
    try {
      const created = await createOrg({ name: obOrgName.trim(), gitlab_token: obToken });
      localStorage.setItem(LAST_ORG_KEY, created.org_id);
      setObOrgMsg(
        created.reused
          ? "Organization already existed — token updated."
          : "Organization created. Add a repository below.",
      );
      if (!created.reused) setObOrgName("");
      setObToken("");
      setSelectedOrgId(created.org_id);
      setObRepoOrgId(created.org_id);
      await loadOrgs();
    } catch (err) {
      setObOrgErr(
        err instanceof ApiError
          ? `${err.status}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "failed",
      );
    } finally {
      setObOrgSubmitting(false);
    }
  };

  const onObRegisterRepo = async (e: FormEvent) => {
    e.preventDefault();
    if (!canObRepo) return;
    setObRepoSubmitting(true);
    setObRepoErr(null);
    setObRepoMsg(null);
    try {
      const r = await registerRepo({
        repo_url: obRepoUrl.trim(),
        org_id: obRepoOrgId,
        branch: obBranch.trim() || "main",
      });
      setObRepoMsg(
        r.reused
          ? "Repository was already registered. Open Repos to run ingestion if needed."
          : "Repository registered. It will move to READY after ingestion.",
      );
      localStorage.setItem(LAST_ORG_KEY, r.org_id);
      if (!r.reused) setObRepoUrl("");
      try {
        setObReposHint(await listReposForOrg(obRepoOrgId));
      } catch {
        // ignore
      }
      await loadOrgs();
    } catch (err) {
      setObRepoErr(
        err instanceof ApiError
          ? `${err.status}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "failed",
      );
    } finally {
      setObRepoSubmitting(false);
    }
  };

  const orgSelectDisabled = jobSubmitting || orgsLoading;
  const noOrgsAfterLoad = !orgsLoading && orgs.length === 0;
  const noOrgHint =
    !orgsError && noOrgsAfterLoad
      ? "No organizations in this account yet — use Onboarding, or ensure this UI is pointed at the same API / DynamoDB as your data."
      : null;

  return (
    <main className="page home">
      <div className="home-head">
        <h1>What do you want to build?</h1>
        <p className="lead home-lead">
          Switch between a new merge request and first-time setup. Open{" "}
          <strong>Jobs</strong> to list every job for an organization, or{" "}
          <strong>Repos</strong> for clones and ingestion.
        </p>
      </div>

      <section className="jobs-panel" aria-label="Job status">
        <div className="jobs-panel-head">
          <h2 className="jobs-panel-title">Jobs</h2>
          {recentJobIds.length > 0 ? (
            <ul className="job-chip-list">
              {recentJobIds.map((id) => (
                <li key={id}>
                  <Link className="job-chip" href={`/job/${encodeURIComponent(id)}`} title={id}>
                    {id.length > 20 ? `${id.slice(0, 10)}…${id.slice(-6)}` : id}
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <p className="jobs-panel-empty">No recent jobs in this browser yet.</p>
          )}
        </div>
        <form className="job-goto-form" onSubmit={onGotoJob}>
          <label className="sr-only" htmlFor="job-goto">
            Open job by id
          </label>
          <input
            id="job-goto"
            className="job-goto-input"
            value={jobGoto}
            onChange={(e) => setJobGoto(e.target.value)}
            placeholder="Open job by id…"
            autoComplete="off"
          />
          <button type="submit" className="btn btn-secondary job-goto-btn" disabled={!jobGoto.trim()}>
            Open
          </button>
        </form>
      </section>

      <div
        className="mode-toggle"
        role="tablist"
        aria-label="Main mode"
      >
        <button
          type="button"
          role="tab"
          id="mode-mr"
          aria-selected={mode === "mr"}
          className={mode === "mr" ? "mode-tab is-active" : "mode-tab"}
          onClick={() => {
            setMode("mr");
          }}
        >
          New MR
        </button>
        <button
          type="button"
          role="tab"
          id="mode-onb"
          aria-selected={mode === "onboarding"}
          className={mode === "onboarding" ? "mode-tab is-active" : "mode-tab"}
          onClick={() => {
            setMode("onboarding");
          }}
        >
          Onboarding
        </button>
      </div>

      {orgsError ? (
        <p className="error inline-warn" role="status">
          <strong>Organizations</strong> could not be loaded: {orgsError}
        </p>
      ) : null}

      {noOrgHint && mode === "mr" ? (
        <p className="hint inline-warn-muted" role="status">
          {noOrgHint}
        </p>
      ) : null}

      {mode === "mr" ? (
        <section
          className="section main-flow composer-panel"
          aria-labelledby="main-heading"
        >
          <h2 id="main-heading" className="sr-only">
            New merge request
          </h2>
          <form className="stack composer-form" onSubmit={onJob} noValidate>
            <label className="field-spec">
              <span className="field-label-outer">Spec</span>
              <textarea
                name="spec"
                className="input-spec"
                value={spec}
                onChange={(e) => setSpec(e.target.value)}
                disabled={jobSubmitting}
                rows={10}
                placeholder="e.g. Add a health check endpoint that returns JSON with service name and build id…"
                required
              />
            </label>
            <div className="composer-footer">
              <label className="field-inline-tight field-org-select">
                <span className="field-label-outer">Organization</span>
                <select
                  className="input-org select-input"
                  value={orgsLoading ? "" : selectedOrgId}
                  onChange={(e) => setSelectedOrgId(e.target.value)}
                  disabled={orgSelectDisabled || (noOrgsAfterLoad && !orgsLoading)}
                  required
                >
                  {orgsLoading ? (
                    <option value="">Loading organizations…</option>
                  ) : orgs.length === 0 ? (
                    <option value="">
                      No organizations — switch to Onboarding or fix API connection
                    </option>
                  ) : (
                    orgs.map((o) => (
                      <option key={o.org_id} value={o.org_id}>
                        {formatOrgOptionLabel(o, orgs)}
                      </option>
                    ))
                  )}
                </select>
              </label>
              <button
                type="submit"
                className="primary btn-send"
                disabled={!canJob || noOrgsAfterLoad}
              >
                {jobSubmitting ? "Running…" : "Generate MR"}
              </button>
            </div>
            {jobError ? (
              <div className="error composer-error" role="alert">
                {jobError}
              </div>
            ) : null}
          </form>
        </section>
      ) : (
        <section className="onboarding-panel composer-panel" aria-labelledby="onb-heading">
          <h2 id="onb-heading" className="sr-only">
            Onboarding
          </h2>
          <p className="onboarding-intro">
            Create an organization (GitLab token), then register at least one repository URL
            for your team.
          </p>

          <form className="onb-block" onSubmit={onObCreateOrg} noValidate>
            <h3 className="onb-step-title">1. Organization</h3>
            <div className="onb-grid">
              <label className="onb-field">
                <span>Name</span>
                <input
                  type="text"
                  value={obOrgName}
                  onChange={(e) => setObOrgName(e.target.value)}
                  disabled={obOrgSubmitting}
                  placeholder="e.g. acme"
                  autoComplete="off"
                />
              </label>
              {dupNameHint ? (
                <p className="onb-hint-warn">
                  An org with this name exists — saving will update its token:{" "}
                  <code>{dupNameHint.org_id}</code>
                </p>
              ) : null}
              <label className="onb-field">
                <span>GitLab / GitHub token</span>
                <input
                  type="password"
                  value={obToken}
                  onChange={(e) => setObToken(e.target.value)}
                  disabled={obOrgSubmitting}
                  placeholder="glpat-… / ghp_…"
                  autoComplete="off"
                />
              </label>
            </div>
            {obOrgErr ? <div className="error onb-err">{obOrgErr}</div> : null}
            {obOrgMsg ? <p className="ok-msg onb-ok">{obOrgMsg}</p> : null}
            <button
              type="submit"
              className="primary onb-btn"
              disabled={!canObOrg}
            >
              {obOrgSubmitting
                ? "Saving…"
                : dupNameHint
                  ? "Update org & token"
                  : "Create organization"}
            </button>
          </form>

          <div className="onb-divider" aria-hidden />

          <form className="onb-block" onSubmit={onObRegisterRepo} noValidate>
            <h3 className="onb-step-title">2. Repository</h3>
            {orgs.length === 0 && !orgsLoading ? (
              <p className="onb-hint-muted">Create an organization above first (or load orgs from the API).</p>
            ) : null}
            {orgsLoading ? (
              <p className="onb-hint-muted">Loading organizations…</p>
            ) : null}
            <div className="onb-grid">
              <label className="onb-field">
                <span>Organization</span>
                <select
                  className="select-input"
                  value={obRepoOrgId}
                  onChange={(e) => setObRepoOrgId(e.target.value)}
                  disabled={obRepoSubmitting || orgs.length === 0 || orgsLoading}
                >
                  {orgs.length === 0 ? (
                    <option value="">No organizations — complete step 1</option>
                  ) : (
                    orgs.map((o) => (
                      <option key={o.org_id} value={o.org_id}>
                        {formatOrgOptionLabel(o, orgs)}
                      </option>
                    ))
                  )}
                </select>
              </label>
              <label className="onb-field">
                <span>Repository URL</span>
                <input
                  type="text"
                  value={obRepoUrl}
                  onChange={(e) => setObRepoUrl(e.target.value)}
                  disabled={obRepoSubmitting}
                  placeholder="https://gitlab.com/…"
                  autoComplete="off"
                />
              </label>
              {dupRepoHint ? (
                <p className="onb-hint-warn">
                  Already registered: <code>{dupRepoHint.repo_id}</code> ({dupRepoHint.status})
                </p>
              ) : null}
              <label className="onb-field">
                <span>Branch</span>
                <input
                  type="text"
                  value={obBranch}
                  onChange={(e) => setObBranch(e.target.value)}
                  disabled={obRepoSubmitting}
                  placeholder="main"
                />
              </label>
            </div>
            {obRepoErr ? <div className="error onb-err">{obRepoErr}</div> : null}
            {obRepoMsg ? <p className="ok-msg onb-ok">{obRepoMsg}</p> : null}
            <button
              type="submit"
              className="primary onb-btn"
              disabled={!canObRepo || orgs.length === 0}
            >
              {obRepoSubmitting ? "Registering…" : "Register repository"}
            </button>
            <p className="onb-footnote">
              Manage clones and <strong>Process</strong> from{" "}
              <Link href="/repos">Repositories</Link>.
            </p>
          </form>
        </section>
      )}
    </main>
  );
}
