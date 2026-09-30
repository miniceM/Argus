import React, { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import {
  ArrowLeft,
  Bot,
  CheckCircle2,
  Clock,
  Database,
  ExternalLink,
  FileCode,
  Play,
  RefreshCw,
  Rocket,
  Sliders,
  Zap,
} from "lucide-react";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";
import { StatusBadge } from "../../components/StatusBadge";
import { QualityBadge } from "../../components/QualityBadge";
import { SyncStatusBadge } from "../../components/SyncStatusBadge";
import { JsonViewer } from "../../components/JsonViewer";
import { ItemTable } from "./ItemTable";
import { ComparisonReport } from "./ComparisonReport";
import { ACTIVE_LAUNCH_STATUSES, LaunchStatus } from "./LaunchesList";
import { ErrorState, LoadingState } from "../../components/StateViews";
import { Button, PageHeader, Panel, buttonClassName } from "../../components/ui/Primitives";
import { Modal } from "../../components/ui/Overlay";

type LaunchResponse = import("../../api/schema").components["schemas"]["ExperimentLaunchResponse"];
type ItemExecution = import("../../api/schema").components["schemas"]["ExperimentItemExecutionResponse"];

interface ManifestData {
  schema_version?: string;
  manifest_version?: string;
  comparison?: {
    environment?: string;
    baseline_snapshot_id?: string | null;
    baseline_binding_revision?: number | null;
    baseline_resolution?: string;
  };
  dataset?: {
    source?: string;
    dataset_name?: string;
    dataset_id?: string;
    dataset_version?: string;
    snapshot_digest?: string;
    items_count?: number;
    name?: string;
    version?: string;
  };
  agent?: {
    agent_id?: string;
    id?: string;
    version?: string;
    endpoint?: string;
    method?: string;
    spec_digest?: string;
    credential_ref?: string;
    execution_policy?: {
      timeout_seconds?: number;
      max_retries?: number;
      rate_limit_per_minute?: number | null;
      max_concurrency?: number;
      is_idempotent?: boolean;
    };
    request_mapping?: unknown;
  };
  evaluators?: Array<{
    id?: string;
    name?: string;
    version?: string;
    type?: string;
    scope?: string;
    threshold?: number;
  }>;
  execution_policy?: {
    timeout_seconds?: number;
    max_retries?: number;
    rate_limit_per_minute?: number | null;
    max_concurrency?: number;
  };
  runner?: {
    runner_version?: string;
    concurrency?: number;
    mapping_engine_version?: string;
  };
  created_at?: string;
}

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
};

interface ProgressStateSpec {
  state: string;
  label: string;
  /** Meter fill. */
  fillClass: string;
  /** Count card surface. */
  cardClass: string;
  labelClass: string;
  valueClass: string;
  count: (p: ProgressCounts) => number;
}

// One description per execution state, consumed by both the meter and the
// count grid. Deriving both from a single list is what keeps them honest: the
// meter previously omitted `queued`, so a launch with nothing started yet
// rendered an empty bar beside a grid reading "queued: 6".
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

export const LaunchDetail: React.FC = () => {
  const { launchId } = useParams<{ launchId: string }>();
  const queryClient = useQueryClient();
  const [showRawManifest, setShowRawManifest] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [showRetryModal, setShowRetryModal] = useState(false);
  const [forceRetry, setForceRetry] = useState(false);

  useEffect(() => {
    setShowRawManifest(false);
    setActionError(null);
    setShowRetryModal(false);
    setForceRetry(false);
  }, [launchId]);

  // 1. Fetch Launch Details with S2 Polling
  const {
    data: launch,
    isLoading: isLaunchLoading,
    error: launchError,
    refetch: refetchLaunch,
    isFetching: isLaunchFetching,
  } = useQuery<LaunchResponse>({
    queryKey: queryKeys.launches.detail(launchId || ""),
    queryFn: async () => {
      if (!launchId) throw new Error("缺少 Launch ID");
      const res = await api.GET("/api/v1/experiment-launches/{launch_id}", {
        params: { path: { launch_id: launchId } },
      });

      if (res.error) {
        if (res.response.status === 404) {
          throw new Error(`Launch ${launchId} 不存在或已删除`);
        }
        throw res.error;
      }

      if (
        !res.data ||
        Array.isArray(res.data) ||
        typeof res.data !== "object" ||
        res.data.id !== launchId
      ) {
        throw new Error("Launch 详情响应与请求 ID 不一致，请重新加载");
      }

      return res.data as LaunchResponse;
    },
    enabled: Boolean(launchId),
    refetchInterval: (query) => {
      const data = query.state.data;
      if (
        data &&
        ACTIVE_LAUNCH_STATUSES.has((data.status?.toUpperCase() || "") as LaunchStatus)
      ) {
        return 1500;
      }
      return false;
    },
  });

  // 2. Fetch Items with S2 Polling
  const {
    data: rawItems,
    isLoading: isItemsLoading,
    error: itemsError,
    refetch: refetchItems,
  } = useQuery<ItemExecution[]>({
    queryKey: queryKeys.launches.items(launchId || ""),
    queryFn: async () => {
      if (!launchId) return [];
      const res = await api.GET("/api/v1/experiment-launches/{launch_id}/items", {
        params: { path: { launch_id: launchId } },
      });

      if (res.error) throw res.error;
      if (!Array.isArray(res.data)) {
        throw new Error("用例明细响应格式无效，请重新加载");
      }

      if (res.data.some((item) => item.launch_id !== launchId)) {
        throw new Error("用例明细归属的 Launch 与当前页面不一致，请重新加载");
      }

      return res.data as ItemExecution[];
    },
    enabled: Boolean(launchId && launch?.id === launchId),
    refetchInterval: () => {
      if (
        launch &&
        ACTIVE_LAUNCH_STATUSES.has((launch.status?.toUpperCase() || "") as LaunchStatus)
      ) {
        return 1500;
      }
      return false;
    },
  });

  const items: ItemExecution[] = Array.isArray(rawItems) ? rawItems : [];

  const invalidateAll = () => {
    if (launchId) {
      queryClient.invalidateQueries({ queryKey: queryKeys.launches.detail(launchId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.launches.items(launchId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.launches.list() });
    }
  };

  // 3. Trigger Asynchronous Run Mutation
  const runMutation = useMutation({
    mutationFn: async () => {
      if (!launchId) return;
      setActionError(null);
      const res = await api.POST("/api/v1/experiment-launches/{launch_id}/run", {
        params: { path: { launch_id: launchId } },
      });
      if (res.error) throw res.error;
      return res.data;
    },
    onSuccess: invalidateAll,
    onError: (err) => setActionError(formatApiError(err)),
  });

  // 4. Cancel Mutation
  const cancelMutation = useMutation({
    mutationFn: async () => {
      if (!launchId) return;
      setActionError(null);
      const res = await api.POST("/api/v1/experiment-launches/{launch_id}/cancel", {
        params: { path: { launch_id: launchId } },
      });
      if (res.error) throw res.error;
      return res.data;
    },
    onSuccess: invalidateAll,
    onError: (err) => setActionError(formatApiError(err)),
  });

  // 5. Resume Mutation
  const resumeMutation = useMutation({
    mutationFn: async () => {
      if (!launchId) return;
      setActionError(null);
      const res = await api.POST("/api/v1/experiment-launches/{launch_id}/resume", {
        params: { path: { launch_id: launchId } },
      });
      if (res.error) throw res.error;
      return res.data;
    },
    onSuccess: invalidateAll,
    onError: (err) => setActionError(formatApiError(err)),
  });

  // 6. Retry Failed Mutation
  const retryFailedMutation = useMutation({
    mutationFn: async (force: boolean) => {
      if (!launchId) return;
      setActionError(null);
      const res = await api.POST("/api/v1/experiment-launches/{launch_id}/retry-failed", {
        params: { path: { launch_id: launchId } },
        body: { force },
      });
      if (res.error) throw res.error;
      return res.data;
    },
    onSuccess: () => {
      setShowRetryModal(false);
      invalidateAll();
    },
    onError: (err) => {
      const errMsg = formatApiError(err);
      setActionError(errMsg);
      // If error mentions ambiguous or force, reopen modal with hint
      if (errMsg.toLowerCase().includes("force") || errMsg.toLowerCase().includes("ambiguous")) {
        setShowRetryModal(true);
      }
    },
  });

  if (isLaunchLoading) return <LoadingState message="正在加载评测任务与不可变快照..." />;
  if (launchError) {
    return (
      <div className="space-y-3">
        <Link
          to="/launches"
          className="inline-flex items-center gap-1.5 text-xs font-semibold text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="w-3.5 h-3.5" />
          <span>返回评测列表</span>
        </Link>
        <ErrorState message={formatApiError(launchError)} onRetry={() => refetchLaunch()} />
      </div>
    );
  }
  if (!launch) return <ErrorState message="未找到对应的评测任务" />;

  const allowedActions = launch.allowed_actions || (launch.status === "PENDING" ? ["run"] : []);
  const progress = launch.progress;
  // Derived inline: seven entries, and this sits below the component's early
  // returns where a hook would violate the rules of hooks.
  const progressSegments = progress
    ? PROGRESS_STATES.map((spec) => ({ ...spec, count: spec.count(progress) }))
    : [];

  const manifest = (launch.manifest || {}) as ManifestData;
  const manifestAgent = manifest.agent || {};
  const manifestPolicy = manifest.execution_policy || manifestAgent.execution_policy || {};
  const manifestEvaluators = manifest.evaluators || [];
  const manifestRunner = manifest.runner || {};

  // Metrics calculation from items
  const totalItems = itemsError ? null : items.length;
  const passedItems = itemsError
    ? null
    : items.filter((i) => i.quality_conclusion?.toLowerCase() === "pass").length;

  // Duration calculation
  let durationText = "-";
  if (launch.started_at && launch.completed_at) {
    const start = new Date(launch.started_at).getTime();
    const end = new Date(launch.completed_at).getTime();
    const diffSec = ((end - start) / 1000).toFixed(2);
    durationText = `${diffSec} 秒`;
  }

  return (
    <div className="space-y-6">
      <div className="space-y-3">
        <Link
          to="/launches"
          className="inline-flex items-center gap-1.5 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          <span>返回评测列表</span>
        </Link>
        <PageHeader
          title={(
            <span className="flex min-w-0 items-center gap-2">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-primary-border bg-primary-subtle text-primary">
                <Rocket className="h-4 w-4" />
              </span>
              <span className="break-all font-mono">{launch.id}</span>
            </span>
          )}
          description={`创建时间: ${new Date(launch.created_at).toLocaleString("zh-CN", { hour12: false })}`}
          actions={(
            <>
              <Button
                variant="secondary"
                aria-label="刷新"
                onClick={() => {
                  refetchLaunch();
                  refetchItems();
                }}
                disabled={isLaunchFetching}
                title="刷新"
                className="px-2"
              >
                <RefreshCw className={`h-4 w-4 ${isLaunchFetching ? "animate-spin text-primary" : ""}`} />
              </Button>

              {allowedActions.includes("run") && (
                <Button onClick={() => runMutation.mutate()} disabled={runMutation.isPending} className="text-xs">
                  <Play className="h-3.5 w-3.5 fill-current" />
                  <span>{runMutation.isPending ? "正在运行评测..." : "立即执行评测 (Run Evaluation)"}</span>
                </Button>
              )}

              {allowedActions.includes("cancel") && (
                <Button variant="danger" onClick={() => cancelMutation.mutate()} disabled={cancelMutation.isPending} className="text-xs">
                  <span>{cancelMutation.isPending ? "正在取消..." : "取消评测 (Cancel)"}</span>
                </Button>
              )}

              {allowedActions.includes("resume") && (
                <Button onClick={() => resumeMutation.mutate()} disabled={resumeMutation.isPending} className="text-xs">
                  <RefreshCw className="h-3.5 w-3.5" />
                  <span>{resumeMutation.isPending ? "正在恢复..." : "断点恢复 (Resume)"}</span>
                </Button>
              )}

              {allowedActions.includes("retry_failed") && (
                <Button variant="warning" onClick={() => {
                  setForceRetry(false);
                  setShowRetryModal(true);
                }} disabled={retryFailedMutation.isPending} className="text-xs">
                  <span>{retryFailedMutation.isPending ? "重试中..." : "重试失败用例 (Retry Failed)"}</span>
                </Button>
              )}

              {launch.langfuse_experiment_url && (
                <a
                  href={launch.langfuse_experiment_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label="在 Langfuse 中查看"
                  className={buttonClassName("secondary", "text-xs")}
                >
                  <span>在 Langfuse 中查看</span>
                  <ExternalLink className="h-3.5 w-3.5" />
                </a>
              )}
            </>
          )}
        />
      </div>

      {actionError && (
        <div className="p-3 text-xs bg-fail-subtle border border-fail-border rounded-lg text-fail font-medium">
          {actionError}
        </div>
      )}

      {/* Cancellation Banner */}
      {launch.cancel_requested_at && launch.status !== "CANCELLED" && (
        <div className="p-3.5 text-xs bg-timeout-subtle border border-timeout-border rounded-xl text-timeout-strong flex items-center justify-between shadow-xs">
          <div className="flex items-center gap-2">
            <Clock className="w-4 h-4 text-timeout animate-spin" />
            <span>
              已收到协作取消请求，系统正在等待处于执行态的任务安全终止（状态过渡中：CANCELLING）。
            </span>
          </div>
          <span className="font-mono text-micro text-timeout">
            申请时间: {new Date(launch.cancel_requested_at).toLocaleTimeString("zh-CN")}
          </span>
        </div>
      )}

      {/* Status Reason */}
      {launch.status_reason && (
        <div className="p-3 text-xs bg-canvas border border-border rounded-xl text-foreground-secondary">
          <span className="font-semibold text-foreground mr-1.5">状态说明:</span>
          <span>{launch.status_reason}</span>
        </div>
      )}

      {/* Primary Status Banner (Dual Badges, Langfuse & Metrics) */}
      <Panel className="grid grid-cols-2 gap-4 p-5 shadow-xs sm:grid-cols-3 lg:grid-cols-5">
        <div>
          <span className="text-xs font-medium text-muted-foreground block mb-1.5">
            执行调度状态 (Execution Status)
          </span>
          <StatusBadge status={launch.status} />
        </div>

        <div>
          <span className="text-xs font-medium text-muted-foreground block mb-1.5">
            质量门禁结论 (Quality Conclusion)
          </span>
          <QualityBadge quality={launch.quality_conclusion} />
        </div>

        <div>
          <span className="text-xs font-medium text-muted-foreground block mb-1.5">
            Langfuse 同步状态 (Sync Status)
          </span>
          <div className="flex items-center gap-1.5">
            <SyncStatusBadge
              status={launch.langfuse_sync_status}
              data-testid="langfuse-sync-badge"
              className="font-mono"
            />
          </div>
          {launch.langfuse_sync_error && (
            <p className="text-micro text-fail mt-1 truncate" title={launch.langfuse_sync_error}>
              {launch.langfuse_sync_error}
            </p>
          )}
        </div>

        {/* Two different ratios share the word "pass" on this page. This cell is
            the live all-cases quality ratio; the comparison panel below reports
            a narrower comparable-cohort ratio. The label and the footnote name
            the denominator so neither can be read as execution success. */}
        <div data-testid="quality-pass-rate">
          <span
            id="quality-pass-rate-label"
            aria-describedby="quality-pass-rate-help"
            className="text-xs font-medium text-muted-foreground block mb-1.5"
          >
            质量通过率 (Quality Pass Rate)
          </span>
          <span className="text-base font-bold font-mono text-foreground">
            {itemsError ? (
              <span className="text-xs text-fail">暂不可用</span>
            ) : totalItems !== null && totalItems > 0 ? (
              <>
                <span className="text-pass">{passedItems}</span>
                <span className="text-muted-foreground font-normal"> / </span>
                <span>{totalItems}</span>
                <span className="text-xs text-muted-foreground font-normal ml-2">
                  ({(((passedItems ?? 0) / totalItems) * 100).toFixed(1)}%)
                </span>
              </>
            ) : (
              <span className="text-xs text-muted-foreground">尚未统计</span>
            )}
          </span>
          <span className="block mt-1 text-micro text-muted-foreground">
            统计范围：全部用例
          </span>
        </div>

        <div>
          <span className="text-xs font-medium text-muted-foreground block mb-1.5 flex items-center gap-1">
            <Clock className="w-3.5 h-3.5" /> 执行总耗时 (Duration)
          </span>
          <span className="text-sm font-semibold font-mono text-foreground">{durationText}</span>
        </div>
      </Panel>

      <p
        id="quality-pass-rate-help"
        data-testid="quality-pass-rate-help"
        className="text-micro text-muted-foreground"
      >
        质量通过率说明：分子为质量结论 PASS 的用例数，分母为当前返回的全部用例数。执行失败、超时、取消、评测跳过或不可评测的用例仍计入分母，但不计入分子，不代表质量结论为 FAIL。该比例不是执行成功率。
      </p>

      {/* Real-time Progress Board */}
      {progress && progress.total > 0 && (
        <div className="ui-panel p-5 shadow-xs space-y-3">
          <div className="flex items-center justify-between text-xs">
            <div className="flex items-center gap-2 font-bold text-foreground">
              <span>实时执行进度看板</span>
              <span className="font-mono text-primary">({progress.percentage}%)</span>
            </div>
            <div className="flex items-center gap-3 text-muted-foreground font-mono text-micro">
              <span>总调用: {progress.attempts} 次</span>
              <span>重试: {progress.retries} 次</span>
            </div>
          </div>

          {/* Progress Bar — derived from the state list so that no state can be
              silently dropped. Queued items are included; a launch whose work
              has not started yet must not render an empty meter. */}
          <div
            data-testid="progress-meter"
            className="w-full bg-surface-muted rounded-full h-2.5 overflow-hidden flex"
          >
            {progressSegments.map((segment) => (
              <div
                key={segment.state}
                data-segment={segment.state}
                data-share={segment.count}
                className={clsx("h-full transition-all duration-300", segment.fillClass)}
                style={{ width: `${progress.total > 0 ? (segment.count / progress.total) * 100 : 0}%` }}
                title={`${segment.label}: ${segment.count}`}
              />
            ))}
          </div>

          {/* Grid Counts */}
          <div className="grid grid-cols-4 sm:grid-cols-8 gap-2 pt-1">
            {/* The denominator, not a state: flat and untinted so it cannot be
                mistaken for one of the seven status buckets. */}
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
      )}

      {/* Frozen Manifest 4-Dimension Snapshot Overview */}
      <div className="ui-panel p-5 shadow-xs space-y-4">
        <div className="flex items-center justify-between border-b border-border pb-3">
          <div className="flex items-center gap-2">
            <CheckCircle2 className="w-4 h-4 text-primary" />
            <h3 className="text-sm font-bold text-foreground">
              四维不可变冻结快照 (Frozen Manifest Snapshot)
            </h3>
            <span
              data-testid="manifest-schema-version"
              className="px-2 py-0.5 rounded text-micro font-mono bg-primary-subtle text-primary-strong border border-primary-border font-semibold"
            >
              Schema v{manifest.schema_version || manifest.manifest_version || "1.0"}
            </span>
          </div>

          <div className="flex items-center gap-3">
            <span className="text-micro text-muted-foreground hidden sm:inline">
              严格固定执行时规格，不随 Registry 后续变更漂移
            </span>
            <button
              onClick={() => setShowRawManifest(!showRawManifest)}
              className="inline-flex items-center gap-1 text-xs text-primary hover:text-primary-strong font-medium cursor-pointer"
            >
              <FileCode className="w-3.5 h-3.5" />
              <span>{showRawManifest ? "收起 JSON" : "查看完整 Manifest JSON"}</span>
            </button>
          </div>
        </div>

        {/* 4-Dimension Grid */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 text-xs">
          {/* Dimension 1: Agent Snapshot */}
          <div className="p-3.5 bg-canvas/80 rounded-lg border border-border space-y-2">
            <div className="flex items-center gap-1.5 font-semibold text-foreground">
              <Bot className="w-4 h-4 text-primary" />
              <span>1. Agent 规格快照</span>
            </div>
            <div className="space-y-1 text-foreground-secondary">
              <div>
                <span className="text-muted-foreground">Agent:</span>{" "}
                <span className="font-semibold">{manifestAgent.id || launch.agent_id}</span>
              </div>
              <div>
                <span className="text-muted-foreground">Version:</span>{" "}
                <span className="font-mono font-semibold">{manifestAgent.version || launch.agent_version}</span>
              </div>
              <div className="truncate" title={manifestAgent.endpoint || ""}>
                <span className="text-muted-foreground">Endpoint:</span>{" "}
                <span className="font-mono text-micro">{manifestAgent.endpoint || "-"}</span>
              </div>
              <div className="truncate" title={manifestAgent.spec_digest || ""}>
                <span className="text-muted-foreground">Digest:</span>{" "}
                <span className="font-mono text-micro">{manifestAgent.spec_digest?.slice(0, 12)}...</span>
              </div>
            </div>
          </div>

          {/* Dimension 2: Dataset Snapshot */}
          <div className="p-3.5 bg-canvas/80 rounded-lg border border-border space-y-2">
            <div className="flex items-center gap-1.5 font-semibold text-foreground">
              <Database className="w-4 h-4 text-primary" />
              <span>2. Dataset 快照</span>
            </div>
            <div className="space-y-1 text-foreground-secondary">
              <div>
                <span className="text-muted-foreground">Name:</span>{" "}
                <span className="font-semibold">{manifest.dataset?.dataset_name || manifest.dataset?.name || launch.dataset_name}</span>
              </div>
              <div>
                <span className="text-muted-foreground">Version:</span>{" "}
                <span className="font-mono text-micro block truncate" title={manifest.dataset?.dataset_version || manifest.dataset?.version || launch.dataset_version || ""}>
                  {manifest.dataset?.dataset_version || manifest.dataset?.version || launch.dataset_version || "-"}
                </span>
              </div>
              <div className="truncate" title={manifest.dataset?.snapshot_digest || ""}>
                <span className="text-muted-foreground">Digest:</span>{" "}
                <span data-testid="dataset-snapshot-digest" className="font-mono text-micro">
                  {manifest.dataset?.snapshot_digest ? `${manifest.dataset.snapshot_digest.slice(0, 12)}...` : "-"}
                </span>
              </div>
              <div>
                <span className="text-muted-foreground">Items:</span>{" "}
                <span className="font-mono font-semibold">
                  {manifest.dataset?.items_count ?? (manifest.dataset as any)?.items?.length ?? totalItems}
                </span>
              </div>
            </div>
          </div>

          {/* Dimension 3: Evaluators */}
          <div className="p-3.5 bg-canvas/80 rounded-lg border border-border space-y-2">
            <div className="flex items-center gap-1.5 font-semibold text-foreground">
              <Zap className="w-4 h-4 text-timeout" />
              <span>3. 评测门禁指标 ({manifestEvaluators.length})</span>
            </div>
            <div className="flex flex-wrap gap-1">
              {manifestEvaluators.map((ev) => {
                const evalId = ev.id || ev.name;
                return (
                  <span
                    key={evalId}
                    className="px-2 py-0.5 rounded text-micro font-mono bg-surface border border-border text-foreground-secondary"
                  >
                    {evalId}
                  </span>
                );
              })}
            </div>
          </div>

          {/* Dimension 4: Execution Policy & Runner */}
          <div className="p-3.5 bg-canvas/80 rounded-lg border border-border space-y-2">
            <div className="flex items-center gap-1.5 font-semibold text-foreground">
              <Sliders className="w-4 h-4 text-pass" />
              <span>4. Runner 与调度策略</span>
            </div>
            <div className="space-y-1 text-foreground-secondary">
              <div>
                <span className="text-muted-foreground">Runner Ver:</span>{" "}
                <span data-testid="runner-version" className="font-mono text-micro">
                  {manifestRunner.runner_version || "1.0.0"}
                </span>
              </div>
              <div className="truncate" title={manifestRunner.mapping_engine_version || ""}>
                <span className="text-muted-foreground">Engine:</span>{" "}
                <span className="font-mono text-micro">{manifestRunner.mapping_engine_version || "-"}</span>
              </div>
              <div>
                <span className="text-muted-foreground">Concurrency:</span>{" "}
                <span className="font-semibold">{manifestPolicy.max_concurrency ?? manifestRunner.concurrency ?? 1}</span>
              </div>
              <div>
                <span className="text-muted-foreground">Timeout:</span>{" "}
                <span>{manifestPolicy.timeout_seconds ?? 30}s</span>
              </div>
              <div>
                <span className="text-muted-foreground">Retries:</span>{" "}
                <span>{manifestPolicy.max_retries ?? 0}</span>
              </div>
            </div>
          </div>
        </div>

        {/* Raw Manifest Viewer Drawer */}
        {showRawManifest && (
          <div className="pt-3 border-t border-border">
            <span className="text-xs font-semibold text-foreground-secondary block mb-2">
              完整冻结 Manifest 快照 (launch.manifest)
            </span>
            <JsonViewer data={manifest} title="Immutable Manifest JSON" />
          </div>
        )}
      </div>

      <ComparisonReport
        launchId={launch.id}
        launchStatus={launch.status}
        environment={manifest.comparison?.environment || "production"}
      />

      {/* Items Execution & Evaluations */}
      {isItemsLoading && <LoadingState message="正在加载用例明细与得分..." />}
      {itemsError && <ErrorState message={formatApiError(itemsError)} onRetry={() => refetchItems()} />}
      {!isItemsLoading && !itemsError && (
        <ItemTable items={items} />
      )}

      {/* Retry Failed Confirmation Modal */}
      <Modal
        open={showRetryModal}
        onClose={() => setShowRetryModal(false)}
        title="重试失败用例 (Retry Failed Items)"
        tone="danger"
        // A retry already submitted must not be abandoned by a stray click.
        dismissable={!retryFailedMutation.isPending}
        footer={
          <>
            <Button
              type="button"
              variant="secondary"
              className="text-xs"
              onClick={() => setShowRetryModal(false)}
            >
              取消
            </Button>
            <Button
              type="button"
              variant="warning-solid"
              className="text-xs"
              disabled={retryFailedMutation.isPending}
              onClick={() => retryFailedMutation.mutate(forceRetry)}
            >
              {retryFailedMutation.isPending ? "正在提交重试..." : "确认重新调度"}
            </Button>
          </>
        }
      >
        <div className="p-6 space-y-4">
          <p className="text-xs text-foreground-secondary leading-relaxed">
            系统将仅针对执行失败 (<code className="text-fail font-mono font-semibold">FAILED</code>) 或超时 (<code className="text-timeout font-mono font-semibold">TIMED_OUT</code>) 的用例发起全新调度代次 (generation + 1)，已成功的用例将被严格保护并跳过。
          </p>

          <div className="p-3 bg-timeout-subtle border border-timeout-border rounded-xl">
            <label className="flex items-start gap-2.5 cursor-pointer">
              <input
                type="checkbox"
                checked={forceRetry}
                onChange={(e) => setForceRetry(e.target.checked)}
                className="mt-0.5 rounded text-primary focus:ring-focus"
              />
              <span className="text-xs text-timeout-strong">
                <strong className="block font-semibold">强制重试非幂等可能已发送用例 (Force Replay)</strong>
                若用例在 Worker 崩溃前可能已将请求发出且接口非幂等，勾选此项以确认允许二次执行。
              </span>
            </label>
          </div>
        </div>
      </Modal>
    </div>
  );
};
