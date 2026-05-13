/**
 * Client for the FastAPI app under ``/api/v1/…`` (Next rewrites ``/api/*`` in dev).
 */

export type JobStatus = "CREATED" | "PROCESSING" | "COMPLETED" | "FAILED" | "CANCELLED";
export type RepoStatus = "PENDING" | "CLONING" | "READY";

export interface JobCreateRequest {
  org_id: string;
  spec: string;
}

export interface JobCreateResponse {
  job_id: string;
  status: JobStatus;
}

export interface Job {
  job_id: string;
  org_id: string;
  spec: string;
  status: JobStatus;
  mr_url: string;
  staging_url: string;
  logs: string[];
  created_at: string;
}

/** Row from ``GET /v1/orgs/{org_id}/jobs`` (no log tail). */
export interface JobSummary {
  job_id: string;
  org_id: string;
  spec: string;
  status: JobStatus;
  mr_url: string;
  staging_url: string;
  created_at: string;
}

export interface Org {
  org_id: string;
  name: string;
  secret_name: string;
  created_at: string;
  updated_at: string;
}

export interface OrgCreateResponse {
  org_id: string;
  name: string;
  secret_name: string;
  created_at: string;
  reused?: boolean;
}

export interface OrgTokenUpdateResponse {
  org_id: string;
  secret_name: string;
  rotated_at: string;
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

export interface RepoRegisterResponse extends Repo {
  reused?: boolean;
}

function apiBase(): string {
  if (typeof window !== "undefined") return "";
  return process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000";
}

export class ApiError extends Error {
  status: number;
  body: unknown;
  code?: string;
  requestId?: string;
  details?: Record<string, unknown>;
  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    if (body && typeof body === "object" && body !== null && "error" in body) {
      const o = (body as { error?: Record<string, unknown> }).error;
      if (o && typeof o === "object") {
        if (typeof o.code === "string") this.code = o.code;
        if (typeof o.request_id === "string") this.requestId = o.request_id;
        if (o.details && typeof o.details === "object" && o.details !== null) {
          this.details = o.details as Record<string, unknown>;
        }
      }
    }
  }
}

async function parseJsonSafe(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function messageFromV1Error(body: unknown, status: number): string {
  if (body && typeof body === "object") {
    const o = body as { error?: { message?: string }; detail?: string | unknown[] };
    if (typeof o.error?.message === "string" && o.error.message) {
      return o.error.message;
    }
    if (typeof o.detail === "string" && o.detail) {
      return o.detail;
    }
  }
  return `HTTP ${status}`;
}

/**
 * All versioned API calls: ``/api/v1`` + path.
 * FastAPI :mod:`app.errors` uses ``{ "error": { "message" } }``; Pydantic uses ``detail``.
 */
export async function apiV1Request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const p = path.startsWith("/") ? path : `/${path}`;
  const url = `${apiBase()}/api/v1${p}`;
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("Accept", "application/json");
  const res = await fetch(url, { ...init, headers, cache: "no-store" });
  const body = await parseJsonSafe(res);
  if (!res.ok) {
    throw new ApiError(messageFromV1Error(body, res.status), res.status, body);
  }
  return body as T;
}

export async function createJob(
  payload: JobCreateRequest,
  opts: { signal?: AbortSignal } = {},
): Promise<JobCreateResponse> {
  return apiV1Request<JobCreateResponse>("/jobs", {
    method: "POST",
    body: JSON.stringify(payload),
    signal: opts.signal,
  });
}

export async function getJob(
  jobId: string,
  opts: { logLines?: number; signal?: AbortSignal } = {},
): Promise<Job> {
  const qs = opts.logLines != null ? `?log_lines=${opts.logLines}` : "";
  return apiV1Request<Job>(`/jobs/${encodeURIComponent(jobId)}${qs}`, {
    signal: opts.signal,
  });
}

export async function cancelJob(
  jobId: string,
  opts: { signal?: AbortSignal } = {},
): Promise<{ job_id: string; status: JobStatus }> {
  return apiV1Request<{ job_id: string; status: JobStatus }>(
    `/jobs/${encodeURIComponent(jobId)}/cancel`,
    { method: "POST", signal: opts.signal },
  );
}

export async function listJobsForOrg(
  orgId: string,
  opts: { limit?: number; signal?: AbortSignal } = {},
): Promise<JobSummary[]> {
  const lim = opts.limit != null && opts.limit > 0 ? `?limit=${opts.limit}` : "";
  return apiV1Request<JobSummary[]>(
    `/orgs/${encodeURIComponent(orgId)}/jobs${lim}`,
    { signal: opts.signal },
  );
}

// ---- Onboarding (same as Vite `frontend/src/api.ts`) --------------------

export async function createOrg(input: {
  name: string;
  gitlab_token: string;
}): Promise<OrgCreateResponse> {
  return apiV1Request<OrgCreateResponse>("/org/create", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/**
 * Return org rows from ``GET /v1/orgs``. Tolerate legacy/wrapped JSON shapes
 * (some gateways may alter the body).
 */
function normalizeOrgList(data: unknown): Org[] {
  if (data == null) return [];
  if (Array.isArray(data)) {
    return data as Org[];
  }
  if (typeof data === "object" && data !== null) {
    const o = data as Record<string, unknown>;
    for (const k of ["orgs", "items", "data", "results"] as const) {
      const v = o[k];
      if (Array.isArray(v)) {
        return v as Org[];
      }
    }
  }
  return [];
}

export async function listOrgs(limit = 100): Promise<Org[]> {
  const data = await apiV1Request<unknown>(`/orgs?limit=${limit}`);
  return normalizeOrgList(data);
}

export async function updateOrgToken(
  orgId: string,
  gitlabToken: string,
): Promise<OrgTokenUpdateResponse> {
  return apiV1Request<OrgTokenUpdateResponse>(
    `/orgs/${encodeURIComponent(orgId)}/token`,
    {
      method: "PUT",
      body: JSON.stringify({ gitlab_token: gitlabToken }),
    },
  );
}

export async function registerRepo(input: {
  repo_url: string;
  org_id: string;
  branch?: string;
}): Promise<RepoRegisterResponse> {
  return apiV1Request<RepoRegisterResponse>("/repo/register", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export async function listReposForOrg(orgId: string): Promise<Repo[]> {
  return apiV1Request<Repo[]>(`/orgs/${encodeURIComponent(orgId)}/repos`);
}

export async function processRepo(repoId: string): Promise<Repo> {
  return apiV1Request<Repo>(`/repo/process/${encodeURIComponent(repoId)}`, {
    method: "POST",
  });
}

/** Match backend / UI dedup: normalized key for “same” repo URL (hint only). */
export function canonicalGitUrl(url: string): string {
  let raw = (url || "").trim();
  if (!raw) return raw;
  if (raw.endsWith(".git")) raw = raw.slice(0, -4);
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

export function normalizeOrgName(name: string): string {
  return (name || "").trim().toLowerCase();
}
