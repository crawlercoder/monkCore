"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, cancelJob, getJob, type Job, type JobStatus } from "@/lib/api";

/** Poll window: 3–5 seconds per spec (use midpoint + small jitter). */
const POLL_MIN_MS = 3000;
const POLL_MAX_MS = 5000;

const TERMINAL: ReadonlySet<JobStatus> = new Set<JobStatus>([
  "COMPLETED",
  "FAILED",
  "CANCELLED",
]);

function nextPollDelayMs(): number {
  return POLL_MIN_MS + Math.random() * (POLL_MAX_MS - POLL_MIN_MS);
}

function isProcessing(s: JobStatus | null): boolean {
  return s === "CREATED" || s === "PROCESSING";
}

/**
 * ``/job/[job_id]`` — poll the backend and surface status; when the job
 * completes successfully, show MR and staging links.
 */
export default function JobDetailPage() {
  const params = useParams<{ job_id: string }>();
  const jobId = params?.job_id ?? "";

  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [killError, setKillError] = useState<string | null>(null);
  const [killLoading, setKillLoading] = useState(false);
  const [initialLoad, setInitialLoad] = useState(true);
  const stoppedRef = useRef(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const schedule = useCallback((fn: () => void) => {
    if (timeoutRef.current != null) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(fn, nextPollDelayMs());
  }, []);

  useEffect(() => {
    if (!jobId) return;
    stoppedRef.current = false;
    const ac = new AbortController();

    const tick = async () => {
      try {
        const j = await getJob(jobId, { logLines: 80, signal: ac.signal });
        if (stoppedRef.current) return;
        setJob(j);
        setError(null);
        setInitialLoad(false);
        if (TERMINAL.has(j.status)) return;
      } catch (err) {
        if (ac.signal.aborted) return;
        setInitialLoad(false);
        const msg =
          err instanceof ApiError
            ? `${err.status}: ${err.message}`
            : err instanceof Error
              ? err.message
              : "failed to load job";
        setError(msg);
      }
      if (!stoppedRef.current) {
        schedule(() => void tick());
      }
    };
    void tick();

    return () => {
      stoppedRef.current = true;
      if (timeoutRef.current != null) {
        clearTimeout(timeoutRef.current);
        timeoutRef.current = null;
      }
      ac.abort();
    };
  }, [jobId, schedule]);

  const onKillJob = useCallback(async () => {
    if (!jobId) return;
    setKillError(null);
    setKillLoading(true);
    try {
      await cancelJob(jobId);
      const j = await getJob(jobId, { logLines: 80 });
      setJob(j);
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `${err.status}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "failed to cancel job";
      setKillError(msg);
    } finally {
      setKillLoading(false);
    }
  }, [jobId]);

  const processing = job == null && initialLoad ? true : job != null && isProcessing(job.status);
  const completed = job?.status === "COMPLETED";
  const failed = job?.status === "FAILED";
  const cancelled = job?.status === "CANCELLED";

  return (
    <main className="page job-page">
      <h1>Job status</h1>
      <p className="subtle" title="job_id">
        {jobId}
      </p>

      {error ? (
        <div className="error" role="alert" aria-live="polite">
          {error}
        </div>
      ) : null}

      {job != null && (
        <section className="block" aria-live="polite">
          <h2>Status</h2>
          <p className="status-value">
            {processing ? (
              <>
                <span className="spinner" role="status" aria-label="Loading" />
                <span className="muted">{job.status}</span>
              </>
            ) : (
              <span
                className={
                  failed ? "danger" : cancelled ? "warn-strong" : "muted strong"
                }
              >
                {job.status}
              </span>
            )}
          </p>
          {processing ? (
            <p className="job-kill-wrap">
              <button
                type="button"
                className="btn btn-secondary btn-small"
                disabled={killLoading}
                onClick={() => void onKillJob()}
              >
                {killLoading ? "Stopping…" : "Stop job"}
              </button>
            </p>
          ) : null}
        </section>
      )}

      {killError ? (
        <div className="error" role="alert" aria-live="polite">
          {killError}
        </div>
      ) : null}

      {job == null && initialLoad && !error ? (
        <section className="block">
          <p className="status-value">
            <span className="spinner" role="status" aria-label="Loading" />
            <span className="muted">Loading…</span>
          </p>
        </section>
      ) : null}

      {completed && job ? (
        <section className="block" aria-label="Completed job links">
          <h2>Links</h2>
          <dl>
            <div className="result-pair">
              <dt>Merge request</dt>
              <dd>
                {job.mr_url ? (
                  <a href={job.mr_url} target="_blank" rel="noreferrer noopener">
                    {job.mr_url}
                  </a>
                ) : (
                  "—"
                )}
              </dd>
            </div>
            <div className="result-pair">
              <dt>Staging</dt>
              <dd>
                {job.staging_url ? (
                  <a href={job.staging_url} target="_blank" rel="noreferrer noopener">
                    {job.staging_url}
                  </a>
                ) : (
                  "—"
                )}
              </dd>
            </div>
          </dl>
        </section>
      ) : null}

      {job != null && job.logs && job.logs.length > 0 ? (
        <section className="block">
          <label className="logs" htmlFor="job-logs">
            Log (tail)
          </label>
          <textarea
            id="job-logs"
            readOnly
            value={job.logs.join("\n")}
            rows={8}
            className="logs-out"
            spellCheck={false}
          />
        </section>
      ) : null}

      <p className="nav-back job-nav-actions">
        <Link href="/jobs" className="btn btn-secondary btn-small">
          All jobs
        </Link>
        <Link href="/" className="btn btn-secondary btn-small">
          New MR
        </Link>
      </p>
    </main>
  );
}
