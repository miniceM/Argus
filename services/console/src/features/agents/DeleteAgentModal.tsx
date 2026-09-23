import React, { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, AlertTriangle, Trash2, X } from "lucide-react";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError, getApiErrorCode } from "../../api/errors";

export interface DeleteAgentModalProps {
  isOpen: boolean;
  agent: {
    id: string;
    name: string;
    version_count?: number;
    launch_count?: number;
    active_launch_count?: number;
  } | null;
  onClose: () => void;
  onSuccess?: () => void;
}

export const DeleteAgentModal: React.FC<DeleteAgentModalProps> = ({
  isOpen,
  agent,
  onClose,
  onSuccess,
}) => {
  const queryClient = useQueryClient();
  const [confirmName, setConfirmName] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [requiresForce, setRequiresForce] = useState(false);
  const [localCounts, setLocalCounts] = useState<{
    launch_count?: number;
    active_launch_count?: number;
  }>({});

  useEffect(() => {
    if (isOpen && agent) {
      setConfirmName("");
      setErrorMsg(null);
      setLocalCounts({
        launch_count: agent.launch_count,
        active_launch_count: agent.active_launch_count,
      });
      // Only require force purge if associated launches exist; version alone does not require force
      setRequiresForce((agent.launch_count ?? 0) > 0);
    }
  }, [isOpen, agent]);

  const effectiveLaunchCount = localCounts.launch_count ?? agent?.launch_count ?? 0;
  const effectiveActiveLaunchCount = localCounts.active_launch_count ?? agent?.active_launch_count ?? 0;
  const hasActiveLaunches = effectiveActiveLaunchCount > 0;

  const mutation = useMutation({
    mutationFn: async () => {
      if (!agent) return;
      setErrorMsg(null);

      if (requiresForce) {
        // Dedicated irreversible purge endpoint with server-side exact name verification
        const res = await api.POST("/api/v1/agents/purge", {
          body: {
            agent_id: agent.id,
            confirm_name: confirmName,
          },
        });
        if (res.error) {
          throw res.error;
        }
        return res.data;
      } else {
        // Standard safe delete (without force)
        const res = await api.DELETE("/api/v1/agents", {
          params: {
            query: {
              id: agent.id,
              force: false,
            },
          },
        });
        if (res.error) {
          throw res.error;
        }
        return res.data;
      }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.list() });
      if (agent) {
        queryClient.invalidateQueries({ queryKey: queryKeys.agents.detail(agent.id) });
      }
      onClose();
      if (onSuccess) {
        onSuccess();
      }
    },
    onError: (err: unknown) => {
      const code = getApiErrorCode(err);
      const msg = formatApiError(err);
      setErrorMsg(msg);

      // Invalidate queries so that underlying list/detail is refreshed with latest server data
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.list() });
      if (agent) {
        queryClient.invalidateQueries({ queryKey: queryKeys.agents.detail(agent.id) });
      }

      // Sync latest launch counts from server domain error response
      const apiErr = err as { launch_count?: number; active_launch_count?: number } | undefined;
      if (apiErr && (apiErr.launch_count !== undefined || apiErr.active_launch_count !== undefined)) {
        setLocalCounts((prev) => ({
          launch_count: apiErr.launch_count ?? prev.launch_count,
          active_launch_count: apiErr.active_launch_count ?? prev.active_launch_count,
        }));
      }

      // Structured error code branching instead of fragile text matching
      if (code === "AGENT_HAS_LAUNCHES") {
        setRequiresForce(true);
      }
    },
  });

  // Handle ESC key to close modal (disabled while mutation is pending)
  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !mutation.isPending) {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, onClose, mutation.isPending]);

  if (!isOpen || !agent) return null;

  // Strict exact match without trimming
  const isNameMatched = confirmName === agent.name;
  const canSubmit = !hasActiveLaunches && (requiresForce ? isNameMatched : true);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit || mutation.isPending) return;
    mutation.mutate();
  };

  const handleBackdropClick = () => {
    if (!mutation.isPending) {
      onClose();
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="delete-agent-title"
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/50 backdrop-blur-xs"
      onClick={handleBackdropClick}
    >
      <div
        className="relative w-full max-w-lg bg-white rounded-2xl shadow-2xl border border-slate-200 overflow-hidden animate-in fade-in zoom-in-95 duration-150"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100 bg-rose-50/40">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-rose-100 border border-rose-200 flex items-center justify-center text-rose-600">
              <AlertTriangle className="w-5 h-5" />
            </div>
            <div>
              <h3 id="delete-agent-title" className="text-base font-bold text-slate-900">
                {requiresForce ? "高危：强制清理 Agent 及评测记录" : "删除 Agent"}
              </h3>
              <p className="text-xs text-slate-500 font-mono mt-0.5">ID: {agent.id}</p>
            </div>
          </div>
          <button
            onClick={onClose}
            disabled={mutation.isPending}
            className="p-1.5 text-slate-400 hover:text-slate-600 hover:bg-slate-100 rounded-lg transition-colors cursor-pointer disabled:opacity-50"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Form Body */}
        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          {errorMsg && (
            <div className="p-3 text-xs bg-rose-50 border border-rose-200 rounded-lg text-rose-700 font-medium leading-relaxed">
              {errorMsg}
            </div>
          )}

          {/* Active Launches Blocking Notice */}
          {hasActiveLaunches && (
            <div className="p-3.5 bg-rose-50 border border-rose-200 rounded-xl text-xs text-rose-800 space-y-2 leading-relaxed">
              <p className="font-semibold text-rose-900 flex items-center gap-1.5">
                <AlertCircle className="w-4 h-4 text-rose-600 flex-shrink-0" />
                <span>禁止删除：存在活跃评测任务</span>
              </p>
              <p>
                该 Agent 当前有 <strong>{effectiveActiveLaunchCount}</strong> 个正在执行或排队中的评测任务。为避免未定义副作用，请先取消或等待所有任务完成后再进行删除。
              </p>
            </div>
          )}

          {requiresForce ? (
            <div className="space-y-4">
              <div className="p-3.5 bg-amber-50/90 border border-amber-200/80 rounded-xl text-xs text-amber-800 space-y-2 leading-relaxed">
                <p className="font-semibold text-amber-900 flex items-center gap-1.5">
                  <AlertTriangle className="w-4 h-4 text-amber-600 flex-shrink-0" />
                  <span>注意：该 Agent 包含关联评测记录</span>
                </p>
                <p>
                  该 Agent 存在 <strong>{effectiveLaunchCount}</strong> 条历史评测记录。强制清理将连同本地所有执行历史一并清除，此操作不可撤销。
                </p>
                <div className="pt-1 text-[11px] text-amber-700 border-t border-amber-200/60">
                  🛡️ <strong>安全保障</strong>：仅清除当前 Argus 本地记录，<strong>Langfuse 中的 Dataset / Trace 记录不会被删除</strong>。
                </div>
              </div>

              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1.5">
                  请输入 Agent 全称 <span className="text-rose-600 font-bold select-all">"{agent.name}"</span> 以确认：
                </label>
                <input
                  type="text"
                  autoFocus
                  disabled={hasActiveLaunches || mutation.isPending}
                  value={confirmName}
                  onChange={(e) => setConfirmName(e.target.value)}
                  placeholder={`请输入 ${agent.name}`}
                  className="w-full px-3.5 py-2 text-sm bg-white border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-rose-500/20 focus:border-rose-500 transition-all font-medium disabled:opacity-50 disabled:bg-slate-50"
                />
              </div>
            </div>
          ) : (
            <p className="text-sm text-slate-600 leading-relaxed">
              确认删除 Agent <strong className="text-slate-900">"{agent.name}"</strong>（{agent.id}）吗？此操作将清理该 Agent 及其规格快照（当前无关联评测记录）。
            </p>
          )}

          {/* Footer Actions */}
          <div className="flex items-center justify-end gap-3 pt-3 border-t border-slate-100">
            <button
              type="button"
              onClick={onClose}
              disabled={mutation.isPending}
              className="px-4 py-2 text-xs font-semibold text-slate-600 hover:text-slate-800 hover:bg-slate-100 rounded-lg transition-colors cursor-pointer disabled:opacity-50"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={!canSubmit || mutation.isPending}
              className="inline-flex items-center gap-1.5 px-4 py-2 text-xs font-semibold text-white bg-rose-600 hover:bg-rose-700 rounded-lg shadow-xs transition-colors cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <Trash2 className="w-3.5 h-3.5" />
              <span>
                {mutation.isPending ? "正在删除..." : requiresForce ? "确认强制清理" : "确认删除"}
              </span>
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
