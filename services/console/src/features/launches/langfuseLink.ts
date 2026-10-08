/**
 * Shared Langfuse link presentation rules.
 *
 * A launch link is only rendered when the URL is a browser-safe absolute
 * http(s) address. Otherwise the UI states the concrete reason instead of
 * showing a dead or unsafe external link.
 */

export interface LangfuseLinkLaunch {
  id?: string;
  status?: string | null;
  langfuse_sync_status?: string | null;
  langfuse_experiment_id?: string | null;
  langfuse_experiment_url?: string | null;
  manifest?: { dataset?: { source?: string | null } | null } | null;
}

export interface LangfuseLinkTarget {
  kind: "link";
  href: string;
  /** Compact label for table cells. */
  label: string;
  /** Verbose label for the detail header. */
  detailLabel: string;
  title: string;
}

export interface LangfuseLinkReason {
  kind: "reason";
  label: string;
  title: string;
}

export type LangfuseLinkView = LangfuseLinkTarget | LangfuseLinkReason;

const REASONS = {
  notCreated: "尚未创建 Langfuse Run",
  syncing: "Langfuse 同步中",
  failed: "Langfuse 同步失败",
  notApplicable: "Langfuse 同步不适用",
  syncedNoLink: "已同步，链接暂不可用",
  noRunInfo: "未取得 Langfuse Run 信息",
  seedNoRun: "未创建 Langfuse Run",
  unavailable: "Langfuse 链接暂不可用",
  invalidUrl: "Langfuse 地址无效",
} as const;

const reason = (label: string, title: string): LangfuseLinkReason => ({
  kind: "reason",
  label,
  title,
});

/**
 * Accepts only absolute http(s) URLs without credentials, control characters,
 * backslashes or surrounding whitespace. Query and hash are allowed.
 */
export const isSafeLangfuseUrl = (value: unknown): value is string => {
  if (typeof value !== "string") return false;
  if (!value || value !== value.trim()) return false;
  for (const char of value) {
    const code = char.codePointAt(0) ?? 0;
    if (code < 0x20 || code === 0x7f) return false;
    if (char === "\\") return false;
  }
  try {
    const url = new URL(value);
    return (
      (url.protocol === "http:" || url.protocol === "https:") &&
      Boolean(url.hostname) &&
      !url.username &&
      !url.password
    );
  } catch {
    return false;
  }
};

const normalized = (value: unknown): string =>
  typeof value === "string" ? value.toUpperCase() : "";

const isSeedDataset = (launch: LangfuseLinkLaunch): boolean =>
  (launch.manifest?.dataset?.source ?? "").toLowerCase() === "seed";

/**
 * Resolves the Langfuse cell/header state. A safe URL always wins; otherwise the
 * most specific reason is reported. A seed dataset is not a blanket opt-out: a
 * real remote run still yields a valid link.
 */
export const getLangfuseLinkView = (launch: LangfuseLinkLaunch): LangfuseLinkView => {
  const url = launch.langfuse_experiment_url;
  if (isSafeLangfuseUrl(url)) {
    return {
      kind: "link",
      href: url,
      label: "Langfuse",
      detailLabel: "在 Langfuse 中查看",
      title: "在 Langfuse UI 中查看",
    };
  }
  if (url) {
    return reason(REASONS.invalidUrl, "Langfuse 地址无效");
  }

  const status = normalized(launch.status);
  const sync = normalized(launch.langfuse_sync_status);
  const runId = (launch.langfuse_experiment_id ?? "").trim();
  const started = status !== "" && status !== "PENDING" && status !== "QUEUED";
  const seed = isSeedDataset(launch);

  if (!runId && !started) {
    return reason(REASONS.notCreated, "尚未创建 Langfuse Run");
  }
  if (sync === "PENDING" || sync === "SYNCING") {
    return reason(REASONS.syncing, "Langfuse 同步中");
  }
  if (sync === "FAILED") {
    return reason(REASONS.failed, "Langfuse 同步失败");
  }
  if (seed && !runId) {
    return reason(REASONS.seedNoRun, "未创建 Langfuse Run");
  }
  if (sync === "NOT_APPLICABLE") {
    return reason(REASONS.notApplicable, "Langfuse 同步不适用");
  }
  if (sync === "SYNCED" && runId) {
    return reason(REASONS.syncedNoLink, "已同步，链接暂不可用");
  }
  if (sync === "SYNCED" && !runId) {
    return reason(REASONS.noRunInfo, "未取得 Langfuse Run 信息");
  }
  return reason(REASONS.unavailable, "Langfuse 链接暂不可用");
};
