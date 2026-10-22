import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  AlertCircle,
  ArrowLeft,
  Check,
  Copy,
  ExternalLink,
  Play,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { Button, type ButtonProps } from "../../components/ui/Primitives";
import { StatusBadge } from "../../components/StatusBadge";
import { QualityBadge } from "../../components/QualityBadge";
import { useCopyFeedback } from "./useCopyFeedback";
import { SyncStatusBadge } from "../../components/SyncStatusBadge";
import type { LangfuseLinkView } from "./langfuseLink";
import { LaunchActionMenu, type LaunchActionMenuItem } from "./LaunchActionMenu";

type LaunchResponse = import("../../api/schema").components["schemas"]["ExperimentLaunchResponse"];
type SnapshotRevision = import("../../api/schema").components["schemas"]["ResultSnapshotRevisionResponse"];

interface LaunchHeaderProps {
  launch: LaunchResponse;
  activeSnapshot: SnapshotRevision | null;
  allowedActions: string[];
  langfuseLink: LangfuseLinkView;
  isFetching: boolean;
  qualityConclusion?: string | null;
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

type ActionVariant = NonNullable<ButtonProps["variant"]>;

type LaunchActionDefinition = {
  id: string;
  label: string;
  pendingLabel: string;
  icon?: React.ReactNode;
  variant: ActionVariant;
  title?: string;
  testId?: string;
  allowed: boolean;
  pending: boolean;
  onSelect?: () => void;
};

/**
 * The 1024px `lg` layout boundary from the design system: below it the header is
 * narrow and the secondary actions live in the "more actions" menu; at or above
 * it they remain inline beside the two primary actions.
 */
const DESKTOP_QUERY = "(min-width: 64rem)";

function useIsDesktopViewport(): boolean {
  const [isDesktop, setIsDesktop] = useState(() =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia(DESKTOP_QUERY).matches
      : true,
  );

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const query = window.matchMedia(DESKTOP_QUERY);
    const onChange = (event: MediaQueryListEvent) => setIsDesktop(event.matches);
    setIsDesktop(query.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  return isDesktop;
}

export const LaunchHeader: React.FC<LaunchHeaderProps> = ({
  launch,
  activeSnapshot,
  allowedActions,
  langfuseLink,
  isFetching,
  qualityConclusion,
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

  const effectiveQuality = qualityConclusion !== undefined ? qualityConclusion : launch.quality_conclusion;
  const qualityConclusionStr = (effectiveQuality || "").toLowerCase();
  const isPass = qualityConclusionStr === "pass";
  const isFail = qualityConclusionStr === "fail";
  const isComplete = activeSnapshot?.evidence_state === "COMPLETE";

  const shortUuid = launch.id.length > 12
    ? `${launch.id.slice(0, 8)}…${launch.id.slice(-4)}`
    : launch.id;

  // The copied content is this launch's id, so the feedback identity is the launch.
  const copyId = useCopyFeedback(2000, launch?.id);
  const handleCopyId = () => {
    void copyId.copy(launch.id);
  };

  // One definition list drives both layouts: identical permission, pending and
  // handler wiring, so the menu can never drift from the inline buttons.
  const actionDefinitions: LaunchActionDefinition[] = [
    {
      id: "run",
      label: "启动评测 (Run)",
      pendingLabel: "正在运行...",
      icon: <Play className="h-3.5 w-3.5 fill-current" />,
      variant: "primary",
      allowed: allowedActions.includes("run") && Boolean(onRun),
      pending: Boolean(isRunPending),
      onSelect: onRun,
    },
    {
      id: "cancel",
      label: "取消评测 (Cancel)",
      pendingLabel: "正在取消...",
      variant: "danger",
      allowed: allowedActions.includes("cancel") && Boolean(onCancel),
      pending: Boolean(isCancelPending),
      onSelect: onCancel,
    },
    {
      id: "resume",
      label: "断点恢复 (Resume)",
      pendingLabel: "正在恢复...",
      icon: <RefreshCw className="h-3.5 w-3.5" />,
      variant: "primary",
      allowed: allowedActions.includes("resume") && Boolean(onResume),
      pending: Boolean(isResumePending),
      onSelect: onResume,
    },
    {
      id: "retry_failed",
      label: "重试失败用例 (Retry Failed)",
      pendingLabel: "重试中...",
      variant: "warning",
      allowed: allowedActions.includes("retry_failed") && Boolean(onRetryFailed),
      pending: Boolean(isRetryFailedPending),
      onSelect: onRetryFailed,
    },
    {
      id: "retry_evaluation",
      label: "重试评测 (Retry Eval)",
      pendingLabel: "重评中...",
      variant: "secondary",
      title: "仅重新评测失败的指标，复用原 Agent 输出，不会再次调用 Agent",
      testId: "retry-evaluation-button",
      allowed: allowedActions.includes("retry_evaluation") && Boolean(onRetryEvaluation),
      pending: Boolean(isRetryEvaluationPending),
      onSelect: onRetryEvaluation,
    },
  ];
  const visibleActions = actionDefinitions.filter((action) => action.allowed);

  const isDesktopLayout = useIsDesktopViewport();
  const [menuOpen, setMenuOpen] = useState(false);
  // Crossing the breakpoint closes the menu so no focus stays on a control of
  // the layout that is no longer rendered.
  useEffect(() => {
    setMenuOpen(false);
  }, [isDesktopLayout]);

  const menuItems: LaunchActionMenuItem[] = [
    ...(langfuseLink.kind === "link"
      ? [
          {
            kind: "link" as const,
            id: "langfuse",
            label: langfuseLink.detailLabel || "Langfuse Trace",
            href: langfuseLink.href,
            title: langfuseLink.title,
          },
        ]
      : [
          {
            kind: "note" as const,
            id: "langfuse-reason",
            label: langfuseLink.label,
            title: langfuseLink.title,
          },
        ]),
    ...visibleActions.map((action) => ({
      kind: "action" as const,
      id: action.id,
      label: action.label,
      pendingLabel: action.pendingLabel,
      pending: action.pending,
      icon: action.icon,
      variant: action.variant,
      title: action.title,
      testId: action.testId,
      onSelect: action.onSelect ?? (() => {}),
    })),
  ];

  const refreshButton = (
    <Button
      variant="secondary"
      aria-label="刷新"
      onClick={onRefresh}
      disabled={isFetching}
      title="刷新"
      className="min-h-8 h-auto py-1 px-2.5 text-xs whitespace-nowrap"
    >
      <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? "animate-spin text-primary" : ""}`} />
    </Button>
  );

  const baselineButton = (compact: boolean) =>
    isComplete && onOpenBaselineModal ? (
      <Button
        variant="primary"
        onClick={onOpenBaselineModal}
        className={`min-h-8 h-auto py-1 px-2.5 text-xs font-semibold whitespace-nowrap ${compact ? "" : ""}`}
        title="将当前固定版本设为该环境的 Baseline"
      >
        <ShieldCheck className="h-3.5 w-3.5" />
        <span>设为新 Baseline</span>
      </Button>
    ) : null;

  return (
    <div className="space-y-3">
      {/* 顶栏面包屑与快捷状态 */}
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
        <Link
          to="/launches"
          className="inline-flex items-center gap-1.5 hover:text-foreground transition-colors font-medium"
        >
          <ArrowLeft className="w-3.5 h-3.5" />
          <span>返回评测列表</span>
        </Link>
        <div
          className="flex flex-wrap items-center gap-1.5 text-micro text-muted-foreground"
          title="OTLP 同步（实时链路，非质量评分结论）"
        >
          <Activity className="w-3.5 h-3.5 text-muted-foreground/80 shrink-0" aria-hidden="true" />
          <span>OTLP 同步（实时，非评分结论）</span>
          <SyncStatusBadge
            status={launch.langfuse_sync_status}
            data-testid="langfuse-sync-badge"
            className="font-mono text-micro"
          />
        </div>
      </div>

      {/* 业务主体 Header */}
      <header className="bg-surface border border-border rounded-xl p-4 sm:p-5 shadow-xs">
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
                <QualityBadge quality={effectiveQuality || "unknown"} />
                <span className="text-xs font-semibold text-muted-foreground">
                  {isPass ? "冻结质量通过 (PASS)" : isFail ? "冻结质量未通过 (FAIL)" : "冻结质量未知 (UNKNOWN)"}
                </span>
              </div>

              {/* 证据状态徽章：无快照 / 证据完整 / 仅诊断 三态分离 */}
              {!activeSnapshot ? (
                <span
                  className="px-2 py-0.5 rounded text-xs text-muted-foreground bg-surface-muted border border-border flex items-center gap-1"
                  title="当前 Launch 尚未产出冻结结果，不可作为 Baseline"
                  data-testid="snapshot-evidence-state"
                >
                  尚未生成冻结结果
                </span>
              ) : isComplete ? (
                <span
                  className="px-2 py-0.5 rounded text-xs text-muted-foreground bg-surface-muted border border-border flex items-center gap-1"
                  title="当前选定 Revision 的冻结证据完整"
                  data-testid="snapshot-evidence-state"
                >
                  <ShieldCheck className="w-3.5 h-3.5 text-pass" />
                  冻结证据完整
                </span>
              ) : (
                <span
                  className="px-2 py-0.5 rounded text-xs text-muted-foreground bg-surface-muted border border-border flex items-center gap-1"
                  title="证据不足，仅供诊断"
                  data-testid="snapshot-evidence-state"
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
                  {copyId.isSuccess ? (
                    <span className="inline-flex items-center gap-0.5 text-pass font-sans text-micro">
                      <Check className="w-3 h-3 text-pass" />
                      <span>已复制</span>
                    </span>
                  ) : copyId.isError ? (
                    <span className="inline-flex items-center gap-0.5 text-fail font-sans text-micro" role="alert">
                      <AlertCircle className="w-3 h-3 text-fail" />
                      <span>复制失败</span>
                    </span>
                  ) : (
                    <Copy className="w-3 h-3" />
                  )}
                </button>
              </h2>
            </div>
          </div>

          {/* 右侧：操作区 */}
          {isDesktopLayout ? (
            /* 宽屏：两个主动作（Baseline / Langfuse）+ 次级运行动作同行铺开，
               主动作保持 32px 且不折行；空间不足时次级动作收入同一 More 菜单。 */
            <div className="flex flex-wrap items-center gap-1.5 sm:gap-2 w-full lg:w-auto max-w-full self-start lg:self-center">
              {refreshButton}
              {baselineButton(false)}

              {visibleActions.map((action) => (
                <Button
                  key={action.id}
                  variant={action.variant}
                  onClick={action.onSelect}
                  disabled={action.pending}
                  className={`min-h-8 h-auto py-1 px-2.5 text-xs whitespace-normal sm:whitespace-nowrap ${
                    action.id === "retry_evaluation" ? "" : ""
                  }`}
                  data-testid={action.testId}
                  title={action.title}
                >
                  {action.icon}
                  <span>{action.pending ? action.pendingLabel : action.label}</span>
                </Button>
              ))}

              {langfuseLink.kind === "link" ? (
                <a
                  href={langfuseLink.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label="在 Langfuse 中查看"
                  className="ui-button ui-button--secondary min-h-8 h-auto py-1 px-2.5 text-xs whitespace-nowrap"
                >
                  <span>{langfuseLink.detailLabel || "Langfuse Trace"}</span>
                  <ExternalLink className="h-3.5 w-3.5" />
                </a>
              ) : (
                <span
                  className="sr-only"
                  title={langfuseLink.title}
                  data-testid="langfuse-link-reason"
                >
                  {langfuseLink.label}
                </span>
              )}
            </div>
          ) : (
            /* 窄屏 (<1024px)：紧凑主动作 + 图标刷新 + 更多操作菜单，
               全部允许能力经菜单可达，不绕过任何确认。 */
            <div className="flex flex-wrap items-center gap-1.5 sm:gap-2 w-full lg:w-auto max-w-full self-start lg:self-center">
              {baselineButton(true)}
              {refreshButton}
              <LaunchActionMenu items={menuItems} open={menuOpen} onOpenChange={setMenuOpen} />
              {langfuseLink.kind !== "link" && (
                <span
                  className="sr-only"
                  title={langfuseLink.title}
                  data-testid="langfuse-link-reason"
                >
                  {langfuseLink.label}
                </span>
              )}
            </div>
          )}
        </div>
      </header>
    </div>
  );
};
