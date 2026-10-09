import React from "react";
import { useQuery } from "@tanstack/react-query";
import { Badge, type BadgeTone } from "../../components/Badge";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";

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
};

export const ResultSnapshotPanel: React.FC<PanelProps> = ({
  launchId,
  selectedSnapshotId,
  onSelect,
}) => {
  const history = useQuery({
    queryKey: [...queryKeys.launches.all, "result-snapshots", launchId],
    enabled: Boolean(launchId),
    queryFn: async () => {
      const res = await api.GET("/api/v1/experiment-launches/{launch_id}/result-snapshots", {
        params: { path: { launch_id: launchId } },
      });
      if (res.error) throw res.error;
      return (res.data ?? null) as SnapshotList | null;
    },
  });

  const revisions: SnapshotRevision[] = history.data?.revisions ?? [];
  const selected = revisions.find((row) => row.snapshot_id === selectedSnapshotId) ?? null;
  const active = selectedSnapshotId ? selected : (revisions[0] ?? null);
  const isHistorical = Boolean(selected) && Boolean(history.data?.latest_snapshot_id) &&
    selected?.snapshot_id !== history.data?.latest_snapshot_id;

  const detail = useQuery({
    queryKey: [...queryKeys.launches.all, "result-snapshot", launchId, active?.snapshot_id ?? "none"],
    enabled: Boolean(launchId && active?.snapshot_id),
    queryFn: async () => {
      const res = await api.GET(
        "/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}",
        { params: { path: { launch_id: launchId, snapshot_id: active!.snapshot_id } } },
      );
      if (res.error) throw res.error;
      return (res.data ?? null) as SnapshotDetail | null;
    },
  });

  if (history.isLoading) {
    return (
      <section className="bg-surface border border-border rounded-lg p-4" data-testid="result-snapshot-panel">
        <h3 className="text-sm font-semibold text-foreground">结果报告 (Result Snapshot)</h3>
        <p className="text-xs text-muted-foreground mt-2">正在加载冻结结果版本...</p>
      </section>
    );
  }

  if (revisions.length === 0) {
    return (
      <section className="bg-surface border border-border rounded-lg p-4" data-testid="result-snapshot-panel">
        <h3 className="text-sm font-semibold text-foreground">结果报告 (Result Snapshot)</h3>
        <p className="text-xs text-muted-foreground mt-2" data-testid="result-snapshot-empty">
          尚未冻结任何结果版本。评测进入终态后会自动生成，届时可分享固定版本链接。
        </p>
      </section>
    );
  }

  const shareUrl = active
    ? `${typeof window !== "undefined" ? window.location.origin : ""}/launches/${launchId}?snapshot_id=${active.snapshot_id}`
    : "";

  return (
    <section className="bg-surface border border-border rounded-lg p-4" data-testid="result-snapshot-panel">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <h3 className="text-sm font-semibold text-foreground">结果报告 (Result Snapshot)</h3>
        {active && <SnapshotEvidenceBadge state={active.evidence_state} />}
      </div>

      {active && (
        <div className="mt-3 space-y-1.5">
          <div className="flex items-baseline gap-2 flex-wrap">
            <span className="text-xs text-muted-foreground">当前查看版本</span>
            <span className="text-sm font-semibold text-foreground" data-testid="snapshot-revision">
              Revision {active.revision}
            </span>
            {active.is_latest && (
              <span className="text-micro text-muted-foreground" data-testid="snapshot-latest-tag">
                (最新)
              </span>
            )}
            {isHistorical && (
              <span className="text-micro text-retry font-medium" data-testid="snapshot-newer-available">
                已有更新的 Revision {history.data?.latest_revision}，你正在查看历史版本
              </span>
            )}
          </div>
          <p className="text-micro text-muted-foreground font-mono" data-testid="snapshot-id">
            {shortId(active.snapshot_id)}
          </p>
          <p className="text-micro text-muted-foreground" data-testid="snapshot-created-at">
            冻结时间 {new Date(active.created_at).toLocaleString("zh-CN")}
          </p>
          <p className="text-micro text-muted-foreground font-mono" data-testid="snapshot-source-digest">
            结果摘要 {active.source_result_digest.slice(0, 16)}…
          </p>
          <p className="text-micro text-muted-foreground" data-testid="snapshot-evidence-help">
            {evidenceHelp(active.evidence_state)}
          </p>
          {(active.evidence_reasons ?? []).length > 0 && (
            <ul className="text-micro text-muted-foreground list-disc pl-4" data-testid="snapshot-evidence-reasons">
              {(active.evidence_reasons ?? []).map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          )}
          <p className="text-micro text-muted-foreground" data-testid="snapshot-quality-counts">
            PASS {active.quality_pass_count} · FAIL {active.quality_fail_count} · UNKNOWN{" "}
            {active.quality_unknown_count}
          </p>
          <p
            className="text-micro text-muted-foreground font-mono break-all"
            data-testid="snapshot-share-url"
            title={shareUrl}
          >
            固定分享链接 {shareUrl}
          </p>
          {detail.data?.releasable === false && (
            <p className="text-micro text-muted-foreground" data-testid="snapshot-not-releasable">
              该版本不可作为正式 Baseline。
            </p>
          )}
        </div>
      )}

      {(revisions.length > 1 || !active) && revisions.length > 0 && (
        <div className="mt-4">
          <h4 className="text-xs font-medium text-muted-foreground mb-1.5">历史版本 (Revision History)</h4>
          <ul className="flex flex-col gap-1" data-testid="snapshot-history">
            {revisions.map((row) => (
              <li key={row.snapshot_id}>
                <button
                  type="button"
                  onClick={() => onSelect(row.snapshot_id)}
                  aria-current={row.snapshot_id === active?.snapshot_id ? "true" : undefined}
                  data-testid={`snapshot-revision-${row.revision}`}
                  className={`w-full text-left text-xs px-2 py-1.5 rounded-sm border focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus ${
                    row.snapshot_id === active?.snapshot_id
                      ? "border-accent bg-accent-subtle text-foreground font-medium"
                      : "border-border bg-surface text-muted-foreground hover:bg-surface-hover"
                  }`}
                >
                  <span className="font-semibold text-foreground mr-1.5">Revision {row.revision}</span>
                  {row.is_latest ? "(最新) · " : ""}PASS {row.quality_pass_count} / FAIL{" "}
                  {row.quality_fail_count} / UNKNOWN {row.quality_unknown_count}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
};
