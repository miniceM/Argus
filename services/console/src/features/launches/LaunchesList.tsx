import React, { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ExternalLink,
  Filter,
  Plus,
  RefreshCw,
  Rocket,
} from "lucide-react";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";
import { StatusBadge } from "../../components/StatusBadge";
import { QualityBadge } from "../../components/QualityBadge";
import { EmptyState, ErrorState, LoadingState } from "../../components/StateViews";

type LaunchResponse = import("../../api/schema").components["schemas"]["ExperimentLaunchResponse"];

export const LAUNCH_STATUSES = [
  "PENDING",
  "QUEUED",
  "RUNNING",
  "COMPLETED",
  "PARTIAL_FAILED",
  "FAILED",
  "CANCELLING",
  "CANCELLED",
] as const;

export type LaunchStatus = (typeof LAUNCH_STATUSES)[number];

export const ACTIVE_LAUNCH_STATUSES = new Set<LaunchStatus>([
  "PENDING",
  "QUEUED",
  "RUNNING",
  "CANCELLING",
]);

export const LAUNCH_STATUS_LABELS: Record<LaunchStatus, string> = {
  PENDING: "PENDING (准备中)",
  QUEUED: "QUEUED (队列中)",
  RUNNING: "RUNNING (运行中)",
  COMPLETED: "COMPLETED (已完成)",
  PARTIAL_FAILED: "PARTIAL_FAILED (部分失败)",
  FAILED: "FAILED (失败)",
  CANCELLING: "CANCELLING (取消中)",
  CANCELLED: "CANCELLED (已取消)",
};

export const LaunchesList: React.FC = () => {
  const [filterAgent, setFilterAgent] = useState<string>("");
  const [filterStatus, setFilterStatus] = useState<string>("");
  const [filterQuality, setFilterQuality] = useState<string>("");

  const { data: launches, isLoading, error, refetch, isFetching } = useQuery<LaunchResponse[]>({
    queryKey: queryKeys.launches.list({
      agent_id: filterAgent || undefined,
      status: filterStatus || undefined,
      quality_conclusion: filterQuality || undefined,
    }),
    queryFn: async () => {
      const res = await api.GET("/api/v1/experiment-launches", {
        params: {
          query: {
            agent_id: filterAgent || undefined,
            status: (filterStatus as any) || undefined,
            quality_conclusion: (filterQuality as any) || undefined,
          },
        },
      });
      if (res.error) throw res.error;
      const list = Array.isArray(res.data) ? res.data : [res.data];
      return list as LaunchResponse[];
    },
    refetchInterval: (query) => {
      const data = query.state.data;
      const hasActive = data?.some((l) =>
        ACTIVE_LAUNCH_STATUSES.has(l.status.toUpperCase() as LaunchStatus)
      );
      return hasActive ? 2000 : false;
    },
  });

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h2 className="text-xl font-bold text-slate-900 tracking-tight">评测发射台 (Experiment Launches)</h2>
          <p className="text-xs text-slate-500 mt-1">
            查看、检索与触发 Agent 自动化评测任务，所有历史记录均严格锁定不可变快照 (Frozen Manifest)。
          </p>
        </div>

        <div className="flex items-center gap-3">
          <button
            onClick={() => refetch()}
            disabled={isFetching}
            className="p-2 text-slate-500 hover:text-slate-700 bg-white hover:bg-slate-50 border border-slate-200 rounded-lg shadow-xs transition-colors cursor-pointer disabled:opacity-50"
            title="刷新列表"
          >
            <RefreshCw className={`w-4 h-4 ${isFetching ? "animate-spin text-indigo-600" : ""}`} />
          </button>
          <Link
            to="/launches/new"
            className="inline-flex items-center gap-2 px-4 py-2 text-sm font-semibold text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg shadow-xs transition-colors cursor-pointer"
          >
            <Plus className="w-4 h-4" />
            <span>新建评测 (New Evaluation)</span>
          </Link>
        </div>
      </div>

      {/* Filter Bar */}
      <div className="bg-white p-4 rounded-xl border border-slate-200 shadow-xs flex flex-wrap items-center gap-3 text-xs">
        <div className="flex items-center gap-1.5 text-slate-400 font-semibold uppercase tracking-wider text-[11px] mr-1">
          <Filter className="w-3.5 h-3.5" />
          <span>过滤筛选:</span>
        </div>

        {/* Agent Filter */}
        <div className="relative min-w-[180px]">
          <input
            type="text"
            placeholder="按 Agent ID 过滤..."
            value={filterAgent}
            onChange={(e) => setFilterAgent(e.target.value)}
            className="w-full px-3 py-1.5 text-xs bg-slate-50 border border-slate-200 rounded-lg focus:outline-hidden focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 text-slate-800"
          />
        </div>

        {/* Status Filter */}
        <select
          value={filterStatus}
          onChange={(e) => setFilterStatus(e.target.value)}
          className="px-3 py-1.5 text-xs bg-slate-50 border border-slate-200 rounded-lg focus:outline-hidden focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 text-slate-700"
        >
          <option value="">全部执行状态 (Status)</option>
          {LAUNCH_STATUSES.map((status) => (
            <option key={status} value={status}>
              {LAUNCH_STATUS_LABELS[status]}
            </option>
          ))}
        </select>

        {/* Quality Filter */}
        <select
          value={filterQuality}
          onChange={(e) => setFilterQuality(e.target.value)}
          className="px-3 py-1.5 text-xs bg-slate-50 border border-slate-200 rounded-lg focus:outline-hidden focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 text-slate-700"
        >
          <option value="">全部质量结论 (Quality)</option>
          <option value="pass">PASS (通过)</option>
          <option value="fail">FAIL (未通过)</option>
          <option value="unknown">UNKNOWN (未知/未测)</option>
        </select>

        {(filterAgent || filterStatus || filterQuality) && (
          <button
            onClick={() => {
              setFilterAgent("");
              setFilterStatus("");
              setFilterQuality("");
            }}
            className="text-xs text-indigo-600 hover:text-indigo-800 font-semibold cursor-pointer underline ml-auto"
          >
            重置筛选
          </button>
        )}
      </div>

      {/* Content Area */}
      {isLoading && <LoadingState message="正在加载评测记录..." />}
      {error && <ErrorState message={formatApiError(error)} onRetry={() => refetch()} />}

      {!isLoading && !error && launches && launches.length === 0 && (
        <EmptyState
          title="暂无评测记录"
          description={
            filterAgent || filterStatus || filterQuality
              ? "未匹配到符合当前过滤条件的评测任务，请尝试重置筛选。"
              : "尚未发起过任何评测任务。点击上方按钮发起第一次评测吧！"
          }
          action={
            <Link
              to="/launches/new"
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold text-white bg-indigo-600 rounded-lg"
            >
              <Rocket className="w-3.5 h-3.5" />
              <span>新建评测</span>
            </Link>
          }
        />
      )}

      {!isLoading && !error && launches && launches.length > 0 && (
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden shadow-xs">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm text-slate-600">
              <thead className="bg-slate-50/75 border-b border-slate-200 text-xs font-semibold text-slate-500 uppercase tracking-wider">
                <tr>
                  <th className="px-6 py-3.5">评测任务 (Launch ID)</th>
                  <th className="px-6 py-3.5">被测 Agent @ 版本</th>
                  <th className="px-6 py-3.5">评测集 (Dataset @ Ver)</th>
                  <th className="px-6 py-3.5">执行状态 (Status)</th>
                  <th className="px-6 py-3.5">质量结论 (Quality)</th>
                  <th className="px-6 py-3.5">Langfuse 同步</th>
                  <th className="px-6 py-3.5">创建时间</th>
                  <th className="px-6 py-3.5 text-right">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {launches.map((launch) => (
                  <tr key={launch.id} className="hover:bg-slate-50/80 transition-colors">
                    <td className="px-6 py-4">
                      <Link
                        to={`/launches/${launch.id}`}
                        className="font-mono text-xs font-bold text-indigo-600 hover:text-indigo-800 hover:underline block"
                      >
                        {launch.id}
                      </Link>
                    </td>

                    <td className="px-6 py-4">
                      <div className="flex items-center gap-1 text-xs">
                        <span className="font-semibold text-slate-900">{launch.agent_id}</span>
                        <span className="text-slate-400">@</span>
                        <span className="font-mono text-slate-600">{launch.agent_version}</span>
                      </div>
                    </td>

                    <td className="px-6 py-4">
                      <div className="text-xs">
                        <span className="font-semibold text-slate-800">{launch.dataset_name}</span>
                        <span className="text-slate-400 font-mono text-[11px] block truncate max-w-[140px]" title={launch.dataset_version || ""}>
                          {launch.dataset_version || "-"}
                        </span>
                      </div>
                    </td>

                    <td className="px-6 py-4">
                      <div className="space-y-1">
                        <StatusBadge status={launch.status} />
                        {launch.progress && launch.progress.total > 0 && (
                          <div className="text-[11px] text-slate-500 font-mono">
                            <span>{launch.progress.percentage}%</span>
                            <span className="text-slate-400 ml-1">
                              ({launch.progress.completed}/{launch.progress.total})
                            </span>
                          </div>
                        )}
                      </div>
                    </td>

                    <td className="px-6 py-4">
                      <QualityBadge quality={launch.quality_conclusion} />
                    </td>

                    <td className="px-6 py-4 text-xs font-mono">
                      <span
                        className={`inline-flex items-center px-2 py-0.5 rounded text-[11px] font-semibold ${
                          launch.langfuse_sync_status === "SYNCED"
                            ? "bg-purple-50 text-purple-700 border border-purple-200"
                            : launch.langfuse_sync_status === "FAILED"
                            ? "bg-rose-50 text-rose-700 border border-rose-200"
                            : "bg-slate-100 text-slate-500"
                        }`}
                      >
                        {launch.langfuse_sync_status || "PENDING"}
                      </span>
                    </td>

                    <td className="px-6 py-4 text-xs text-slate-500 whitespace-nowrap">
                      {new Date(launch.created_at).toLocaleString("zh-CN", { hour12: false })}
                    </td>

                    <td className="px-6 py-4 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <Link
                          to={`/launches/${launch.id}`}
                          className="text-xs font-semibold text-indigo-600 hover:text-indigo-800"
                        >
                          详情
                        </Link>
                        {launch.langfuse_experiment_url && (
                          <a
                            href={launch.langfuse_experiment_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center gap-1 text-xs text-slate-500 hover:text-indigo-600"
                            title="在 Langfuse UI 中查看"
                          >
                            <ExternalLink className="w-3 h-3" />
                          </a>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
};
