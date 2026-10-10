import React from "react";
import { useQuery } from "@tanstack/react-query";
import { Badge, type BadgeTone } from "../../components/Badge";
import { AlertCircle } from "lucide-react";
import { Button } from "../../components/ui/Primitives";
import { formatApiError } from "../../api/errors";
import {
  snapshotDetailQueryOptions,
  snapshotListQueryOptions,
} from "./launchSnapshotQueries";

/**
 * Issue #85 — the fixed result report.
 *
 * The product promise is that the report a user reads or shares is *one
 * specific revision*. Two facts must therefore never be blurred together:
 *
 *   1. the latest run state (which keeps moving), and
 *   2. the selected historical revision (which never moves).
 *
 * This panel always names the revision it is showing, says whether that
 * revision's evidence is complete enough to act on, and lets the user switch
 * revisions without ever implying that "latest" is what a shared link means.
 */

type Schema = import("../../api/schema").components["schemas"];

export type SnapshotRevision = Schema["ResultSnapshotRevisionResponse"];
export type SnapshotList = Schema["ResultSnapshotListResponse"];
export type SnapshotDetail = Schema["ResultSnapshotDetailResponse"];

/**
 * COMPLETE may back a formal decision; DIAGNOSTIC may only explain a failure.
 * `neutral` is deliberate: a diagnostic snapshot is not a product failure, it
 * is evidence that is simply not sufficient to sign anything.
 */
export const evidenceTone = (state?: string | null): BadgeTone =>
  (state || "").toUpperCase() === "COMPLETE" ? "pass" : "neutral";

export const evidenceLabel = (state?: string | null): string =>
  (state || "").toUpperCase() === "COMPLETE" ? "证据完整" : "诊断快照";

export const evidenceHelp = (state?: string | null): string =>
  (state || "").toUpperCase() === "COMPLETE"
    ? "该版本的证据完整，可作为正式 Baseline 与发布依据。"
    : "该版本仅用于诊断失败，证据不足，不可作为正式 Baseline 或发布依据。";

const shortId = (id: string) => (id.length > 12 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id);

export const SnapshotEvidenceBadge: React.FC<{ state?: string | null; className?: string }> = ({
  state,
  className = "",
}) => (
  <Badge tone={evidenceTone(state)} className={className} data-testid="snapshot-evidence-badge">
    {evidenceLabel(state)}
  </Badge>
);

type PanelProps = {
  launchId: string;
  selectedSnapshotId: string | null;
  onSelect: (snapshotId: string) => void;
  /** True while the bounded re-read for a just-frozen revision is running. */
  discovering?: boolean;
};

export const ResultSnapshotPanel: React.FC<PanelProps> = ({
  launchId,
  selectedSnapshotId,
  onSelect,
  discovering = false,
}) => {
  const history = useQuery<SnapshotList>(snapshotListQueryOptions(launchId));

  const revisions: SnapshotRevision[] = history.data?.revisions ?? [];
  const selected = revisions.find((row) => row.snapshot_id === selectedSnapshotId) ?? null;
  const active = selectedSnapshotId ? selected : (revisions[0] ?? null);
  const isHistorical = Boolean(selected) && Boolean(history.data?.latest_snapshot_id) &&
    selected?.snapshot_id !== history.data?.latest_snapshot_id;

  const detail = useQuery<SnapshotDetail | null>(
    snapshotDetailQueryOptions(
      launchId && active?.snapshot_id ? { launchId, snapshotId: active.snapshot_id } : null,
    ),
  );

  const historyRefreshError = history.isError && history.data ? (
    <div
      role="alert"
      className="flex w-full flex-wrap items-center justify-between gap-2 rounded-md border border-fail-border bg-fail-subtle px-2 py-1.5 text-micro text-fail"
      data-testid="snapshot-history-refresh-error"
    >
      <div className="flex min-w-0 items-center gap-2">
        <AlertCircle aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
        <span>修订目录刷新失败，仍显示已缓存版本：{formatApiError(history.error)}</span>
      </div>
      <Button
        type="button"
        variant="secondary"
        className="h-6 text-2xs px-2"
        onClick={() => history.refetch()}
      >
        重试
      </Button>
    </div>
  ) : null;

  if (history.isLoading) {
    return (
      <section className="bg-surface border border-border rounded-lg px-4 py-2.5 flex flex-wrap items-center justify-between gap-3 text-xs" data-testid="result-snapshot-panel">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-foreground">结果报告 (Result Snapshot)</span>
          <span className="text-muted-foreground">正在加载冻结结果版本...</span>
        </div>
      </section>
    );
  }

  if (history.isError && !history.data) {
    return (
      <section className="bg-surface border border-fail-border rounded-lg px-4 py-2 flex items-center justify-between gap-3 text-xs" data-testid="result-snapshot-panel">
        <div className="flex items-center gap-2 text-fail">
          <AlertCircle className="w-4 h-4 shrink-0" />
          <span className="font-semibold">加载快照版本失败</span>
          <span>{formatApiError(history.error)}</span>
        </div>
        <Button
          type="button"
          variant="secondary"
          className="h-6 text-2xs px-2"
          onClick={() => history.refetch()}
        >
          重试
        </Button>
      </section>
    );
  }

  if (revisions.length === 0) {
    return (
      <section className="bg-surface border border-border rounded-lg px-4 py-2.5 flex items-center justify-between gap-3 text-xs" data-testid="result-snapshot-panel">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-foreground">结果报告 (Result Snapshot)</span>
          <span className="text-muted-foreground" data-testid="result-snapshot-empty">
            尚未冻结任何结果版本。评测进入终态后会自动生成。
            {discovering && (
              <span className="ml-1 text-primary" data-testid="snapshot-discovering">
                正在自动检测本次运行刚冻结的结果版本...
              </span>
            )}
          </span>
        </div>
        {historyRefreshError}
      </section>
    );
  }

  const shareUrl = active
    ? `${typeof window !== "undefined" ? window.location.origin : ""}/launches/${launchId}?snapshot_id=${active.snapshot_id}`
    : "";

  return (
    <section className="bg-surface border border-border rounded-lg px-3.5 py-2 flex flex-wrap items-center justify-between gap-2 text-xs shadow-xs" data-testid="result-snapshot-panel">
      {active ? (
        <div className="flex flex-col gap-1 w-full">
          <div className="flex flex-wrap items-center justify-between gap-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-muted-foreground">当前查看版本</span>
              <span className="font-semibold text-foreground font-mono" data-testid="snapshot-revision">
                Revision {active.revision}
              </span>
              {active.is_latest && (
                <span className="px-1.5 py-0.5 rounded text-micro bg-pass-subtle text-pass border border-pass-border font-medium" data-testid="snapshot-latest-tag">
                  (最新)
                </span>
              )}
              {isHistorical && (
                <span className="px-1.5 py-0.5 rounded text-micro bg-retry-subtle text-retry border border-retry-border font-medium" data-testid="snapshot-newer-available">
                  已有更新的 Revision {history.data?.latest_revision}，你正在查看历史版本
                </span>
              )}
              <span className="text-muted-foreground font-mono text-micro" data-testid="snapshot-id" title={active.snapshot_id}>
                {shortId(active.snapshot_id)}
              </span>
              <span className="text-muted-foreground text-micro" data-testid="snapshot-created-at">
                冻结时间 {new Date(active.created_at).toLocaleString("zh-CN")}
              </span>
              <SnapshotEvidenceBadge state={active.evidence_state} />
              <span className="font-mono text-micro text-muted-foreground" data-testid="snapshot-source-digest">
                结果摘要 {active.source_result_digest?.slice(0, 16)}…
              </span>
            </div>

            {revisions.length > 1 && (
              <div className="flex items-center gap-1.5 shrink-0">
                <span className="text-muted-foreground text-micro">版本历史:</span>
                <div className="flex items-center gap-1" data-testid="snapshot-history">
                  {revisions.map((row) => (
                    <button
                      key={row.snapshot_id}
                      type="button"
                      onClick={() => onSelect(row.snapshot_id)}
                      aria-current={row.snapshot_id === active?.snapshot_id ? "true" : undefined}
                      data-testid={`snapshot-revision-${row.revision}`}
                      className={`text-micro px-1.5 py-0.5 rounded border transition-colors cursor-pointer ${
                        row.snapshot_id === active?.snapshot_id
                          ? "border-primary bg-primary-subtle text-primary font-semibold"
                          : "border-border bg-surface text-muted-foreground hover:bg-surface-hover hover:text-foreground"
                      }`}
                      title={`Revision ${row.revision}${row.is_latest ? " (最新)" : ""} · PASS ${row.quality_pass_count} / FAIL ${row.quality_fail_count} / UNKNOWN ${row.quality_unknown_count}`}
                    >
                      <span>Rev {row.revision}</span>
                      {row.is_latest ? " (最新)" : ""}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-micro text-muted-foreground pt-1 border-t border-border">
            <span data-testid="snapshot-evidence-help">{evidenceHelp(active.evidence_state)}</span>
            <span data-testid="snapshot-quality-counts">
              PASS {active.quality_pass_count} · FAIL {active.quality_fail_count} · UNKNOWN {active.quality_unknown_count}
            </span>
            {(active.evidence_reasons ?? []).length > 0 && (
              <ul className="inline-flex items-center gap-2 list-none p-0 m-0" data-testid="snapshot-evidence-reasons">
                {(active.evidence_reasons ?? []).map((reason) => (
                  <li key={reason}>· {reason}</li>
                ))}
              </ul>
            )}
            {detail.data?.releasable === false && (
              <span className="text-timeout font-medium" data-testid="snapshot-not-releasable">
                该版本不可作为正式 Baseline。
              </span>
            )}
            <span
              className="font-mono text-muted-foreground truncate max-w-xs"
              data-testid="snapshot-share-url"
              title={shareUrl}
            >
              固定分享链接 {shareUrl}
            </span>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap items-center justify-between gap-2 w-full">
          <span className="text-fail font-medium">指定快照版本不存在</span>
          {revisions.length > 0 && (
            <div className="flex items-center gap-1" data-testid="snapshot-history">
              {revisions.map((row) => (
                <button
                  key={row.snapshot_id}
                  type="button"
                  onClick={() => onSelect(row.snapshot_id)}
                  data-testid={`snapshot-revision-${row.revision}`}
                  className="text-micro px-2 py-0.5 rounded border border-border bg-surface text-muted-foreground hover:bg-surface-hover hover:text-foreground"
                >
                  Revision {row.revision}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
      {historyRefreshError}
    </section>
  );
};
