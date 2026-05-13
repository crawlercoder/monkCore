import type { JobStatus } from "@/lib/api";

const LABEL: Record<JobStatus, string> = {
  CREATED: "Created",
  PROCESSING: "Processing",
  COMPLETED: "Completed",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
};

export function JobStatusPill({ status }: { status: JobStatus }) {
  const k = status.toLowerCase();
  return (
    <span className={`job-pill job-pill--${k}`}>
      <span className="job-pill-dot" aria-hidden />
      {LABEL[status]}
    </span>
  );
}
