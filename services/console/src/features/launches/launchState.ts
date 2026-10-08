export const LAUNCH_STATUSES = [
  "PENDING",
  "QUEUED",
  "RUNNING",
  "COMPLETED",
  "PARTIAL_FAILED",
  "FAILED",
  "CANCELLING",
  "CANCELLED",
] as const;

export type LaunchStatus = (typeof LAUNCH_STATUSES)[number];

export const ACTIVE_LAUNCH_STATUSES = new Set<LaunchStatus>([
  "PENDING",
  "QUEUED",
  "RUNNING",
  "CANCELLING",
]);

export const TERMINAL_LAUNCH_STATUSES = new Set<LaunchStatus>([
  "COMPLETED",
  "PARTIAL_FAILED",
  "FAILED",
  "CANCELLED",
]);

export const LAUNCH_STATUS_LABELS: Record<LaunchStatus, string> = {
  PENDING: "PENDING (准备中)",
  QUEUED: "QUEUED (队列中)",
  RUNNING: "RUNNING (运行中)",
  COMPLETED: "COMPLETED (已完成)",
  PARTIAL_FAILED: "PARTIAL_FAILED (部分失败)",
  FAILED: "FAILED (失败)",
  CANCELLING: "CANCELLING (取消中)",
  CANCELLED: "CANCELLED (已取消)",
};

export const SYNC_ACTIVE_STATUSES = new Set(["PENDING", "SYNCING"]);

export const normalizeLaunchStatus = (value: unknown): LaunchStatus | "" => {
  const normalized = typeof value === "string" ? value.toUpperCase() : "";
  return (LAUNCH_STATUSES as readonly string[]).includes(normalized)
    ? (normalized as LaunchStatus)
    : "";
};

export const normalizeSyncStatus = (value: unknown): string =>
  typeof value === "string" ? value.toUpperCase() : "";

export const isLaunchExecutionActive = (status: unknown): boolean => {
  const normalized = normalizeLaunchStatus(status);
  return normalized !== "" && ACTIVE_LAUNCH_STATUSES.has(normalized);
};

export const isLaunchSyncActive = (syncStatus: unknown): boolean =>
  SYNC_ACTIVE_STATUSES.has(normalizeSyncStatus(syncStatus));
