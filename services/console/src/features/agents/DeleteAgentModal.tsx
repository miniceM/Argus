import React, { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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

type AgentSummary = import("../../api/schema").components["schemas"]["AgentResponse"];

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
  const [validatedSummary, setValidatedSummary] = useState<{
    agentId: string;
    requestId: number;
    data: AgentSummary;
    dataUpdatedAt: number;
  } | null>(null);
  const [serverRequiresForce, setServerRequiresForce] = useState(false);
  const summaryValidationRequestId = useRef(0);
  const agentId = agent?.id;

  const summaryQuery = useQuery({
    queryKey: queryKeys.agents.detail(agentId ?? ""),
    queryFn: async ({ signal }) => {
      if (!agentId) throw new Error("缺少 Agent 信息");
      const res = await api.GET("/api/v1/agents", {
        params: { query: { id: agentId } },
        signal,
      });
      if (res.error) throw res.error;
      const data = Array.isArray(res.data) ? res.data[0] : res.data;
      if (!data) throw new Error("未找到 Agent 最新统计");
      return data as AgentSummary;
    },
    enabled: false,
  });

  const verifyLatestSummary = useCallback(async () => {
    if (!agentId) return false;
    const requestId = ++summaryValidationRequestId.current;
    const queryKey = queryKeys.agents.detail(agentId);
    setValidatedSummary(null);

    // A refetch can reuse an in-flight promise when the query has no data.
    // Cancel it first so this validation is always backed by a new GET.
    await queryClient.cancelQueries({ queryKey, exact: true }, { silent: true });
    if (summaryValidationRequestId.current !== requestId) return false;

    const result = await summaryQuery.refetch({ cancelRefetch: true });
    const freshSummary = result.isSuccess && !result.isError ? result.data : undefined;
    const isSuccessful = freshSummary !== undefined;
    if (summaryValidationRequestId.current === requestId) {
      setValidatedSummary(
        freshSummary
          ? {
              agentId,
              requestId,
              data: freshSummary,
              dataUpdatedAt: result.dataUpdatedAt,
            }
          : null
      );
      if (isSuccessful) setServerRequiresForce(false);
    }
    return isSuccessful;
  }, [agentId, queryClient, summaryQuery.refetch]);

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
      setServerRequiresForce(false);
    }
  }, [isOpen, agent?.id]);

  useEffect(() => {
    if (isOpen && summaryQuery.data) {
      setLocalCounts({
        launch_count: summaryQuery.data.launch_count,
        active_launch_count: summaryQuery.data.active_launch_count,
      });
      setRequiresForce((summaryQuery.data.launch_count ?? 0) > 0);
    }
  }, [isOpen, summaryQuery.data]);

  useEffect(() => {
    if (!isOpen || !agentId) {
      summaryValidationRequestId.current += 1;
      setValidatedSummary(null);
      return;
    }

    void verifyLatestSummary();
    return () => {
      summaryValidationRequestId.current += 1;
      void queryClient.cancelQueries(
        { queryKey: queryKeys.agents.detail(agentId), exact: true },
        { silent: true }
      );
    };
  }, [isOpen, agentId, queryClient, verifyLatestSummary]);

  const currentValidatedSummaryState =
    isOpen && validatedSummary?.agentId === agentId ? validatedSummary : null;
  const currentValidatedSummary = currentValidatedSummaryState
    ? summaryQuery.data &&
      (summaryQuery.dataUpdatedAt > currentValidatedSummaryState.dataUpdatedAt ||
        summaryQuery.data !== currentValidatedSummaryState.data)
      ? summaryQuery.data
      : currentValidatedSummaryState.data
    : null;
  const effectiveLaunchCount = currentValidatedSummary?.launch_count ?? localCounts.launch_count ?? summaryQuery.data?.launch_count ?? agent?.launch_count ?? 0;
  const effectiveActiveLaunchCount = currentValidatedSummary?.active_launch_count ?? localCounts.active_launch_count ?? summaryQuery.data?.active_launch_count ?? agent?.active_launch_count ?? 0;
  const hasActiveLaunches = effectiveActiveLaunchCount > 0;
  const summaryReady = Boolean(
    currentValidatedSummary &&
    currentValidatedSummaryState?.requestId === summaryValidationRequestId.current
  );
  const forceRequired = serverRequiresForce || (currentValidatedSummary
    ? (currentValidatedSummary.launch_count ?? 0) > 0
    : requiresForce);

  useEffect(() => {
    if (!isOpen || effectiveActiveLaunchCount <= 0) return;
    const intervalId = window.setInterval(() => {
      void verifyLatestSummary();
    }, 2000);
    return () => window.clearInterval(intervalId);
  }, [isOpen, effectiveActiveLaunchCount, verifyLatestSummary]);

  const mutation = useMutation({
    mutationFn: async () => {
      if (!agent) return;
      setErrorMsg(null);

      if (forceRequired) {
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
        if (agent) {
          queryClient.setQueryData<AgentSummary | undefined>(
            queryKeys.agents.detail(agent.id),
            (current) => current ? {
              ...current,
              launch_count: apiErr.launch_count ?? current.launch_count,
              active_launch_count: apiErr.active_launch_count ?? current.active_launch_count,
            } : current
          );
        }
      }

      // Structured error code branching instead of fragile text matching
      if (code === "AGENT_HAS_LAUNCHES") {
        setRequiresForce(true);
        setServerRequiresForce(true);
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
  const canSubmit =
    summaryReady &&
    !summaryQuery.isError &&
    currentValidatedSummary?.active_launch_count === 0 &&
    (forceRequired ? isNameMatched : true);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const latestSummary = agent
      ? queryClient.getQueryData<AgentSummary>(queryKeys.agents.detail(agent.id))
      : undefined;
    const activeCountAtSubmit =
      latestSummary?.active_launch_count ?? currentValidatedSummary?.active_launch_count;
    const launchCountAtSubmit =
      latestSummary?.launch_count ?? currentValidatedSummary?.launch_count;
    const forceRequiredAtSubmit =
      serverRequiresForce ||
      (launchCountAtSubmit !== undefined ? launchCountAtSubmit > 0 : forceRequired);
    if (
      !canSubmit ||
      !agent ||
      currentValidatedSummaryState?.agentId !== agent.id ||
      currentValidatedSummaryState?.requestId !== summaryValidationRequestId.current ||
      activeCountAtSubmit !== 0 ||
      (forceRequiredAtSubmit && !isNameMatched) ||
      mutation.isPending
    ) return;
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
                {forceRequired ? "高危：强制清理 Agent 及评测记录" : "删除 Agent"}
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
          {!summaryReady && !summaryQuery.isError && (
            <p role="status" className="text-xs text-slate-500">正在核对最新评测状态...</p>
          )}
          {summaryQuery.isError && (
            <div role="alert" className="p-3 text-xs bg-rose-50 border border-rose-200 rounded-lg text-rose-700">
              <p>无法加载最新评测状态：{formatApiError(summaryQuery.error)}</p>
              <button
                type="button"
                onClick={() => { void verifyLatestSummary(); }}
                className="mt-2 underline font-semibold"
              >
                重新加载状态
              </button>
            </div>
          )}
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
                该 Agent 当前有 <strong>{effectiveActiveLaunchCount}</strong> 条尚未结束的评测记录。活跃评测包括待执行、排队中、运行中、取消中等尚未结束状态的任务。请先取消或等待任务结束，再进行删除。
              </p>
            </div>
          )}

          {forceRequired ? (
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
                {mutation.isPending ? "正在删除..." : forceRequired ? "确认强制清理" : "确认删除"}
              </span>
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
