import type { ApiError } from "@/lib/api";

/** Renders structured API failures (``error.code``, ``details.error_type`` when present). */
export function ErrorBanner({ error }: { error: ApiError | null }) {
  if (!error) return null;
  const errorType =
    error.details && typeof error.details === "object" && "error_type" in error.details
      ? String((error.details as { error_type?: unknown }).error_type)
      : null;

  return (
    <div role="alert" className="error-banner">
      {error.code ? (
        <div className="error-banner-title">
          {error.code}{" "}
          <span className="error-banner-meta">· HTTP {error.status}</span>
        </div>
      ) : (
        <div className="error-banner-title">HTTP {error.status}</div>
      )}
      <div>{error.message}</div>
      {errorType ? (
        <div className="err-detail">
          type: <code>{errorType}</code>
        </div>
      ) : null}
      {error.requestId ? (
        <div className="err-detail">
          request id: <code>{error.requestId}</code>
        </div>
      ) : null}
    </div>
  );
}
