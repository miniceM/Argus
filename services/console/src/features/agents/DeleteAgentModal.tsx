import React, { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Trash2, X } from "lucide-react";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";

export interface DeleteAgentModalProps {
  isOpen: boolean;
  agent: {
    id: string;
    name: string;
    version_count?: number;
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

  useEffect(() => {
    if (isOpen && agent) {
      setConfirmName("");
      setErrorMsg(null);
      // If the agent already has versions registered, default to force-delete safety flow
      setRequiresForce((agent.version_count ?? 0) > 0);
    }
  }, [isOpen, agent]);

  const mutation = useMutation({
    mutationFn: async () => {
      if (!agent) return;
      setErrorMsg(null);
      const res = await api.DELETE("/api/v1/agents", {
        params: {
          query: {
            id: agent.id,
            force: requiresForce,
          },
        },
      });

      if (res.error) {
        throw res.error;
      }
      return res.data;
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
      const msg = formatApiError(err);
      setErrorMsg(msg);
      // If 409 returned due to associated launches, automatically switch to force confirmation
      if (msg.includes("关联的评测记录") || msg.includes("强制删除")) {
        setRequiresForce(true);
      }
    },
  });

  if (!isOpen || !agent) return null;

  const isNameMatched = confirmName.trim() === agent.name.trim();
  const canSubmit = requiresForce ? isNameMatched : true;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    mutation.mutate();
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/50 backdrop-blur-xs"
      onClick={onClose}
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
              <h3 className="text-base font-bold text-slate-900">
                {requiresForce ? "高危：强制删除 Agent" : "删除 Agent"}
              </h3>
              <p className="text-xs text-slate-500 font-mono mt-0.5">ID: {agent.id}</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 text-slate-400 hover:text-slate-600 hover:bg-slate-100 rounded-lg transition-colors cursor-pointer"
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

          {requiresForce ? (
            <div className="space-y-4">
              <div className="p-3.5 bg-amber-50/90 border border-amber-200/80 rounded-xl text-xs text-amber-800 space-y-2 leading-relaxed">
                <p className="font-semibold text-amber-900 flex items-center gap-1.5">
                  <AlertTriangle className="w-4 h-4 text-amber-600 flex-shrink-0" />
                  <span>注意：该 Agent 包含规格快照或历史评测记录</span>
                </p>
                <p>
                  强制删除将连同该 Agent 在当前 Argus 平台内的<strong>所有执行与评测记录</strong>一并清理，此操作不可撤销。
                </p>
                <div className="pt-1 text-[11px] text-amber-700 border-t border-amber-200/60">
                  🛡️ <strong>安全保障</strong>：仅清除当前 Argus 数据库中的记录，<strong>Langfuse 中的 Dataset / Trace 记录不会被删除</strong>。
                </div>
              </div>

              <div>
                <label className="block text-xs font-semibold text-slate-700 mb-1.5">
                  请输入 Agent 全称 <span className="text-rose-600 font-bold select-all">"{agent.name}"</span> 以确认：
                </label>
                <input
                  type="text"
                  autoFocus
                  value={confirmName}
                  onChange={(e) => setConfirmName(e.target.value)}
                  placeholder={`请输入 ${agent.name}`}
                  className="w-full px-3.5 py-2 text-sm bg-white border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-rose-500/20 focus:border-rose-500 transition-all font-medium"
                />
              </div>
            </div>
          ) : (
            <p className="text-sm text-slate-600 leading-relaxed">
              确认删除 Agent <strong className="text-slate-900">"{agent.name}"</strong>（{agent.id}）吗？此操作将清理该 Agent 及其未引用的版本配置，且不可撤销。
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
                {mutation.isPending ? "正在删除..." : requiresForce ? "确认强制删除" : "确认删除"}
              </span>
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
