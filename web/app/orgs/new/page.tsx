"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import { ErrorBanner } from "@/components/ErrorBanner";
import {
  ApiError,
  Org,
  OrgCreateResponse,
  OrgTokenUpdateResponse,
  createOrg,
  listOrgs,
  normalizeOrgName,
  updateOrgToken,
} from "@/lib/api";

export default function OrgSetupPage() {
  const router = useRouter();

  const [name, setName] = useState("");
  const [token, setToken] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [result, setResult] = useState<OrgCreateResponse | null>(null);

  const [orgs, setOrgs] = useState<Org[] | null>(null);

  const [rotateOrgId, setRotateOrgId] = useState("");
  const [rotateToken, setRotateToken] = useState("");
  const [rotating, setRotating] = useState(false);
  const [rotateError, setRotateError] = useState<ApiError | null>(null);
  const [rotateResult, setRotateResult] = useState<OrgTokenUpdateResponse | null>(null);

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
        setRotateOrgId(pick);
      } catch {
        // non-fatal
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const existingMatch = useMemo<Org | null>(() => {
    if (!orgs || !name.trim()) return null;
    const needle = normalizeOrgName(name);
    return orgs.find((o) => normalizeOrgName(o.name) === needle) ?? null;
  }, [orgs, name]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    setResult(null);
    try {
      const created = await createOrg({ name: name.trim(), gitlab_token: token });
      setResult(created);
      localStorage.setItem("last_org_id", created.org_id);
      if (!created.reused) {
        setName("");
      }
      setToken("");
      try {
        const rows = await listOrgs();
        setOrgs(rows);
      } catch {
        // non-fatal
      }
    } catch (e) {
      setError(e instanceof ApiError ? e : new ApiError("request failed", 0, null));
    } finally {
      setSubmitting(false);
    }
  }

  async function onRotate(e: FormEvent) {
    e.preventDefault();
    if (!rotateOrgId) return;
    setRotating(true);
    setRotateError(null);
    setRotateResult(null);
    try {
      const res = await updateOrgToken(rotateOrgId, rotateToken);
      setRotateResult(res);
      setRotateToken("");
    } catch (e) {
      setRotateError(e instanceof ApiError ? e : new ApiError("request failed", 0, null));
    } finally {
      setRotating(false);
    }
  }

  return (
    <main className="page wide">
      <section className="card">
        <header className="card-header">
          <h1>Create organization</h1>
          <p className="muted">
            Registers an org in DynamoDB and stores its GitLab token. Creating
            with a name that already exists updates the token and returns the
            existing <code>org_id</code>.
          </p>
        </header>

        <form onSubmit={onSubmit} className="form">
          <label className="field">
            <span className="field-label">Organization name</span>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="acme"
              required
              minLength={1}
              maxLength={200}
              disabled={submitting}
              autoFocus
            />
            {existingMatch ? (
              <span className="field-hint" style={{ color: "var(--amber, #b8860b)" }}>
                An org named <strong>{existingMatch.name}</strong> already exists (
                <code>{existingMatch.org_id}</code>). Submitting will rotate its
                token and reuse that <code>org_id</code>.
              </span>
            ) : null}
          </label>

          <label className="field">
            <span className="field-label">GitHub / GitLab token</span>
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="ghp_… / glpat-…"
              required
              minLength={1}
              maxLength={500}
              disabled={submitting}
              autoComplete="off"
              spellCheck={false}
            />
            <span className="field-hint">Token with repository read access.</span>
          </label>

          <ErrorBanner error={error} />

          <div className="actions">
            <button type="submit" className="btn btn-primary" disabled={submitting}>
              {submitting
                ? existingMatch
                  ? "Rotating…"
                  : "Creating…"
                : existingMatch
                  ? "Rotate token on existing org"
                  : "Create org"}
            </button>
          </div>
        </form>

        {result ? (
          <div className="result">
            <div className="result-title">
              {result.reused ? "Org already existed — token rotated" : "Created"}
            </div>
            <dl className="kv">
              <dt>org_id</dt>
              <dd>
                <code>{result.org_id}</code>
              </dd>
              <dt>name</dt>
              <dd>{result.name}</dd>
              <dt>secret_name</dt>
              <dd>
                <code>{result.secret_name}</code>
              </dd>
            </dl>
            {result.reused ? (
              <p className="muted small">
                The stored token was updated. Workers pick up the new value on
                their next read.
              </p>
            ) : null}
            <div className="actions">
              <button
                type="button"
                className="btn"
                onClick={() => router.push("/repos/new")}
              >
                Register a repo for this org →
              </button>
            </div>
          </div>
        ) : null}

        {orgs && orgs.length > 0 ? (
          <>
            <hr className="card-divider" />
            <header className="card-header flat">
              <h2 className="h2-inline">Rotate GitLab token</h2>
              <p className="muted">
                Update the token for an existing org without re-creating it.
              </p>
            </header>

            <form onSubmit={onRotate} className="form">
              <label className="field">
                <span className="field-label">Organization</span>
                <select
                  value={rotateOrgId}
                  onChange={(e) => setRotateOrgId(e.target.value)}
                  required
                  disabled={rotating}
                >
                  {orgs.map((o) => (
                    <option key={o.org_id} value={o.org_id}>
                      {o.name} ({o.org_id})
                    </option>
                  ))}
                </select>
              </label>

              <label className="field">
                <span className="field-label">New token</span>
                <input
                  type="password"
                  value={rotateToken}
                  onChange={(e) => setRotateToken(e.target.value)}
                  placeholder="ghp_… / glpat-…"
                  required
                  minLength={1}
                  maxLength={500}
                  disabled={rotating}
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>

              <ErrorBanner error={rotateError} />

              <div className="actions">
                <button
                  type="submit"
                  className="btn btn-primary"
                  disabled={rotating || !rotateOrgId}
                >
                  {rotating ? "Rotating…" : "Rotate token"}
                </button>
              </div>
            </form>

            {rotateResult ? (
              <div className="result">
                <div className="result-title">Token rotated</div>
                <dl className="kv">
                  <dt>org_id</dt>
                  <dd>
                    <code>{rotateResult.org_id}</code>
                  </dd>
                  <dt>secret_name</dt>
                  <dd>
                    <code>{rotateResult.secret_name}</code>
                  </dd>
                  <dt>rotated_at</dt>
                  <dd className="small muted">
                    {new Date(rotateResult.rotated_at).toLocaleString()}
                  </dd>
                </dl>
              </div>
            ) : null}
          </>
        ) : null}
      </section>
    </main>
  );
}
