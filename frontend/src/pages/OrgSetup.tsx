import { FormEvent, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  ApiError,
  Org,
  OrgCreateResponse,
  OrgTokenUpdateResponse,
  createOrg,
  listOrgs,
  normalizeOrgName,
  updateOrgToken,
} from "../api";
import { ErrorBanner } from "../components/ErrorBanner";

// Page 1: create an org (idempotent on name).
//
// Design notes:
//
// * The backend collapses duplicate-name creates into a token rotation
//   on the existing row and returns ``reused=true`` + 200 OK. The UI
//   reflects that: we show a distinct "already existed — token
//   rotated" banner instead of the green "Created" banner, so the
//   operator knows no new ``org_id`` was minted.
//
// * We also run a cheap client-side check (``normalizeOrgName`` against
//   a cached ``listOrgs``) so the user is warned *before* they submit
//   that this name is already taken. The backend remains the source of
//   truth; this is just UX.
//
// * A secondary "Rotate GitLab token" panel lets the user rotate a
//   token without first pretending to re-create the org. It calls
//   ``PUT /v1/orgs/{org_id}/token`` directly. This is strictly an
//   affordance — functionally it's the same as re-submitting the
//   create form with the same name.
export function OrgSetup() {
  const nav = useNavigate();

  // --- create-org state ---
  const [name, setName] = useState("");
  const [token, setToken] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [result, setResult] = useState<OrgCreateResponse | null>(null);

  // --- existing orgs (for dup-name hint + rotate panel) ---
  const [orgs, setOrgs] = useState<Org[] | null>(null);

  // --- rotate-token state ---
  const [rotateOrgId, setRotateOrgId] = useState("");
  const [rotateToken, setRotateToken] = useState("");
  const [rotating, setRotating] = useState(false);
  const [rotateError, setRotateError] = useState<ApiError | null>(null);
  const [rotateResult, setRotateResult] = useState<OrgTokenUpdateResponse | null>(
    null,
  );

  // Load orgs once for the dup-name hint and the rotate-token dropdown.
  // Failure here is non-fatal — the create flow still works, we just
  // lose the client-side warning.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const rows = await listOrgs();
        if (cancelled) return;
        setOrgs(rows);
        // Pre-select the most-recent org for the rotate panel.
        const last = localStorage.getItem("last_org_id");
        const pick =
          rows.find((o) => o.org_id === last)?.org_id ?? rows[0]?.org_id ?? "";
        setRotateOrgId(pick);
      } catch {
        // swallow
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // O(n) over a small list — tens of orgs at most, recomputed on every
  // keystroke is fine.
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
      // Keep the name visible when reused — the user may want to copy
      // the org_id; clear the token field either way.
      if (!created.reused) {
        setName("");
      }
      setToken("");
      // Refresh the cached orgs list so subsequent keystrokes show
      // the updated state without a manual reload.
      try {
        const rows = await listOrgs();
        setOrgs(rows);
      } catch {
        // non-fatal
      }
    } catch (e) {
      setError(e as ApiError);
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
      setRotateError(e as ApiError);
    } finally {
      setRotating(false);
    }
  }

  return (
    <section className="card">
      <header className="card-header">
        <h1>Create organization</h1>
        <p className="muted">
          Registers an org in DynamoDB and stores its GitLab token in AWS
          Secrets Manager. Creating an org with a name that already
          exists updates the stored token and returns the existing{" "}
          <code>org_id</code> — so every customer stays mapped to a
          single vector store.
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
          {existingMatch && (
            <span className="field-hint" style={{ color: "var(--amber)" }}>
              An org named <strong>{existingMatch.name}</strong> already
              exists (<code>{existingMatch.org_id}</code>). Submitting
              will rotate its GitLab token and reuse that{" "}
              <code>org_id</code> — no new org will be created.
            </span>
          )}
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
          <span className="field-hint">
            Token with repository read access (GitHub or GitLab).
          </span>
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

      {result && (
        <div className="result">
          <div className="result-title">
            {result.reused
              ? "Org already existed — token rotated"
              : "Created"}
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
          {result.reused && (
            <p className="muted small">
              The stored GitLab token was updated. Workers will pick up
              the new value on their next read (Secrets Manager cache
              TTL applies).
            </p>
          )}
          <div className="actions">
            <button
              type="button"
              className="btn"
              onClick={() => nav("/repos/new")}
            >
              Register a repo for this org →
            </button>
          </div>
        </div>
      )}

      {/* ----------------- Rotate-token panel ----------------- */}
      {orgs && orgs.length > 0 && (
        <>
          <hr style={{ margin: "24px 0", borderColor: "var(--border)" }} />
          <header className="card-header" style={{ marginTop: 0 }}>
            <h2 style={{ fontSize: "1.1rem", margin: 0 }}>
              Rotate GitLab token
            </h2>
            <p className="muted">
              Update the GitLab token for an existing organization
              without re-creating it. Takes effect immediately for new
              worker reads.
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

          {rotateResult && (
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
          )}
        </>
      )}
    </section>
  );
}
