import React, { useState } from "react";
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

  // 1. Fetch Launch Details
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
      const res = await api.GET("/api/v1/experiment-launches", {
        params: { query: { id: launchId } },
      });
      if (res.error) throw res.error;
      const list = Array.isArray(res.data) ? res.data : [res.data];
      const match = list.find((l) => l.id === launchId) || list[0];
      if (!match) throw new Error(`Launch ${launchId} not found`);
      return match as LaunchResponse;
    },
    enabled: Boolean(launchId),
    refetchInterval: (query) => {
      const data = query.state.data;
      if (data && (data.status === "PENDING" || data.status === "RUNNING")) {
        return 3000;
      }
      return false;
    },
  });

  // 2. Fetch Items
  const {
    data: items,
    isLoading: isItemsLoading,
    error: itemsError,
    refetch: refetchItems,
  } = useQuery<ItemExecution[]>({
    queryKey: queryKeys.launches.items(launchId || ""),
    queryFn: async () => {
      if (!launchId) return [];
      const res = await api.GET("/api/v1/experiment-launch-items", {
        params: { query: { launch_id: launchId } },
      });
      if (res.error) throw res.error;
      const list = Array.isArray(res.data) ? res.data : [res.data];
      return list as ItemExecution[];
    },
    enabled: Boolean(launchId),
    refetchInterval: () => {
      if (launch && (launch.status === "PENDING" || launch.status === "RUNNING")) {
        return 3000;
      }
      return false;
    },
  });

  // 3. Trigger Run Mutation
  const runMutation = useMutation({
    mutationFn: async () => {
      if (!launchId) return;
      setActionError(null);
      const res = await api.POST("/api/v1/experiment-launches/run", {
        body: { launch_id: launchId },
      });
      if (res.error) throw res.error;
      return res.data;
    },
    onSuccess: () => {
      if (launchId) {
        queryClient.invalidateQueries({ queryKey: queryKeys.launches.detail(launchId) });
        queryClient.invalidateQueries({ queryKey: queryKeys.launches.items(launchId) });
        queryClient.invalidateQueries({ queryKey: queryKeys.launches.list() });
      }
    },
    onError: (err) => {
      setActionError(formatApiError(err));
    },
  });

  if (isLaunchLoading) return <LoadingState message="正在加载评测任务与不可变快照..." />;
  if (launchError) return <ErrorState message={formatApiError(launchError)} onRetry={() => refetchLaunch()} />;
  if (!launch) return <ErrorState message="未找到对应的评测任务" />;

  const manifest = (launch.manifest || {}) as ManifestData;
  const manifestAgent = manifest.agent || {};
  const manifestPolicy = manifest.execution_policy || manifestAgent.execution_policy || {};
  const manifestEvaluators = manifest.evaluators || [];
  const manifestRunner = manifest.runner || {};

  // Metrics calculation from items
  const totalItems = items ? items.length : 0;
  const passedItems = items ? items.filter((i) => i.quality_conclusion?.toLowerCase() === "pass").length : 0;

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

          <div className="flex items-center gap-2 self-start sm:self-auto">
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

            {/* Run Button (Visible only when PENDING) */}
            {launch.status === "PENDING" && (
              <button
                type="button"
                onClick={() => runMutation.mutate()}
                disabled={runMutation.isPending}
                className="inline-flex items-center gap-2 px-4 py-2 text-sm font-semibold text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg shadow-xs transition-colors cursor-pointer disabled:opacity-50"
              >
                <Play className="w-4 h-4 fill-current" />
                <span>{runMutation.isPending ? "正在运行评测..." : "立即执行评测 (Run Evaluation)"}</span>
              </button>
            )}

            {/* Langfuse Deep Link (Direct from backend, never handcrafted) */}
            {launch.langfuse_experiment_url && (
              <a
                href={launch.langfuse_experiment_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-2 px-4 py-2 text-sm font-semibold text-white bg-slate-900 hover:bg-slate-800 rounded-lg shadow-xs transition-colors"
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

      {/* Primary Status Banner (Dual Badges & Metrics) */}
      <div className="bg-white p-5 rounded-xl border border-slate-200 shadow-xs grid grid-cols-2 sm:grid-cols-4 gap-4">
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
            用例通过率 (Pass Rate)
          </span>
          <span className="text-base font-bold font-mono text-slate-900">
            {totalItems > 0 ? (
              <>
                <span className="text-emerald-600">{passedItems}</span>
                <span className="text-slate-400 font-normal"> / </span>
                <span>{totalItems}</span>
                <span className="text-xs text-slate-500 font-normal ml-2">
                  ({((passedItems / totalItems) * 100).toFixed(1)}%)
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

      {/* Frozen Manifest 4-Dimension Snapshot Overview */}
      <div className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-4">
        <div className="flex items-center justify-between border-b border-slate-100 pb-3">
          <div className="flex items-center gap-2">
            <CheckCircle2 className="w-4 h-4 text-indigo-600" />
            <h3 className="text-sm font-bold text-slate-900">
              四维不可变冻结快照 (Frozen Manifest Snapshot)
            </h3>
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
                <span className="font-mono text-[11px]">{manifestRunner.runner_version || "1.0.0"}</span>
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
      {!isItemsLoading && !itemsError && items && (
        <ItemTable items={items} />
      )}
    </div>
  );
};
