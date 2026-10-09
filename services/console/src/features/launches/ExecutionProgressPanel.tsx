import React, { useState } from "react";
import clsx from "clsx";
import { ChevronDown, ChevronUp, Clock, Info } from "lucide-react";
import { isLaunchExecutionActive } from "./launchState";

type ProgressCounts = {
  total: number;
  pending: number;
  queued: number;
  running: number;
  retry_wait: number;
  succeeded: number;
  failed: number;
  timed_out: number;
  cancelled: number;
  percentage?: number;
  attempts?: number;
  retries?: number;
};

interface ProgressStateSpec {
  state: string;
  label: string;
  fillClass: string;
  cardClass: string;
  labelClass: string;
  valueClass: string;
  count: (p: ProgressCounts) => number;
}

const PROGRESS_STATES: ProgressStateSpec[] = [
  {
    state: "queued",
    label: "排队中",
    fillClass: "bg-queued-solid",
    cardClass: "bg-queued-subtle border-queued-border",
    labelClass: "text-queued",
    valueClass: "text-queued-strong",
    count: (p) => p.queued + p.pending,
  },
  {
    state: "running",
    label: "运行中",
    fillClass: "bg-running-solid animate-pulse",
    cardClass: "bg-running-subtle border-running-border",
    labelClass: "text-running",
    valueClass: "text-running-strong",
    count: (p) => p.running,
  },
  {
    state: "pass",
    label: "成功",
    fillClass: "bg-pass-solid",
    cardClass: "bg-pass-subtle border-pass-border",
    labelClass: "text-pass",
    valueClass: "text-pass-strong",
    count: (p) => p.succeeded,
  },
  {
    state: "fail",
    label: "失败",
    fillClass: "bg-fail-solid",
    cardClass: "bg-fail-subtle border-fail-border",
    labelClass: "text-fail",
    valueClass: "text-fail-strong",
    count: (p) => p.failed,
  },
  {
    state: "timeout",
    label: "超时",
    fillClass: "bg-timeout-solid",
    cardClass: "bg-timeout-subtle border-timeout-border",
    labelClass: "text-timeout",
    valueClass: "text-timeout-strong",
    count: (p) => p.timed_out,
  },
  {
    state: "retry",
    label: "等待重试",
    fillClass: "bg-retry-solid",
    cardClass: "bg-retry-subtle border-retry-border",
    labelClass: "text-retry",
    valueClass: "text-retry-strong",
    count: (p) => p.retry_wait,
  },
  {
    state: "cancelled",
    label: "已取消",
    fillClass: "bg-cancelled-solid",
    cardClass: "bg-cancelled-subtle border-cancelled-border",
    labelClass: "text-cancelled",
    valueClass: "text-cancelled-strong",
    count: (p) => p.cancelled,
  },
];

interface ExecutionProgressPanelProps {
  status: string;
  progress?: ProgressCounts | null;
  cancelRequestedAt?: string | null;
  statusReason?: string | null;
}

export const ExecutionProgressPanel: React.FC<ExecutionProgressPanelProps> = ({
  status,
  progress,
  cancelRequestedAt,
  statusReason,
}) => {
  const isExecuting = isLaunchExecutionActive(status);
  const [isExpanded, setIsExpanded] = useState<boolean>(isExecuting);

  if (!progress || progress.total === 0) return null;

  const progressSegments = PROGRESS_STATES.map((spec) => ({
    ...spec,
    count: spec.count(progress),
  }));

  return (
    <div className="space-y-3">
      {/* 协作取消过渡横幅 */}
      {cancelRequestedAt && status !== "CANCELLED" && (
        <div className="p-3.5 text-xs bg-timeout-subtle border border-timeout-border rounded-xl text-timeout-strong flex items-center justify-between shadow-xs">
          <div className="flex items-center gap-2">
            <Clock className="w-4 h-4 text-timeout animate-spin" />
            <span>
              已收到协作取消请求，系统正在等待处于执行态的任务安全终止（状态过渡中：CANCELLING）。
            </span>
          </div>
          <span className="font-mono text-micro text-timeout">
            申请时间: {new Date(cancelRequestedAt).toLocaleTimeString("zh-CN")}
          </span>
        </div>
      )}

      {/* 状态说明原因 */}
      {statusReason && (
        <div className="p-3 text-xs bg-surface border border-border rounded-xl text-foreground flex items-center gap-2">
          <Info className="w-4 h-4 text-primary shrink-0" />
          <div>
            <span className="font-semibold mr-1.5">状态说明:</span>
            <span>{statusReason}</span>
          </div>
        </div>
      )}

      {/* 进度看板卡片 */}
      <div className="bg-surface border border-border rounded-xl p-4 shadow-xs space-y-3">
        <div className="flex items-center justify-between text-xs">
          <div className="flex items-center gap-2 font-bold text-foreground">
            <span>实时执行进度看板</span>
            <span className="font-mono text-primary">
              ({Number.isFinite(progress.percentage) ? progress.percentage : 0}%)
            </span>
            {!isExecuting && (
              <span className="text-micro font-normal text-muted-foreground ml-2">
                (已终态 · 默认收敛)
              </span>
            )}
          </div>
          <div className="flex items-center gap-3 text-muted-foreground font-mono text-micro">
            <span>总调用: {progress.attempts ?? 0} 次</span>
            <span>重试: {progress.retries ?? 0} 次</span>
            <button
              type="button"
              onClick={() => setIsExpanded(!isExpanded)}
              className="inline-flex items-center gap-1 text-primary hover:text-foreground cursor-pointer focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus rounded-sm px-1"
            >
              <span>{isExpanded ? "收起诊断" : "展开诊断"}</span>
              {isExpanded ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
            </button>
          </div>
        </div>

        {/* 进度条：始终保留在视口上 */}
        <div
          data-testid="progress-meter"
          className="w-full bg-surface-muted rounded-full h-2.5 overflow-hidden flex"
        >
          {progressSegments.map((segment) => (
            <div
              key={segment.state}
              data-segment={segment.state}
              data-share={String(segment.count ?? 0)}
              className={clsx("h-full transition-all duration-300", segment.fillClass)}
              style={{ width: `${progress.total > 0 ? ((segment.count ?? 0) / progress.total) * 100 : 0}%` }}
              title={`${segment.label}: ${segment.count}`}
            />
          ))}
        </div>

        {/* 8 分格状态卡片：终态下默认隐藏收敛，展开时显示 */}
        <div className={clsx("grid grid-cols-4 sm:grid-cols-8 gap-2 pt-1", !isExpanded && "hidden")}>
          <div className="text-center p-2 rounded-lg border border-border-strong border-dashed">
            <span className="text-micro text-muted-foreground block">总用例</span>
            <span className="text-sm font-bold font-mono text-foreground">{progress.total}</span>
          </div>
          {progressSegments.map((segment) => (
            <div
              key={segment.state}
              data-card={segment.state}
              className={clsx("text-center p-2 rounded-lg border", segment.cardClass)}
            >
              <span className={clsx("text-micro block", segment.labelClass)}>{segment.label}</span>
              <span className={clsx("text-sm font-bold font-mono", segment.valueClass)}>
                {segment.count}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};
