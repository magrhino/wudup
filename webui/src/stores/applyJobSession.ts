// webui/src/stores/applyJobSession.ts
// Transient apply-job recovery state kept in session storage. Every access is
// best-effort: the updates store keeps working in memory without storage.
const APPLY_JOB_STORAGE_KEY = "applyJobId";

export function readRememberedApplyJobId(): string {
  const storage = sessionStorageAvailable();
  try {
    return storage?.getItem(APPLY_JOB_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function readApplyRecoveryStorage<T>(key: string, fallback: T): T {
  try {
    const value = JSON.parse(sessionStorageAvailable()?.getItem(key) ?? "null");
    if (key === "applyJobRun") {
      return (value?.jobId === readRememberedApplyJobId() &&
        Number.isSafeInteger(value?.runId) && value.runId > 0 ? value.runId : fallback) as T;
    }
    return (Array.isArray(value) ? value.filter((entry) =>
      entry && typeof entry.jobId === "string" && entry.jobId &&
      (entry.runId === null || (Number.isSafeInteger(entry.runId) && entry.runId > 0)) &&
      typeof entry.acknowledged === "boolean") : fallback) as T;
  } catch {
    return fallback;
  }
}

export function writeApplyRecoveryStorage(key: string, value: unknown): void {
  try {
    sessionStorageAvailable()?.setItem(key, JSON.stringify(value));
  } catch {
    // Recovery remains available in memory if session storage is unavailable.
  }
}

export function writeRememberedApplyJobId(jobId: string): void {
  const storage = sessionStorageAvailable();
  try {
    storage?.setItem(APPLY_JOB_STORAGE_KEY, jobId);
  } catch {
    // Remembering a transient job id is best-effort.
  }
}

export function removeRememberedApplyJobId(): void {
  const storage = sessionStorageAvailable();
  try {
    storage?.removeItem(APPLY_JOB_STORAGE_KEY);
  } catch {
    // Remembering a transient job id is best-effort.
  }
}

function sessionStorageAvailable(): Storage | null {
  try {
    return "sessionStorage" in globalThis ? globalThis.sessionStorage : null;
  } catch {
    return null;
  }
}
