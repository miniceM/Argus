import React, { useState } from "react";
import { Link } from "react-router-dom";
import {
  ArrowLeft,
  Check,
  Copy,
  ExternalLink,
  Play,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { Button, buttonClassName } from "../../components/ui/Primitives";
import { StatusBadge } from "../../components/StatusBadge";
import { QualityBadge } from "../../components/QualityBadge";
import { SyncStatusBadge } from "../../components/SyncStatusBadge";
import type { LangfuseLinkView } from "./langfuseLink";

type LaunchResponse = import("../../api/schema").components["schemas"]["ExperimentLaunchResponse"];
type SnapshotRevision = import("../../api/schema").components["schemas"]["ResultSnapshotRevisionResponse"];

interface LaunchHeaderProps {
  launch: LaunchResponse;
  activeSnapshot: SnapshotRevision | null;
  allowedActions: string[];
  langfuseLink: LangfuseLinkView;
  isFetching: boolean;
  onRefresh: () => void;
  onRun?: () => void;
  onCancel?: () => void;
  onResume?: () => void;
  onRetryFailed?: () => void;
  onRetryEvaluation?: () => void;
  onOpenBaselineModal?: () => void;
  isRunPending?: boolean;
  isCancelPending?: boolean;
  isResumePending?: boolean;
  isRetryFailedPending?: boolean;
  isRetryEvaluationPending?: boolean;
}

export const LaunchHeader: React.FC<LaunchHeaderProps> = ({
  launch,
  activeSnapshot,
  allowedActions,
  langfuseLink,
  isFetching,
  onRefresh,
  onRun,
  onCancel,
  onResume,
  onRetryFailed,
  onRetryEvaluation,
  onOpenBaselineModal,
  isRunPending,
  isCancelPending,
  isResumePending,
  isRetryFailedPending,
  isRetryEvaluationPending,
}) => {
  const [copied, setCopied] = useState(false);

  const manifest = (launch.manifest || {}) as any;
  const agentId = manifest.agent?.id || launch.agent_id || "未知 Agent";
  const agentVersion = manifest.agent?.version || launch.agent_version || "latest";
  const datasetName = manifest.dataset?.dataset_name || manifest.dataset?.name || launch.dataset_name || "未知数据集";
  const itemsCount = manifest.dataset?.items_count ?? manifest.dataset?.items?.length ?? launch.progress?.total ?? 0;

  // Duration
  let durationText = "—";
  if (launch.started_at && launch.completed_at) {
    const start = new Date(launch.started_at).getTime();
    const end = new Date(launch.completed_at).getTime();
    durationText = `${((end - start) / 1000).toFixed(2)}s`;
  }

  const qualityConclusion = (launch.quality_conclusion || "").toLowerCase();
  const isPass = qualityConclusion === "pass";
  const isFail = qualityConclusion === "fail";
  const isComplete = activeSnapshot?.evidence_state === "COMPLETE";

  const shortUuid = launch.id.length > 12
    ? `${launch.id.slice(0, 8)}…${launch.id.slice(-4)}`
    : launch.id;

  const handleCopyId = () => {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(launch.id);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <div className="space-y-4">
      {/* 顶栏面包屑与快捷状态 */}
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <Link
          to="/launches"
          className="inline-flex items-center gap-1.5 hover:text-foreground transition-colors font-medium"
        >
          <ArrowLeft className="w-3.5 h-3.5" />
          <span>返回评测列表</span>
        </Link>
        <div className="flex items-center gap-3">
          <SyncStatusBadge
            status={launch.langfuse_sync_status}
            data-testid="langfuse-sync-badge"
            className="font-mono text-micro"
          />
        </div>
      </div>

      {/* 业务主体 Header */}
      <header className="bg-surface border border-border rounded-xl p-5 shadow-xs">
        <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
          {/* 左侧：标题与元数据 */}
          <div className="space-y-2 min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-bold text-foreground tracking-tight">
                {agentId}
              </h1>
              <span className="px-2 py-0.5 rounded-md text-xs font-semibold bg-primary-subtle text-primary border border-primary-border font-mono">
                {agentVersion} (Candidate)
              </span>

              {/* 执行状态与门禁徽章 */}
              <StatusBadge status={launch.status} />
              <div className="flex items-center gap-1.5">
                <QualityBadge quality={launch.quality_conclusion} />
                <span className="text-xs font-semibold text-muted-foreground">
                  {isPass ? "门禁准入通过 (PASS)" : isFail ? "门禁未通过 (FAIL)" : "门禁状态未知 (UNKNOWN)"}
                </span>
              </div>

              {/* 证据完整状态徽章 */}
              {isComplete ? (
                <span
                  className="px-2 py-0.5 rounded text-xs text-muted-foreground bg-surface-muted border border-border flex items-center gap-1"
                  title="数据签名完整，可作为基线"
                >
                  <ShieldCheck className="w-3.5 h-3.5 text-pass" />
                  证据完整
                </span>
              ) : (
                <span
                  className="px-2 py-0.5 rounded text-xs text-muted-foreground bg-surface-muted border border-border flex items-center gap-1"
                  title="证据不足，仅供诊断"
                >
                  诊断快照
                </span>
              )}
            </div>

            {/* 人类可读元信息行 */}
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              <span>
                任务：<strong className="text-foreground font-medium">{launch.name || launch.id}</strong>
              </span>
              <span>·</span>
              <span>
                数据集：<strong className="text-foreground font-medium">{datasetName} ({itemsCount} 例)</strong>
              </span>
              <span>·</span>
              <span>
                耗时：<strong className="text-foreground font-mono">{durationText}</strong>
              </span>
              <span>·</span>
              <span>
                时间：{new Date(launch.created_at).toLocaleString("zh-CN", { hour12: false })}
              </span>
              <span>·</span>
              <h2 className="text-xs font-normal inline m-0" aria-label={launch.id}>
                <button
                  type="button"
                  onClick={handleCopyId}
                  className="font-mono text-muted-foreground hover:text-foreground cursor-pointer inline-flex items-center gap-1 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus rounded-sm px-1 py-0.5"
                  title={`点击复制完整 ID: ${launch.id}`}
                >
                  <span>ID: {shortUuid}</span>
                  {copied ? <Check className="w-3 h-3 text-pass" /> : <Copy className="w-3 h-3" />}
                </button>
              </h2>
            </div>
          </div>

          {/* 右侧：操作区（严格单行水平按钮组，高度统一 32px） */}
          <div className="flex items-center gap-2 shrink-0 self-start lg:self-center whitespace-nowrap">
            <Button
              variant="secondary"
              aria-label="刷新"
              onClick={onRefresh}
              disabled={isFetching}
              title="刷新"
              className="h-8 px-2.5 text-xs"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? "animate-spin text-primary" : ""}`} />
            </Button>

            {isComplete && onOpenBaselineModal && (
              <Button
                variant="primary"
                onClick={onOpenBaselineModal}
                className="h-8 text-xs font-semibold"
                title="将当前固定版本设为该环境的 Baseline"
              >
                <ShieldCheck className="h-3.5 w-3.5" />
                <span>设为新 Baseline</span>
              </Button>
            )}

            {allowedActions.includes("run") && onRun && (
              <Button onClick={onRun} disabled={isRunPending} className="h-8 text-xs">
                <Play className="h-3.5 w-3.5 fill-current" />
                <span>{isRunPending ? "正在运行..." : "立即执行评测 (Run)"}</span>
              </Button>
            )}

            {allowedActions.includes("cancel") && onCancel && (
              <Button variant="danger" onClick={onCancel} disabled={isCancelPending} className="h-8 text-xs">
                <span>{isCancelPending ? "正在取消..." : "取消评测 (Cancel)"}</span>
              </Button>
            )}

            {allowedActions.includes("resume") && onResume && (
              <Button onClick={onResume} disabled={isResumePending} className="h-8 text-xs">
                <RefreshCw className="h-3.5 w-3.5" />
                <span>{isResumePending ? "正在恢复..." : "断点恢复 (Resume)"}</span>
              </Button>
            )}

            {allowedActions.includes("retry_failed") && onRetryFailed && (
              <Button variant="warning" onClick={onRetryFailed} disabled={isRetryFailedPending} className="h-8 text-xs">
                <span>{isRetryFailedPending ? "重试中..." : "重试失败用例 (Retry Failed)"}</span>
              </Button>
            )}

            {allowedActions.includes("retry_evaluation") && onRetryEvaluation && (
              <Button
                variant="secondary"
                onClick={onRetryEvaluation}
                disabled={isRetryEvaluationPending}
                className="h-8 text-xs"
                data-testid="retry-evaluation-button"
                title="仅重新评测失败的指标，复用原 Agent 输出，不会再次调用 Agent"
              >
                <span>{isRetryEvaluationPending ? "重评中..." : "重试评测失败 (Retry Evaluation)"}</span>
              </Button>
            )}

            {langfuseLink.kind === "link" ? (
              <a
                href={langfuseLink.href}
                target="_blank"
                rel="noopener noreferrer"
                aria-label="在 Langfuse 中查看"
                className={buttonClassName("secondary", "h-8 text-xs")}
              >
                <span>{langfuseLink.detailLabel || "在 Langfuse 打开 Trace"}</span>
                <ExternalLink className="h-3.5 w-3.5" />
              </a>
            ) : (
              <span
                className="text-xs text-muted-foreground px-2"
                title={langfuseLink.title}
                data-testid="langfuse-link-reason"
              >
                {langfuseLink.label}
              </span>
            )}
          </div>
        </div>
      </header>
    </div>
  );
};
