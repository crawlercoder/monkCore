const KEY = "recent_job_ids_v1";
const MAX = 20;

export function readRecentJobIds(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((x): x is string => typeof x === "string" && x.length > 0).slice(0, MAX);
  } catch {
    return [];
  }
}

export function pushRecentJobId(jobId: string): void {
  if (typeof window === "undefined" || !jobId.trim()) return;
  const id = jobId.trim();
  const next = [id, ...readRecentJobIds().filter((x) => x !== id)].slice(0, MAX);
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    // ignore quota
  }
}
