import React, { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Bot, ChevronRight, Layers, Plus, Trash2, User } from "lucide-react";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";
import { RegisterAgentDialog } from "./RegisterAgentDialog";
import { DeleteAgentModal } from "./DeleteAgentModal";
import { EmptyState, ErrorState, LoadingState } from "../../components/StateViews";

export const AgentsList: React.FC = () => {
  const navigate = useNavigate();
  const [isRegisterOpen, setIsRegisterOpen] = useState(false);
  const [agentToDelete, setAgentToDelete] = useState<{
    id: string;
    name: string;
    version_count?: number;
    launch_count?: number;
    active_launch_count?: number;
  } | null>(null);

  const { data: agents, isLoading, error, refetch } = useQuery({
    queryKey: queryKeys.agents.list(),
    queryFn: async () => {
      const res = await api.GET("/api/v1/agents");
      if (res.error) throw res.error;
      return Array.isArray(res.data) ? res.data : [res.data];
    },
  });

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h2 className="text-xl font-bold text-slate-900 tracking-tight">Agent Registry</h2>
          <p className="text-sm text-slate-500 mt-0.5">
            被测业务 Agent 的治理目录与多版本执行契约快照
          </p>
        </div>
        <button
          onClick={() => setIsRegisterOpen(true)}
          className="inline-flex items-center gap-2 px-4 py-2 text-sm font-semibold text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg shadow-xs transition-colors cursor-pointer self-start sm:self-auto"
        >
          <Plus className="w-4 h-4" />
          <span>注册 Agent</span>
        </button>
      </div>

      {/* Content */}
      {isLoading && <LoadingState message="正在拉取 Agent 注册清单..." />}
      {error && <ErrorState message={formatApiError(error)} onRetry={() => refetch()} />}

      {!isLoading && !error && agents && agents.length === 0 && (
        <EmptyState
          title="暂无已注册 Agent"
          description="尚未注册任何业务 Agent。请点击右上角按钮进行首次注册。"
          action={
            <button
              onClick={() => setIsRegisterOpen(true)}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold text-white bg-indigo-600 rounded-lg"
            >
              <Plus className="w-3.5 h-3.5" />
              <span>注册首个 Agent</span>
            </button>
          }
        />
      )}

      {!isLoading && !error && agents && agents.length > 0 && (
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden shadow-xs">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm text-slate-600">
              <thead className="bg-slate-50/75 border-b border-slate-200 text-xs font-semibold text-slate-500 uppercase tracking-wider">
                <tr>
                  <th className="px-6 py-3.5">Agent / ID</th>
                  <th className="px-6 py-3.5">负责人 / 团队</th>
                  <th className="px-6 py-3.5">状态</th>
                  <th className="px-6 py-3.5">最新可用版本</th>
                  <th className="px-6 py-3.5">历史版本数</th>
                  <th className="px-6 py-3.5">评测记录</th>
                  <th className="px-6 py-3.5">更新时间</th>
                  <th className="px-6 py-3.5 text-right">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {agents.map((agent) => (
                  <tr
                    key={agent.id}
                    onClick={() => navigate(`/agents/${agent.id}`)}
                    className="hover:bg-slate-50/80 transition-colors cursor-pointer group"
                  >
                    <td className="px-6 py-4">
                      <div className="flex items-center gap-3">
                        <div className="w-9 h-9 rounded-lg bg-indigo-50 border border-indigo-100 flex items-center justify-center text-indigo-600 font-semibold flex-shrink-0">
                          <Bot className="w-5 h-5" />
                        </div>
                        <div>
                          <div className="font-semibold text-slate-900 group-hover:text-indigo-600 transition-colors">
                            {agent.name}
                          </div>
                          <div className="text-xs text-slate-400 font-mono mt-0.5">{agent.id}</div>
                        </div>
                      </div>
                    </td>

                    <td className="px-6 py-4 text-xs">
                      {agent.owner ? (
                        <span className="inline-flex items-center gap-1 text-slate-700 font-medium">
                          <User className="w-3.5 h-3.5 text-slate-400" />
                          <span>{agent.owner}</span>
                        </span>
                      ) : (
                        <span className="text-slate-300 text-xs">-</span>
                      )}
                    </td>

                    <td className="px-6 py-4">
                      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200">
                        {agent.status.toUpperCase()}
                      </span>
                    </td>

                    <td className="px-6 py-4">
                      {agent.latest_version ? (
                        <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-mono font-medium bg-slate-100 text-slate-800 border border-slate-200">
                          {agent.latest_version}
                        </span>
                      ) : (
                        <span className="text-slate-400 text-xs italic">无激活版本</span>
                      )}
                    </td>

                    <td className="px-6 py-4">
                      <span className="inline-flex items-center gap-1 text-xs text-slate-600 font-medium">
                        <Layers className="w-3.5 h-3.5 text-slate-400" />
                        <span>{agent.version_count ?? 0}</span>
                      </span>
                    </td>

                    <td className="px-6 py-4">
                      <div className="flex items-center gap-1.5 text-xs text-slate-600 font-medium">
                        <span>{agent.launch_count ?? 0}</span>
                        {(agent.active_launch_count ?? 0) > 0 && (
                          <span
                            title="活跃评测包含待执行、排队中、运行中、取消中等尚未结束状态的 Launch。"
                            className="inline-flex items-center px-1.5 py-0.2 rounded-full text-[10px] font-semibold bg-blue-50 text-blue-700 border border-blue-200"
                          >
                            {agent.active_launch_count} 条活跃评测
                          </span>
                        )}
                      </div>
                    </td>

                    <td className="px-6 py-4 text-xs text-slate-500">
                      {new Date(agent.updated_at).toLocaleString("zh-CN", { hour12: false })}
                    </td>

                    <td className="px-6 py-4 text-right">
                      <div className="inline-flex items-center justify-end gap-3">
                        <Link
                          to={`/agents/${agent.id}`}
                          onClick={(e) => e.stopPropagation()}
                          className="inline-flex items-center gap-1 text-xs font-semibold text-indigo-600 hover:text-indigo-800"
                        >
                          <span>管理</span>
                          <ChevronRight className="w-3.5 h-3.5" />
                        </Link>
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            setAgentToDelete({
                              id: agent.id,
                              name: agent.name,
                              version_count: agent.version_count,
                              launch_count: agent.launch_count,
                              active_launch_count: agent.active_launch_count,
                            });
                          }}
                          className="inline-flex items-center gap-1 text-xs font-semibold text-slate-400 hover:text-rose-600 transition-colors cursor-pointer"
                          title="删除 Agent"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                          <span>删除</span>
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Register Agent Dialog */}
      <RegisterAgentDialog
        isOpen={isRegisterOpen}
        onClose={() => setIsRegisterOpen(false)}
        onSuccess={(id) => navigate(`/agents/${id}`)}
      />

      {/* Delete Agent Modal */}
      <DeleteAgentModal
        isOpen={Boolean(agentToDelete)}
        agent={agentToDelete}
        onClose={() => setAgentToDelete(null)}
      />
    </div>
  );
};
