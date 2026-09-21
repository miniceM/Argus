import React, { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { X, Plus, Bot } from "lucide-react";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";

interface RegisterAgentDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess?: (agentId: string) => void;
}

export const RegisterAgentDialog: React.FC<RegisterAgentDialogProps> = ({ isOpen, onClose, onSuccess }) => {
  const queryClient = useQueryClient();
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [owner, setOwner] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: async () => {
      setErrorMsg(null);
      const res = await api.POST("/api/v1/agents", {
        body: {
          id: id.trim(),
          name: name.trim(),
          description: description.trim() || null,
          owner: owner.trim() || null,
        },
      });

      if (res.error) {
        throw res.error;
      }
      return res.data;
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.list() });
      onClose();
      if (data && onSuccess) {
        onSuccess(data.id);
      }
    },
    onError: (err) => {
      setErrorMsg(formatApiError(err));
    },
  });

  if (!isOpen) return null;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!id.trim() || !name.trim()) {
      setErrorMsg("请填写 Agent ID 与名称");
      return;
    }
    mutation.mutate();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 backdrop-blur-xs p-4">
      <div className="bg-white rounded-xl shadow-xl border border-slate-200 w-full max-w-lg overflow-hidden animate-in fade-in zoom-in-95 duration-150">
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-200">
          <div className="flex items-center gap-2 text-slate-800">
            <Bot className="w-5 h-5 text-indigo-600" />
            <h2 className="text-base font-bold">注册新 Agent</h2>
          </div>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-slate-600 rounded-lg p-1 hover:bg-slate-100 transition-colors"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          {errorMsg && (
            <div className="p-3 text-xs bg-rose-50 border border-rose-200 rounded-lg text-rose-700 font-medium">
              {errorMsg}
            </div>
          )}

          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">
              Agent ID <span className="text-rose-500">*</span>
            </label>
            <input
              type="text"
              required
              placeholder="e.g. banking-agent"
              value={id}
              onChange={(e) => setId(e.target.value)}
              className="w-full px-3 py-2 text-sm border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-600 font-mono"
            />
            <p className="text-[11px] text-slate-400 mt-1">全局唯一标识符，建议小写字母加中划线</p>
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">
              显示名称 <span className="text-rose-500">*</span>
            </label>
            <input
              type="text"
              required
              placeholder="e.g. 银行核心业务助手"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full px-3 py-2 text-sm border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-600"
            />
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">负责人 / 所属团队</label>
            <input
              type="text"
              placeholder="e.g. retail-ai-team"
              value={owner}
              onChange={(e) => setOwner(e.target.value)}
              className="w-full px-3 py-2 text-sm border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-600"
            />
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">详细描述</label>
            <textarea
              rows={3}
              placeholder="简要说明该 Agent 的业务职责与评测重点..."
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="w-full px-3 py-2 text-sm border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-600"
            />
          </div>

          <div className="flex items-center justify-end gap-3 pt-3 border-t border-slate-100">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-100 rounded-lg transition-colors cursor-pointer"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={mutation.isPending}
              className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg shadow-xs transition-colors disabled:opacity-50 cursor-pointer"
            >
              <Plus className="w-4 h-4" />
              <span>{mutation.isPending ? "注册中..." : "确认注册"}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
