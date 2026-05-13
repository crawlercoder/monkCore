// Thin typed wrapper around the backend's /api/v1 API (app mounts the router
// at prefix /api; see app/main.py and app/api/router.py).
//
// The backend returns errors in a uniform envelope:
//     { error: { code, message, details, request_id } }
// We surface `ApiError` with those fields preserved so components can
// distinguish "repo not found" (code === "not_found") from "git auth
// failed" (details.error_type === "GitAuthError") without parsing free-
// form strings.

export type RepoStatus = "PENDING" | "CLONING" | "READY";

export interface Org {
  org_id: string;
  name: string;
  secret_name: string;
  created_at: string;
  updated_at: string;
}

export interface Repo {
  repo_id: string;
  repo_url: string;
  org_id: string;
  branch: string;
  status: RepoStatus;
  created_at: string;
  updated_at: string;
}

export interface OrgCreateResponse {
  org_id: string;
  name: string;
  secret_name: string;
  created_at: string;
  // True when the backend found an org with the same (case-insensitive,
  // trimmed) name and reused it — the request was treated as a token
  // rotation instead of a create. HTTP status in that case is 200 OK
  // (201 Created otherwise); the flag carries the same signal for UI
  // code that doesn't branch on status.
  reused?: boolean;
}

export interface OrgTokenUpdateResponse {
  org_id: string;
  secret_name: string;
  rotated_at: string;
}

// `POST /repo/register` returns the repo row plus a ``reused`` flag
// indicating the request collapsed into an existing row (same
// canonical URL under the same org) instead of minting a new one.
export interface RepoRegisterResponse extends Repo {
  reused?: boolean;
}

export interface ErrorEnvelope {
  code: string;
  message: string;
  details?: Record<string, unknown>;
  request_id?: string;
}

export class ApiError extends Error {
  status: number;
  code: string;
  details: Record<string, unknown>;
  requestId?: string;

  constructor(status: number, env: ErrorEnvelope) {
    super(env.message || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = env.code;
    this.details = env.details ?? {};
    this.requestId = env.request_id;
  }
}

// In dev we always use same-origin + Vite proxy (see ``vite.config.ts``) so the
// browser never opens a second connection to :8000 (avoids ERR_CONNECTION_RESET
// and CORS). ``VITE_API_BASE_URL`` in .env is ignored in dev unless you set
// ``VITE_API_BYPASS_PROXY=1`` (then ``VITE_API_BASE_URL`` is used, or 127.0.0.1:8000).
const BASE_URL: string = (() => {
  const raw = import.meta.env.VITE_API_BASE_URL as string | undefined;
  const envBase = raw?.replace(/\/$/, "") ?? "";
  const dev = import.meta.env.DEV;
  const bypass = import.meta.env.VITE_API_BYPASS_PROXY === "1";
  if (dev && !bypass) {
    return "";
  }
  if (dev && bypass) {
    return envBase || "http://127.0.0.1:8000";
  }
  // Production: same-origin when behind nginx/ALB (one host for UI + /api).
  if (envBase) {
    return envBase;
  }
  return "";
})();

/** Versioned API under FastAPI (not at server root: see ``/api`` in main). */
const V1 = "/api/v1";

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const url = `${BASE_URL}${path}`;
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("Accept", "application/json");

  let resp: Response;
  try {
    resp = await fetch(url, { ...init, headers });
  } catch (e) {
    // Network-level failure (CORS miss, server down, etc.) — surface a
    // uniform ApiError so callers have one catch to write.
    throw new ApiError(0, {
      code: "network_error",
      message: e instanceof Error ? e.message : "Network error",
    });
  }

  if (resp.status === 204) {
    return undefined as T;
  }

  const text = await resp.text();
  const body = text ? (JSON.parse(text) as unknown) : null;

  if (!resp.ok) {
    const envelope =
      (body as { error?: ErrorEnvelope } | null)?.error ?? {
        code: "http_error",
        message: `HTTP ${resp.status}`,
      };
    throw new ApiError(resp.status, envelope);
  }

  return body as T;
}

// ---- Orgs --------------------------------------------------------------

export async function createOrg(input: {
  name: string;
  gitlab_token: string;
}): Promise<OrgCreateResponse> {
  return request(`${V1}/org/create`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export async function listOrgs(limit = 100): Promise<Org[]> {
  return request(`${V1}/orgs?limit=${limit}`);
}

export async function getOrg(orgId: string): Promise<Org> {
  return request(`${V1}/orgs/${encodeURIComponent(orgId)}`);
}

export async function updateOrgToken(
  orgId: string,
  gitlabToken: string,
): Promise<OrgTokenUpdateResponse> {
  return request(`${V1}/orgs/${encodeURIComponent(orgId)}/token`, {
    method: "PUT",
    body: JSON.stringify({ gitlab_token: gitlabToken }),
  });
}

// ---- Repos -------------------------------------------------------------

export async function registerRepo(input: {
  repo_url: string;
  org_id: string;
  branch?: string;
}): Promise<RepoRegisterResponse> {
  return request(`${V1}/repo/register`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export async function listReposForOrg(orgId: string): Promise<Repo[]> {
  return request(`${V1}/orgs/${encodeURIComponent(orgId)}/repos`);
}

export async function getRepo(repoId: string): Promise<Repo> {
  return request(`${V1}/repos/${encodeURIComponent(repoId)}`);
}

export async function processRepo(repoId: string): Promise<Repo> {
  return request(`${V1}/repo/process/${encodeURIComponent(repoId)}`, {
    method: "POST",
  });
}

// ---- Client-side helpers (match backend dedup keys) --------------------

/**
 * Client-side mirror of the backend's ``_canonical_git_url``. Used only
 * for optimistic UI hints — the server still does its own normalization
 * before hitting DynamoDB, so the two never need to agree bit-for-bit.
 * We just need "the same" inputs to produce "the same" key so we can
 * warn the user before they submit a duplicate.
 *
 * Collapses the three common ways GitLab/GitHub URLs are written:
 *   https://gitlab.com/acme/web.git
 *   https://user:token@gitlab.com/acme/web
 *   git@gitlab.com:acme/web.git
 * All three become: ssh://git@gitlab.com/acme/web (or
 * https://gitlab.com/acme/web).
 */
export function canonicalGitUrl(url: string): string {
  let raw = (url || "").trim();
  if (!raw) return raw;
  if (raw.endsWith(".git")) raw = raw.slice(0, -4);

  // git@host:group/proj → ssh://git@host/group/proj. URL() would
  // mis-parse the SCP-style colon as a port separator, so rewrite
  // first.
  if (!raw.includes("://")) {
    const at = raw.indexOf("@");
    if (at > 0) {
      const rest = raw.slice(at + 1);
      const colonIdx = rest.indexOf(":");
      if (colonIdx > 0) {
        const userhost = raw.slice(0, at + 1) + rest.slice(0, colonIdx);
        const path = rest.slice(colonIdx + 1).replace(/^\/+/, "");
        raw = `ssh://${userhost}/${path}`;
      }
    }
  }

  try {
    const u = new URL(raw);
    const scheme = u.protocol.replace(":", "").toLowerCase();
    const host = u.hostname.toLowerCase();
    const netloc = u.port ? `${host}:${u.port}` : host;
    const path = u.pathname.replace(/\/+$/, "").toLowerCase();
    return `${scheme}://${netloc}${path}`;
  } catch {
    return raw.toLowerCase().replace(/\/+$/, "");
  }
}

/** Normalize an org name the same way the backend does for matching. */
export function normalizeOrgName(name: string): string {
  return (name || "").trim().toLowerCase();
}
