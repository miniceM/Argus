import React from "react";
import { AlertTriangle, CheckCircle2, Clock, MinusCircle, RefreshCw } from "lucide-react";

type GeneratedSync = import("../../api/schema").components["schemas"]["LangfuseSyncStatusResponse"];

export type LangfuseSync = GeneratedSync | null | undefined;

type ScopeTone = "pass" | "fail" | "running" | "queued" | "neutral";

const SCOPE_LABELS: Record<string, string> = {
  SYNCED: "已同步",
  PENDING: "同步中",
  FAILED: "同步失败",
  RETRY_EXHAUSTED: "重试已耗尽",
  NOT_APPLICABLE: "不适用",
  UNKNOWN: "状态未知",
};

const OVERALL_LABELS: Record<string, string> = {
  SYNCED: "全部已同步",
  PENDING: "同步进行中",
  FAILED: "同步失败",
  RETRY_EXHAUSTED: "同步重试已耗尽",
  NOT_APPLICABLE: "无可同步内容",
  UNKNOWN: "同步状态未知",
};

const OVERALL_TONES: Record<string, ScopeTone> = {
  SYNCED: "pass",
  PENDING: "running",
  FAILED: "fail",
  RETRY_EXHAUSTED: "fail",
  NOT_APPLICABLE: "neutral",
  UNKNOWN: "neutral",
};

const TONE_CLASS: Record<ScopeTone, string> = {
  pass: "border-pass/40 bg-pass/10 text-pass",
  fail: "border-fail/40 bg-fail/10 text-fail",
  running: "border-running/40 bg-running/10 text-running",
  queued: "border-queued/40 bg-queued/10 text-queued",
  neutral: "border-border bg-canvas text-muted-foreground",
};

const ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  SYNCED: CheckCircle2,
  PENDING: RefreshCw,
  FAILED: AlertTriangle,
  RETRY_EXHAUSTED: AlertTriangle,
  NOT_APPLICABLE: MinusCircle,
  UNKNOWN: Clock,
};

const toneFor = (status: string): ScopeTone => OVERALL_TONES[status] ?? "neutral";

const Scope: React.FC<{ label: string; scope: GeneratedSync["item_trace"]; testId: string }> = ({
  label,
  scope,
  testId,
}) => {
  const Icon = ICONS[scope.status] ?? Clock;
  const tone = toneFor(scope.status);
  return (
    <div className="flex items-start gap-2 text-micro" data-testid={testId}>
      <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 ${TONE_CLASS[tone]}`}>
        <Icon className="h-3 w-3" />
        {label}：{SCOPE_LABELS[scope.status] ?? scope.status}
      </span>
      {scope.reason && (
        <span className="text-muted-foreground" title={scope.reason}>
          {scope.reason}
        </span>
      )}
    </div>
  );
};

/**
 * Langfuse sync state, shown separately from the quality conclusion.
 *
 * A synced Item/Trace scope never implies the Run Score scope is synced, and a
 * Langfuse outage never changes the Argus result — so the two facts are rendered
 * as two facts, each with its own scope and reason.
 */
export const LangfuseSyncPanel: React.FC<{ sync: LangfuseSync }> = ({ sync }) => {
  if (!sync) return null;
  const overallTone = toneFor(sync.overall);
  const OverallIcon = ICONS[sync.overall] ?? Clock;
  return (
    <div className="space-y-2 rounded-lg border border-border bg-canvas/70 p-3" data-testid="langfuse-sync-panel">
      <div className="flex items-center gap-2">
        <span
          data-testid="langfuse-sync-overall"
          className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-micro ${TONE_CLASS[overallTone]}`}
        >
          <OverallIcon className="h-3 w-3" />
          Langfuse 同步：{OVERALL_LABELS[sync.overall] ?? sync.overall}
        </span>
      </div>
      <Scope label="Item / Trace 评分" scope={sync.item_trace} testId="langfuse-sync-item-trace" />
      <Scope label="Run Score 汇总" scope={sync.run_score} testId="langfuse-sync-run-score" />
      <p className="text-micro text-muted-foreground" data-testid="langfuse-sync-disclaimer">
        Langfuse 是 Argus 冻结结果的单向分析投影；同步失败不会改变上方质量结论、结果修订或 digest。
      </p>
    </div>
  );
};
