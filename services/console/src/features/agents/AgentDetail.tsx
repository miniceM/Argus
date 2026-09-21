import React, { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, ArrowLeft, Bot, Calendar, ChevronRight, Layers, Plus, User } from "lucide-react";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";
import { CreateVersionDialog } from "./CreateVersionDialog";
import { EmptyState, ErrorState, LoadingState } from "../../components/StateViews";

export const AgentDetail: React.FC = () => {
  const { agentId } = useParams<{ agentId: string }>();
  const queryClient = useQueryClient();
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const { data: agent, isLoading: isAgentLoading, error: agentError, refetch: refetchAgent } = useQuery({
    queryKey: queryKeys.agents.detail(agentId || ""),
    queryFn: async () => {
      if (!agentId) throw new Error("缺少 Agent ID");
      const res = await api.GET("/api/v1/agents", {
        params: { query: { id: agentId } },
      });
      if (res.error) throw res.error;
      const data = Array.isArray(res.data) ? res.data[0] : res.data;
      return data as import("../../api/schema").components["schemas"]["AgentResponse"];
    },
    enabled: Boolean(agentId),
  });

  const { data: versions, isLoading: isVersionsLoading, error: versionsError, refetch: refetchVersions } = useQuery({
    queryKey: queryKeys.agents.versions(agentId || ""),
    queryFn: async () => {
      if (!agentId) throw new Error("缺少 Agent ID");
      const res = await api.GET("/api/v1/agent-versions", {
        params: { query: { agent_id: agentId } },
      });
      if (res.error) throw res.error;
      return Array.isArray(res.data) ? res.data : [res.data];
    },
    enabled: Boolean(agentId),
  });

  const archiveMutation = useMutation({
    mutationFn: async (versionTag: string) => {
      if (!agentId) return;
      setActionError(null);
      const res = await api.POST("/api/v1/agent-versions/archive", {
        body: { agent_id: agentId, version: versionTag },
      });
      if (res.error) throw res.error;
      return res.data;
    },
    onSuccess: () => {
      if (agentId) {
        queryClient.invalidateQueries({ queryKey: queryKeys.agents.versions(agentId) });
        queryClient.invalidateQueries({ queryKey: queryKeys.agents.detail(agentId) });
        queryClient.invalidateQueries({ queryKey: queryKeys.agents.list() });
      }
    },
    onError: (err) => {
      setActionError(formatApiError(err));
    },
  });

  if (isAgentLoading) return <LoadingState message="正在加载 Agent 详情..." />;
  if (agentError) return <ErrorState message={formatApiError(agentError)} onRetry={() => refetchAgent()} />;
  if (!agent) return <ErrorState message="未找到对应的 Agent" />;

  return (
    <div className="space-y-6">
      {/* Navigation Breadcrumbs */}
      <div>
        <Link
          to="/agents"
          className="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-500 hover:text-slate-800 transition-colors mb-2"
        >
          <ArrowLeft className="w-3.5 h-3.5" />
          <span>返回 Agent 列表</span>
        </Link>
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-indigo-50 border border-indigo-100 flex items-center justify-center text-indigo-600 font-bold">
              <Bot className="w-6 h-6" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-xl font-bold text-slate-900 tracking-tight">{agent.name}</h2>
                <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200">
                  {agent.status.toUpperCase()}
                </span>
              </div>
              <p className="text-xs text-slate-400 font-mono mt-0.5">{agent.id}</p>
            </div>
          </div>

          <button
            onClick={() => setIsCreateOpen(true)}
            className="inline-flex items-center gap-2 px-4 py-2 text-sm font-semibold text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg shadow-xs transition-colors cursor-pointer self-start sm:self-auto"
          >
            <Plus className="w-4 h-4" />
            <span>创建新版本</span>
          </button>
        </div>
      </div>

      {actionError && (
        <div className="p-3 text-xs bg-rose-50 border border-rose-200 rounded-lg text-rose-700 font-medium">
          {actionError}
        </div>
      )}

      {/* Metadata Card */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 bg-white p-5 rounded-xl border border-slate-200 shadow-xs">
        <div>
          <span className="text-xs font-medium text-slate-400 block mb-1">负责人 / 团队</span>
          <span className="text-sm font-semibold text-slate-800 flex items-center gap-1.5">
            <User className="w-4 h-4 text-slate-400" />
            <span>{agent.owner || "未指定"}</span>
          </span>
        </div>
        <div>
          <span className="text-xs font-medium text-slate-400 block mb-1">注册时间</span>
          <span className="text-sm font-semibold text-slate-800 flex items-center gap-1.5">
            <Calendar className="w-4 h-4 text-slate-400" />
            <span>{new Date(agent.created_at).toLocaleString("zh-CN", { hour12: false })}</span>
          </span>
        </div>
        <div>
          <span className="text-xs font-medium text-slate-400 block mb-1">详细描述</span>
          <span className="text-xs text-slate-600 line-clamp-2">
            {agent.description || "暂无描述"}
          </span>
        </div>
      </div>

      {/* Versions Section */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Layers className="w-4 h-4 text-indigo-600" />
            <h3 className="text-base font-bold text-slate-900">版本规格快照 (Agent Versions)</h3>
          </div>
          <span className="text-xs text-slate-400">所有版本规格创建后均为不可变快照</span>
        </div>

        {isVersionsLoading && <LoadingState message="正在加载版本记录..." />}
        {versionsError && <ErrorState message={formatApiError(versionsError)} onRetry={() => refetchVersions()} />}

        {!isVersionsLoading && !versionsError && versions && versions.length === 0 && (
          <EmptyState
            title="暂无任何版本"
            description="该 Agent 尚未创建任何版本规格。请点击上方按钮创建 1.0.0 版本。"
            action={
              <button
                onClick={() => setIsCreateOpen(true)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold text-white bg-indigo-600 rounded-lg"
              >
                <Plus className="w-3.5 h-3.5" />
                <span>立即创建版本</span>
              </button>
            }
          />
        )}

        {!isVersionsLoading && !versionsError && versions && versions.length > 0 && (
          <div className="bg-white rounded-xl border border-slate-200 overflow-hidden shadow-xs">
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm text-slate-600">
                <thead className="bg-slate-50/75 border-b border-slate-200 text-xs font-semibold text-slate-500 uppercase tracking-wider">
                  <tr>
                    <th className="px-6 py-3.5">版本号 (Tag)</th>
                    <th className="px-6 py-3.5">状态</th>
                    <th className="px-6 py-3.5">运行环境</th>
                    <th className="px-6 py-3.5">HTTP 调用端点</th>
                    <th className="px-6 py-3.5">规格指纹 (Spec Digest)</th>
                    <th className="px-6 py-3.5">创建时间</th>
                    <th className="px-6 py-3.5 text-right">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {versions.map((ver) => (
                    <tr key={ver.id} className="hover:bg-slate-50/80 transition-colors">
                      <td className="px-6 py-4 font-mono font-bold text-slate-900">
                        {ver.version}
                      </td>

                      <td className="px-6 py-4">
                        {ver.is_active ? (
                          <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200">
                            ACTIVE
                          </span>
                        ) : (
                          <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold bg-slate-100 text-slate-500 border border-slate-200">
                            ARCHIVED
                          </span>
                        )}
                      </td>

                      <td className="px-6 py-4 text-xs">
                        {ver.environment ? (
                          <span className="px-2 py-0.5 rounded bg-indigo-50 text-indigo-700 font-medium">
                            {ver.environment}
                          </span>
                        ) : (
                          <span className="text-slate-300">-</span>
                        )}
                      </td>

                      <td className="px-6 py-4 font-mono text-xs text-slate-600 max-w-xs truncate" title={ver.endpoint}>
                        {ver.endpoint}
                      </td>

                      <td className="px-6 py-4 font-mono text-xs text-slate-500">
                        <span title={ver.spec_digest}>
                          {ver.spec_digest.slice(0, 10)}...
                        </span>
                      </td>

                      <td className="px-6 py-4 text-xs text-slate-500">
                        {new Date(ver.created_at).toLocaleString("zh-CN", { hour12: false })}
                      </td>

                      <td className="px-6 py-4 text-right space-x-2">
                        <Link
                          to={`/agents/${agent.id}/versions/${ver.version}`}
                          className="inline-flex items-center gap-0.5 text-xs font-semibold text-indigo-600 hover:text-indigo-800"
                        >
                          <span>查看配置</span>
                          <ChevronRight className="w-3 h-3" />
                        </Link>

                        {ver.is_active && (
                          <button
                            type="button"
                            onClick={() => {
                              if (confirm(`确认归档版本 ${ver.version} 吗？归档后将不能用于新评测。`)) {
                                archiveMutation.mutate(ver.version);
                              }
                            }}
                            disabled={archiveMutation.isPending}
                            className="inline-flex items-center gap-1 text-xs font-semibold text-slate-500 hover:text-rose-600 transition-colors ml-2 cursor-pointer disabled:opacity-50"
                          >
                            <Archive className="w-3 h-3" />
                            <span>归档</span>
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>

      {/* Create Version Dialog */}
      <CreateVersionDialog
        agentId={agent.id}
        isOpen={isCreateOpen}
        onClose={() => setIsCreateOpen(false)}
      />
    </div>
  );
};
