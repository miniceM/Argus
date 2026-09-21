import React from "react";
import { useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  Clock,
  Layers,
  Network,
  X,
} from "lucide-react";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";
import { ErrorState, LoadingState } from "../../components/StateViews";

type ExecutionAttempt = import("../../api/schema").components["schemas"]["ExecutionAttemptResponse"];

interface AttemptDrawerProps {
  isOpen: boolean;
  onClose: () => void;
  itemExecutionId: string | null;
  caseId: string | null;
}

export const AttemptDrawer: React.FC<AttemptDrawerProps> = ({
  isOpen,
  onClose,
  itemExecutionId,
  caseId,
}) => {
  const { data: attempts, isLoading, error } = useQuery<ExecutionAttempt[]>({
    queryKey: queryKeys.launches.attempts(itemExecutionId || ""),
    queryFn: async () => {
      if (!itemExecutionId) return [];
      const res = await api.GET("/api/v1/execution-attempts", {
        params: { query: { item_execution_id: itemExecutionId } },
      });
      if (res.error) throw res.error;
      const list = Array.isArray(res.data) ? res.data : [res.data];
      return list as ExecutionAttempt[];
    },
    enabled: Boolean(isOpen && itemExecutionId),
  });

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 overflow-hidden flex justify-end">
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-slate-900/40 backdrop-blur-xs transition-opacity"
        onClick={onClose}
      />

      {/* Slide-over panel */}
      <div className="relative w-full max-w-xl bg-white shadow-2xl z-10 flex flex-col h-full overflow-hidden">
        {/* Drawer Header */}
        <div className="px-6 py-4 border-b border-slate-200 flex items-center justify-between bg-slate-50/75">
          <div className="flex items-center gap-2">
            <Layers className="w-4 h-4 text-indigo-600" />
            <div>
              <h3 className="text-sm font-bold text-slate-900">
                用例执行调用历史 (Attempts Timeline)
              </h3>
              <p className="text-xs text-slate-500 font-mono">Case ID: {caseId || itemExecutionId}</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 text-slate-400 hover:text-slate-600 rounded-lg hover:bg-slate-100 transition-colors cursor-pointer"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Drawer Body */}
        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          {isLoading && <LoadingState message="正在加载 Attempt 历史调用记录..." />}
          {error && <ErrorState message={formatApiError(error)} />}

          {!isLoading && !error && attempts && attempts.length === 0 && (
            <div className="text-center py-12 text-slate-400 text-xs">
              该用例暂无记录的调用 Attempt。
            </div>
          )}

          {!isLoading && !error && attempts && attempts.length > 0 && (
            <div className="space-y-4">
              {attempts.map((attempt) => {
                const isSuccess = attempt.http_status && attempt.http_status >= 200 && attempt.http_status < 300;
                return (
                  <div
                    key={attempt.id}
                    className="border border-slate-200 rounded-xl overflow-hidden bg-white shadow-xs"
                  >
                    {/* Attempt Header */}
                    <div className="px-4 py-3 bg-slate-50/80 border-b border-slate-200 flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <span className="w-6 h-6 rounded-full bg-slate-200 text-slate-700 text-xs font-bold flex items-center justify-center font-mono">
                          #{attempt.attempt_no}
                        </span>
                        <span className="text-xs font-semibold text-slate-900">
                          第 {attempt.attempt_no} 次调用尝试
                        </span>
                      </div>

                      <div className="flex items-center gap-3 text-xs">
                        {attempt.http_status ? (
                          <span
                            className={`px-2 py-0.5 rounded font-mono font-semibold ${
                              isSuccess
                                ? "bg-emerald-50 text-emerald-700 border border-emerald-200"
                                : "bg-rose-50 text-rose-700 border border-rose-200"
                            }`}
                          >
                            HTTP {attempt.http_status}
                          </span>
                        ) : (
                          <span className="text-slate-400 font-mono">HTTP -</span>
                        )}

                        <span className="flex items-center gap-1 text-slate-500 font-mono">
                          <Clock className="w-3.5 h-3.5 text-slate-400" />
                          <span>{attempt.latency_ms ?? 0} ms</span>
                        </span>
                      </div>
                    </div>

                    {/* Attempt Details */}
                    <div className="p-4 space-y-3 text-xs">
                      {/* Trace Context */}
                      <div>
                        <span className="text-[11px] font-medium text-slate-400 block mb-1 flex items-center gap-1">
                          <Network className="w-3 h-3 text-indigo-500" />
                          跨系统调用跟踪 (W3C Trace Context)
                        </span>
                        <span
                          className={`inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium ${
                            attempt.trace_context_received
                              ? "bg-emerald-50 text-emerald-700 border border-emerald-200"
                              : "bg-slate-100 text-slate-500 border border-slate-200"
                          }`}
                        >
                          {attempt.trace_context_received ? "已成功传播 traceparent" : "未传播 traceparent"}
                        </span>
                      </div>

                      {/* Error Banner */}
                      {attempt.error_message && (
                        <div className="p-3 bg-rose-50/80 border border-rose-200 rounded-lg text-rose-800 space-y-1">
                          <div className="flex items-center gap-1.5 font-semibold">
                            <AlertCircle className="w-3.5 h-3.5 text-rose-600" />
                            <span>{attempt.error_type || "执行异常"}</span>
                          </div>
                          <p className="font-mono text-[11px] whitespace-pre-wrap">{attempt.error_message}</p>
                        </div>
                      )}

                      <div className="pt-2 text-[11px] text-slate-400 flex items-center justify-between border-t border-slate-100">
                        <span>Attempt ID: {attempt.id}</span>
                        <span>{new Date(attempt.started_at).toLocaleString("zh-CN", { hour12: false })}</span>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
