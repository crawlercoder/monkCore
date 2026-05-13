import type { ApiError } from "../api";

// Single spot to render errors from the API. We prefer the structured
// fields over `err.message` because they reveal *why* it failed — e.g.
// `error_type=GitAuthError` lets the operator know the token is broken
// without reading logs.
export function ErrorBanner({ error }: { error: ApiError | null }) {
  if (!error) return null;
  const errorType = (error.details?.error_type as string | undefined) ?? null;

  return (
    <div role="alert" className="error-banner">
      <div className="error-banner-title">
        {error.code} <span className="muted small">· HTTP {error.status}</span>
      </div>
      <div>{error.message}</div>
      {errorType && (
        <div className="small muted">
          type: <code>{errorType}</code>
        </div>
      )}
      {error.requestId && (
        <div className="small muted">
          request id: <code>{error.requestId}</code>
        </div>
      )}
    </div>
  );
}
