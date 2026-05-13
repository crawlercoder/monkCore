import type { RepoStatus } from "../api";

// PENDING → CLONING → READY map to amber / blue / green respectively.
// Colours come from CSS variables so the rest of the app can theme.
const LABEL: Record<RepoStatus, string> = {
  PENDING: "Pending",
  CLONING: "Cloning",
  READY: "Ready",
};

export function StatusPill({ status }: { status: RepoStatus }) {
  return (
    <span className={`pill pill-${status.toLowerCase()}`}>
      <span className="pill-dot" aria-hidden />
      {LABEL[status]}
    </span>
  );
}
