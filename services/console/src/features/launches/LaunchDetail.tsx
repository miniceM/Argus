import React, { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
import { JsonViewer } from "../../components/JsonViewer";
import { ItemTable } from "./ItemTable";
import { ACTIVE_LAUNCH_STATUSES, LaunchStatus } from "./LaunchesList";
import { ErrorState, LoadingState } from "../../components/StateViews";

type LaunchResponse = import("../../api/schema").components["schemas"]["ExperimentLaunchResponse"];
type ItemExecution = import("../../api/schema").components["schemas"]["ExperimentItemExecutionResponse"];

interface ManifestData {
  schema_version?: string;
  manifest_version?: string;
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
          className="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-500 hover:text-slate-800"
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
      {/* Navigation Breadcrumbs */}
      <div>
        <Link
          to="/launches"
          className="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-500 hover:text-slate-800 transition-colors mb-2"
        >
          <ArrowLeft className="w-3.5 h-3.5" />
          <span>返回评测列表</span>
        </Link>
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-indigo-50 border border-indigo-100 flex items-center justify-center text-indigo-600 font-bold">
              <Rocket className="w-6 h-6" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-xl font-bold text-slate-900 tracking-tight font-mono">
                  {launch.id}
                </h2>
              </div>
              <p className="text-xs text-slate-400 mt-0.5">
                创建时间: {new Date(launch.created_at).toLocaleString("zh-CN", { hour12: false })}
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2 self-start sm:self-auto">
            <button
              onClick={() => {
                refetchLaunch();
                refetchItems();
              }}
              disabled={isLaunchFetching}
              className="p-2 text-slate-500 hover:text-slate-700 bg-white hover:bg-slate-50 border border-slate-200 rounded-lg shadow-xs transition-colors cursor-pointer disabled:opacity-50"
              title="刷新"
            >
              <RefreshCw className={`w-4 h-4 ${isLaunchFetching ? "animate-spin text-indigo-600" : ""}`} />
            </button>

            {/* Run Button */}
            {allowedActions.includes("run") && (
              <button
                type="button"
                aria-label="立即执行评测"
                onClick={() => runMutation.mutate()}
                disabled={runMutation.isPending}
                className="inline-flex items-center gap-2 px-3.5 py-2 text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg shadow-xs transition-colors cursor-pointer disabled:opacity-50"
              >
                <Play className="w-3.5 h-3.5 fill-current" />
                <span>{runMutation.isPending ? "正在运行评测..." : "立即执行评测 (Run Evaluation)"}</span>
              </button>
            )}

            {/* Cancel Button */}
            {allowedActions.includes("cancel") && (
              <button
                type="button"
                onClick={() => cancelMutation.mutate()}
                disabled={cancelMutation.isPending}
                className="inline-flex items-center gap-1.5 px-3.5 py-2 text-xs font-semibold text-white bg-rose-600 hover:bg-rose-700 rounded-lg shadow-xs transition-colors cursor-pointer disabled:opacity-50"
              >
                <span>{cancelMutation.isPending ? "正在取消..." : "取消评测 (Cancel)"}</span>
              </button>
            )}

            {/* Resume Button */}
            {allowedActions.includes("resume") && (
              <button
                type="button"
                onClick={() => resumeMutation.mutate()}
                disabled={resumeMutation.isPending}
                className="inline-flex items-center gap-1.5 px-3.5 py-2 text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg shadow-xs transition-colors cursor-pointer disabled:opacity-50"
              >
                <RefreshCw className="w-3.5 h-3.5" />
                <span>{resumeMutation.isPending ? "正在恢复..." : "断点恢复 (Resume)"}</span>
              </button>
            )}

            {/* Retry Failed Button */}
            {allowedActions.includes("retry_failed") && (
              <button
                type="button"
                onClick={() => {
                  setForceRetry(false);
                  setShowRetryModal(true);
                }}
                disabled={retryFailedMutation.isPending}
                className="inline-flex items-center gap-1.5 px-3.5 py-2 text-xs font-semibold text-white bg-amber-600 hover:bg-amber-700 rounded-lg shadow-xs transition-colors cursor-pointer disabled:opacity-50"
              >
                <span>{retryFailedMutation.isPending ? "重试中..." : "重试失败用例 (Retry Failed)"}</span>
              </button>
            )}

            {/* Langfuse Deep Link */}
            {launch.langfuse_experiment_url && (
              <a
                href={launch.langfuse_experiment_url}
                target="_blank"
                rel="noopener noreferrer"
                aria-label="在 Langfuse 中查看"
                className="inline-flex items-center gap-2 px-3.5 py-2 text-xs font-semibold text-white bg-slate-900 hover:bg-slate-800 rounded-lg shadow-xs transition-colors"
              >
                <span>在 Langfuse 中查看</span>
                <ExternalLink className="w-3.5 h-3.5" />
              </a>
            )}
          </div>
        </div>
      </div>

      {actionError && (
        <div className="p-3 text-xs bg-rose-50 border border-rose-200 rounded-lg text-rose-700 font-medium">
          {actionError}
        </div>
      )}

      {/* Cancellation Banner */}
      {launch.cancel_requested_at && launch.status !== "CANCELLED" && (
        <div className="p-3.5 text-xs bg-amber-50 border border-amber-200 rounded-xl text-amber-900 flex items-center justify-between shadow-xs">
          <div className="flex items-center gap-2">
            <Clock className="w-4 h-4 text-amber-600 animate-spin" />
            <span>
              已收到协作取消请求，系统正在等待处于执行态的任务安全终止（状态过渡中：CANCELLING）。
            </span>
          </div>
          <span className="font-mono text-[11px] text-amber-700">
            申请时间: {new Date(launch.cancel_requested_at).toLocaleTimeString("zh-CN")}
          </span>
        </div>
      )}

      {/* Status Reason */}
      {launch.status_reason && (
        <div className="p-3 text-xs bg-slate-50 border border-slate-200 rounded-xl text-slate-700">
          <span className="font-semibold text-slate-900 mr-1.5">状态说明:</span>
          <span>{launch.status_reason}</span>
        </div>
      )}

      {/* Primary Status Banner (Dual Badges, Langfuse & Metrics) */}
      <div className="bg-white p-5 rounded-xl border border-slate-200 shadow-xs grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-4">
        <div>
          <span className="text-xs font-medium text-slate-400 block mb-1.5">
            执行调度状态 (Execution Status)
          </span>
          <StatusBadge status={launch.status} />
        </div>

        <div>
          <span className="text-xs font-medium text-slate-400 block mb-1.5">
            质量门禁结论 (Quality Conclusion)
          </span>
          <QualityBadge quality={launch.quality_conclusion} />
        </div>

        <div>
          <span className="text-xs font-medium text-slate-400 block mb-1.5">
            Langfuse 同步状态 (Sync Status)
          </span>
          <div className="flex items-center gap-1.5">
            <span
              data-testid="langfuse-sync-badge"
              className={`px-2.5 py-0.5 rounded-full text-xs font-semibold font-mono inline-flex items-center gap-1 ${
                launch.langfuse_sync_status === "SYNCED"
                  ? "bg-emerald-50 text-emerald-700 border border-emerald-200"
                  : launch.langfuse_sync_status === "FAILED"
                  ? "bg-rose-50 text-rose-700 border border-rose-200"
                  : launch.langfuse_sync_status === "NOT_APPLICABLE"
                  ? "bg-slate-100 text-slate-600 border border-slate-200"
                  : "bg-amber-50 text-amber-700 border border-amber-200"
              }`}
            >
              {launch.langfuse_sync_status}
            </span>
          </div>
          {launch.langfuse_sync_error && (
            <p className="text-[11px] text-rose-600 mt-1 truncate" title={launch.langfuse_sync_error}>
              {launch.langfuse_sync_error}
            </p>
          )}
        </div>

        <div>
          <span className="text-xs font-medium text-slate-400 block mb-1.5">
            用例通过率 (Pass Rate)
          </span>
          <span className="text-base font-bold font-mono text-slate-900">
            {itemsError ? (
              <span className="text-xs text-rose-600">暂不可用</span>
            ) : totalItems !== null && totalItems > 0 ? (
              <>
                <span className="text-emerald-600">{passedItems}</span>
                <span className="text-slate-400 font-normal"> / </span>
                <span>{totalItems}</span>
                <span className="text-xs text-slate-500 font-normal ml-2">
                  ({(((passedItems ?? 0) / totalItems) * 100).toFixed(1)}%)
                </span>
              </>
            ) : (
              <span className="text-xs text-slate-400">尚未统计</span>
            )}
          </span>
        </div>

        <div>
          <span className="text-xs font-medium text-slate-400 block mb-1.5 flex items-center gap-1">
            <Clock className="w-3.5 h-3.5" /> 执行总耗时 (Duration)
          </span>
          <span className="text-sm font-semibold font-mono text-slate-800">{durationText}</span>
        </div>
      </div>

      {/* Real-time Progress Board */}
      {progress && progress.total > 0 && (
        <div className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-3">
          <div className="flex items-center justify-between text-xs">
            <div className="flex items-center gap-2 font-bold text-slate-800">
              <span>实时执行进度看板</span>
              <span className="font-mono text-indigo-600">({progress.percentage}%)</span>
            </div>
            <div className="flex items-center gap-3 text-slate-500 font-mono text-[11px]">
              <span>总调用: {progress.attempts} 次</span>
              <span>重试: {progress.retries} 次</span>
            </div>
          </div>

          {/* Progress Bar */}
          <div className="w-full bg-slate-100 rounded-full h-2.5 overflow-hidden flex">
            <div
              className="bg-emerald-500 h-full transition-all duration-300"
              style={{ width: `${progress.total > 0 ? (progress.succeeded / progress.total) * 100 : 0}%` }}
              title={`成功: ${progress.succeeded}`}
            />
            <div
              className="bg-rose-500 h-full transition-all duration-300"
              style={{ width: `${progress.total > 0 ? (progress.failed / progress.total) * 100 : 0}%` }}
              title={`失败: ${progress.failed}`}
            />
            <div
              className="bg-amber-400 h-full transition-all duration-300"
              style={{ width: `${progress.total > 0 ? (progress.timed_out / progress.total) * 100 : 0}%` }}
              title={`超时: ${progress.timed_out}`}
            />
            <div
              className="bg-sky-400 h-full transition-all duration-300 animate-pulse"
              style={{ width: `${progress.total > 0 ? (progress.running / progress.total) * 100 : 0}%` }}
              title={`运行中: ${progress.running}`}
            />
            <div
              className="bg-yellow-400 h-full transition-all duration-300"
              style={{ width: `${progress.total > 0 ? (progress.retry_wait / progress.total) * 100 : 0}%` }}
              title={`等待重试: ${progress.retry_wait}`}
            />
            <div
              className="bg-slate-300 h-full transition-all duration-300"
              style={{ width: `${progress.total > 0 ? (progress.cancelled / progress.total) * 100 : 0}%` }}
              title={`已取消: ${progress.cancelled}`}
            />
          </div>

          {/* Grid Counts */}
          <div className="grid grid-cols-4 sm:grid-cols-8 gap-2 pt-1">
            <div className="text-center p-2 rounded-lg bg-slate-50 border border-slate-100">
              <span className="text-[11px] text-slate-400 block">总用例</span>
              <span className="text-sm font-bold font-mono text-slate-700">{progress.total}</span>
            </div>
            <div className="text-center p-2 rounded-lg bg-indigo-50/60 border border-indigo-100">
              <span className="text-[11px] text-indigo-600 block">排队中</span>
              <span className="text-sm font-bold font-mono text-indigo-700">{progress.queued + progress.pending}</span>
            </div>
            <div className="text-center p-2 rounded-lg bg-sky-50/60 border border-sky-100">
              <span className="text-[11px] text-sky-600 block">运行中</span>
              <span className="text-sm font-bold font-mono text-sky-700">{progress.running}</span>
            </div>
            <div className="text-center p-2 rounded-lg bg-emerald-50/60 border border-emerald-100">
              <span className="text-[11px] text-emerald-600 block">成功</span>
              <span className="text-sm font-bold font-mono text-emerald-700">{progress.succeeded}</span>
            </div>
            <div className="text-center p-2 rounded-lg bg-rose-50/60 border border-rose-100">
              <span className="text-[11px] text-rose-600 block">失败</span>
              <span className="text-sm font-bold font-mono text-rose-700">{progress.failed}</span>
            </div>
            <div className="text-center p-2 rounded-lg bg-amber-50/60 border border-amber-100">
              <span className="text-[11px] text-amber-600 block">超时</span>
              <span className="text-sm font-bold font-mono text-amber-700">{progress.timed_out}</span>
            </div>
            <div className="text-center p-2 rounded-lg bg-yellow-50/60 border border-yellow-100">
              <span className="text-[11px] text-yellow-600 block">等待重试</span>
              <span className="text-sm font-bold font-mono text-yellow-700">{progress.retry_wait}</span>
            </div>
            <div className="text-center p-2 rounded-lg bg-gray-50 border border-gray-200">
              <span className="text-[11px] text-gray-500 block">已取消</span>
              <span className="text-sm font-bold font-mono text-gray-600">{progress.cancelled}</span>
            </div>
          </div>
        </div>
      )}

      {/* Frozen Manifest 4-Dimension Snapshot Overview */}
      <div className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-4">
        <div className="flex items-center justify-between border-b border-slate-100 pb-3">
          <div className="flex items-center gap-2">
            <CheckCircle2 className="w-4 h-4 text-indigo-600" />
            <h3 className="text-sm font-bold text-slate-900">
              四维不可变冻结快照 (Frozen Manifest Snapshot)
            </h3>
            <span
              data-testid="manifest-schema-version"
              className="px-2 py-0.5 rounded text-[11px] font-mono bg-indigo-50 text-indigo-700 border border-indigo-200 font-semibold"
            >
              Schema v{manifest.schema_version || manifest.manifest_version || "1.0"}
            </span>
          </div>

          <div className="flex items-center gap-3">
            <span className="text-[11px] text-slate-400 hidden sm:inline">
              严格固定执行时规格，不随 Registry 后续变更漂移
            </span>
            <button
              onClick={() => setShowRawManifest(!showRawManifest)}
              className="inline-flex items-center gap-1 text-xs text-indigo-600 hover:text-indigo-800 font-medium cursor-pointer"
            >
              <FileCode className="w-3.5 h-3.5" />
              <span>{showRawManifest ? "收起 JSON" : "查看完整 Manifest JSON"}</span>
            </button>
          </div>
        </div>

        {/* 4-Dimension Grid */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 text-xs">
          {/* Dimension 1: Agent Snapshot */}
          <div className="p-3.5 bg-slate-50/80 rounded-lg border border-slate-100 space-y-2">
            <div className="flex items-center gap-1.5 font-semibold text-slate-800">
              <Bot className="w-4 h-4 text-indigo-600" />
              <span>1. Agent 规格快照</span>
            </div>
            <div className="space-y-1 text-slate-600">
              <div>
                <span className="text-slate-400">Agent:</span>{" "}
                <span className="font-semibold">{manifestAgent.id || launch.agent_id}</span>
              </div>
              <div>
                <span className="text-slate-400">Version:</span>{" "}
                <span className="font-mono font-semibold">{manifestAgent.version || launch.agent_version}</span>
              </div>
              <div className="truncate" title={manifestAgent.endpoint || ""}>
                <span className="text-slate-400">Endpoint:</span>{" "}
                <span className="font-mono text-[11px]">{manifestAgent.endpoint || "-"}</span>
              </div>
              <div className="truncate" title={manifestAgent.spec_digest || ""}>
                <span className="text-slate-400">Digest:</span>{" "}
                <span className="font-mono text-[11px]">{manifestAgent.spec_digest?.slice(0, 12)}...</span>
              </div>
            </div>
          </div>

          {/* Dimension 2: Dataset Snapshot */}
          <div className="p-3.5 bg-slate-50/80 rounded-lg border border-slate-100 space-y-2">
            <div className="flex items-center gap-1.5 font-semibold text-slate-800">
              <Database className="w-4 h-4 text-indigo-600" />
              <span>2. Dataset 快照</span>
            </div>
            <div className="space-y-1 text-slate-600">
              <div>
                <span className="text-slate-400">Name:</span>{" "}
                <span className="font-semibold">{manifest.dataset?.dataset_name || manifest.dataset?.name || launch.dataset_name}</span>
              </div>
              <div>
                <span className="text-slate-400">Version:</span>{" "}
                <span className="font-mono text-[11px] block truncate" title={manifest.dataset?.dataset_version || manifest.dataset?.version || launch.dataset_version || ""}>
                  {manifest.dataset?.dataset_version || manifest.dataset?.version || launch.dataset_version || "-"}
                </span>
              </div>
              <div className="truncate" title={manifest.dataset?.snapshot_digest || ""}>
                <span className="text-slate-400">Digest:</span>{" "}
                <span data-testid="dataset-snapshot-digest" className="font-mono text-[11px]">
                  {manifest.dataset?.snapshot_digest ? `${manifest.dataset.snapshot_digest.slice(0, 12)}...` : "-"}
                </span>
              </div>
              <div>
                <span className="text-slate-400">Items:</span>{" "}
                <span className="font-mono font-semibold">
                  {manifest.dataset?.items_count ?? (manifest.dataset as any)?.items?.length ?? totalItems}
                </span>
              </div>
            </div>
          </div>

          {/* Dimension 3: Evaluators */}
          <div className="p-3.5 bg-slate-50/80 rounded-lg border border-slate-100 space-y-2">
            <div className="flex items-center gap-1.5 font-semibold text-slate-800">
              <Zap className="w-4 h-4 text-amber-500" />
              <span>3. 评测门禁指标 ({manifestEvaluators.length})</span>
            </div>
            <div className="flex flex-wrap gap-1">
              {manifestEvaluators.map((ev) => {
                const evalId = ev.id || ev.name;
                return (
                  <span
                    key={evalId}
                    className="px-2 py-0.5 rounded text-[11px] font-mono bg-white border border-slate-200 text-slate-700"
                  >
                    {evalId}
                  </span>
                );
              })}
            </div>
          </div>

          {/* Dimension 4: Execution Policy & Runner */}
          <div className="p-3.5 bg-slate-50/80 rounded-lg border border-slate-100 space-y-2">
            <div className="flex items-center gap-1.5 font-semibold text-slate-800">
              <Sliders className="w-4 h-4 text-emerald-600" />
              <span>4. Runner 与调度策略</span>
            </div>
            <div className="space-y-1 text-slate-600">
              <div>
                <span className="text-slate-400">Runner Ver:</span>{" "}
                <span data-testid="runner-version" className="font-mono text-[11px]">
                  {manifestRunner.runner_version || "1.0.0"}
                </span>
              </div>
              <div className="truncate" title={manifestRunner.mapping_engine_version || ""}>
                <span className="text-slate-400">Engine:</span>{" "}
                <span className="font-mono text-[11px]">{manifestRunner.mapping_engine_version || "-"}</span>
              </div>
              <div>
                <span className="text-slate-400">Concurrency:</span>{" "}
                <span className="font-semibold">{manifestPolicy.max_concurrency ?? manifestRunner.concurrency ?? 1}</span>
              </div>
              <div>
                <span className="text-slate-400">Timeout:</span>{" "}
                <span>{manifestPolicy.timeout_seconds ?? 30}s</span>
              </div>
              <div>
                <span className="text-slate-400">Retries:</span>{" "}
                <span>{manifestPolicy.max_retries ?? 0}</span>
              </div>
            </div>
          </div>
        </div>

        {/* Raw Manifest Viewer Drawer */}
        {showRawManifest && (
          <div className="pt-3 border-t border-slate-100">
            <span className="text-xs font-semibold text-slate-600 block mb-2">
              完整冻结 Manifest 快照 (launch.manifest)
            </span>
            <JsonViewer data={manifest} title="Immutable Manifest JSON" />
          </div>
        )}
      </div>

      {/* Items Execution & Evaluations */}
      {isItemsLoading && <LoadingState message="正在加载用例明细与得分..." />}
      {itemsError && <ErrorState message={formatApiError(itemsError)} onRetry={() => refetchItems()} />}
      {!isItemsLoading && !itemsError && (
        <ItemTable items={items} />
      )}

      {/* Retry Failed Confirmation Modal */}
      {showRetryModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 backdrop-blur-xs p-4">
          <div className="bg-white rounded-2xl shadow-xl max-w-md w-full p-6 space-y-4 border border-slate-200">
            <h3 className="text-base font-bold text-slate-900">重试失败用例 (Retry Failed Items)</h3>
            <p className="text-xs text-slate-600 leading-relaxed">
              系统将仅针对执行失败 (<code className="text-rose-600 font-mono font-semibold">FAILED</code>) 或超时 (<code className="text-amber-600 font-mono font-semibold">TIMED_OUT</code>) 的用例发起全新调度代次 (generation + 1)，已成功的用例将被严格保护并跳过。
            </p>

            <div className="p-3 bg-amber-50 border border-amber-200 rounded-xl space-y-2">
              <label className="flex items-start gap-2.5 cursor-pointer">
                <input
                  type="checkbox"
                  checked={forceRetry}
                  onChange={(e) => setForceRetry(e.target.checked)}
                  className="mt-0.5 rounded text-indigo-600 focus:ring-indigo-500"
                />
                <span className="text-xs text-amber-900">
                  <strong className="block font-semibold">强制重试非幂等可能已发送用例 (Force Replay)</strong>
                  若用例在 Worker 崩溃前可能已将请求发出且接口非幂等，勾选此项以确认允许二次执行。
                </span>
              </label>
            </div>

            <div className="flex items-center justify-end gap-3 pt-2">
              <button
                type="button"
                onClick={() => setShowRetryModal(false)}
                className="px-3 py-1.5 text-xs font-semibold text-slate-600 hover:text-slate-800 cursor-pointer"
              >
                取消
              </button>
              <button
                type="button"
                disabled={retryFailedMutation.isPending}
                onClick={() => retryFailedMutation.mutate(forceRetry)}
                className="px-4 py-2 text-xs font-semibold text-white bg-amber-600 hover:bg-amber-700 rounded-lg shadow-xs cursor-pointer disabled:opacity-50"
              >
                {retryFailedMutation.isPending ? "正在提交重试..." : "确认重新调度"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
