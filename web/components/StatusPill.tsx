import type { RepoStatus } from "@/lib/api";

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
