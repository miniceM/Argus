import React from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, Clock, Layers, Network } from "lucide-react";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";
import { EmptyState, ErrorState, LoadingState } from "../../components/StateViews";
import { SideDrawer } from "../../components/ui/Overlay";
import { EvaluationResultPanel } from "./EvaluationResultList";
import { QualityRulePanel, type QualityEvaluation } from "./qualityDecision";
import type { EvaluationResult } from "./evaluationResults";

type ExecutionAttempt = import("../../api/schema").components["schemas"]["ExecutionAttemptResponse"];

interface AttemptDrawerProps {
  isOpen: boolean;
  onClose: () => void;
  itemExecutionId: string | null;
  caseId: string | null;
  /** Issue #82: the typed results of this case, rendered above the timeline. */
  evaluationResults?: EvaluationResult[] | null;
  traceUrl?: string | null;
  /** Issue #83: the per-rule reasons behind this case's quality conclusion. */
  qualityEvaluation?: QualityEvaluation | null;
}

export const AttemptDrawer: React.FC<AttemptDrawerProps> = ({
  isOpen,
  onClose,
  itemExecutionId,
  caseId,
  evaluationResults,
  traceUrl,
  qualityEvaluation,
}) => {
  const { data: attempts, isLoading, error } = useQuery<ExecutionAttempt[]>({
    queryKey: queryKeys.launches.attempts(itemExecutionId || ""),
    queryFn: async () => {
      if (!itemExecutionId) return [];
      // Prefer standard /api/v1/execution-attempts for mock & contract backward compatibility
      try {
        const fallback = await api.GET("/api/v1/execution-attempts", {
          params: { query: { item_execution_id: itemExecutionId } },
        });
        if (fallback.data) {
          const list = Array.isArray(fallback.data) ? fallback.data : [fallback.data];
          return list as ExecutionAttempt[];
        }
      } catch {
        // Fallback to path parameter
      }

      const res = await api.GET("/api/v1/experiment-item-executions/{item_execution_id}/attempts", {
        params: { path: { item_execution_id: itemExecutionId } },
      });
      if (res.data) {
        const list = Array.isArray(res.data) ? res.data : [res.data];
        return list as ExecutionAttempt[];
      }
      return [];
    },
    enabled: Boolean(isOpen && itemExecutionId),
  });

  if (!isOpen) return null;

  return (
    <SideDrawer
      open={isOpen}
      onClose={onClose}
      title="用例执行调用历史 (Attempts Timeline)"
      subtitle={`Case ID: ${caseId || itemExecutionId}`}
      icon={<Layers aria-hidden="true" className="w-4 h-4 text-primary" />}
    >
      <div className="p-6 space-y-6">
          {/* Issue #83: why this case concluded PASS / FAIL / UNKNOWN. */}
          <div className="ui-panel p-4" data-testid="quality-decision-panel">
            <QualityRulePanel evaluation={qualityEvaluation} />
          </div>
          <EvaluationResultPanel results={evaluationResults} traceUrl={traceUrl} />
          {isLoading && <LoadingState message="正在加载 Attempt 历史调用记录..." />}
          {error && <ErrorState message={formatApiError(error)} />}

          {!isLoading && !error && attempts && attempts.length === 0 && (
            <EmptyState
              title="暂无记录的调用 Attempt"
              description="该用例尚未产生任何调用记录。若它本应被执行，请检查 Launch 的运行状态与执行策略。"
            />
          )}

          {!isLoading && !error && attempts && attempts.length > 0 && (
            <div className="space-y-4">
              {attempts.map((attempt) => {
                const isSuccess = attempt.http_status && attempt.http_status >= 200 && attempt.http_status < 300;
                return (
                  <div
                    key={attempt.id}
                    className="ui-panel overflow-hidden"
                  >
                    {/* Attempt Header */}
                    <div className="px-4 py-3 bg-canvas/80 border-b border-border flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <span className="w-6 h-6 rounded-full bg-surface-muted text-foreground-secondary text-xs font-bold flex items-center justify-center font-mono">
                          #{attempt.attempt_no}
                        </span>
                        <span className="text-xs font-semibold text-foreground">
                          第 {attempt.attempt_no} 次调用尝试
                        </span>
                        {attempt.worker_id && (
                          <span className="px-2 py-0.5 rounded text-2xs font-mono bg-surface-muted text-foreground-secondary border border-border">
                            {attempt.worker_id}
                          </span>
                        )}
                        {attempt.request_phase && (
                          <span
                            className={`px-2 py-0.5 rounded text-2xs font-mono font-semibold ${
                              attempt.request_phase === "RESPONSE_RECEIVED"
                                ? "bg-pass-subtle text-pass-strong border border-pass-border"
                                : attempt.request_phase === "MAY_HAVE_BEEN_SENT"
                                ? "bg-timeout-subtle text-timeout border border-timeout-border"
                                : "bg-surface-muted text-foreground-secondary border border-border"
                            }`}
                          >
                            {attempt.request_phase}
                          </span>
                        )}
                      </div>

                      <div className="flex items-center gap-3 text-xs">
                        {attempt.http_status ? (
                          <span
                            className={`px-2 py-0.5 rounded font-mono font-semibold ${
                              isSuccess
                                ? "bg-pass-subtle text-pass-strong border border-pass-border"
                                : "bg-fail-subtle text-fail border border-fail-border"
                            }`}
                          >
                            HTTP {attempt.http_status}
                          </span>
                        ) : (
                          <span className="text-muted-foreground font-mono">HTTP -</span>
                        )}

                        <span className="flex items-center gap-1 text-muted-foreground font-mono">
                          <Clock className="w-3.5 h-3.5 text-muted-foreground" />
                          <span>{attempt.latency_ms ?? 0} ms</span>
                        </span>
                      </div>
                    </div>

                    {/* Attempt Details */}
                    <div className="p-4 space-y-3 text-xs">
                      {/* Trace Context */}
                      <div>
                        <span className="text-micro font-medium text-muted-foreground block mb-1 flex items-center gap-1">
                          <Network className="w-3 h-3 text-primary" />
                          跨系统调用跟踪 (W3C Trace Context)
                        </span>
                        <span
                          className={`inline-flex items-center px-2 py-0.5 rounded text-micro font-medium ${
                            attempt.trace_context_received
                              ? "bg-pass-subtle text-pass-strong border border-pass-border"
                              : "bg-surface-muted text-muted-foreground border border-border"
                          }`}
                        >
                          {attempt.trace_context_received ? "已成功传播 traceparent" : "未传播 traceparent"}
                        </span>
                      </div>

                      {/* Ambiguous Outcome Alert */}
                      {attempt.error_type === "AMBIGUOUS_OUTCOME" && (
                        <div className="p-3 bg-timeout-subtle border border-timeout-border rounded-lg text-timeout-strong space-y-1">
                          <div className="flex items-center gap-1.5 font-bold text-xs">
                            <AlertCircle className="w-4 h-4 text-timeout" />
                            <span>非幂等请求结果未决 (AMBIGUOUS_OUTCOME)</span>
                          </div>
                          <p className="text-micro text-timeout-strong leading-relaxed">
                            当前被测 Agent 标记为非幂等，且 Worker 在请求发送后或网络中断期间崩溃。为防资金或业务重复扣款，系统已安全熔断重试。需在详情页点击“重试失败用例”并勾选强制重放确认后方可重新执行。
                          </p>
                        </div>
                      )}

                      {/* Error Banner */}
                      {attempt.error_message && attempt.error_type !== "AMBIGUOUS_OUTCOME" && (
                        <div className="p-3 bg-fail-subtle/80 border border-fail-border rounded-lg text-fail-strong space-y-1">
                          <div className="flex items-center gap-1.5 font-semibold">
                            <AlertCircle className="w-3.5 h-3.5 text-fail" />
                            <span>{attempt.error_type || "执行异常"}</span>
                          </div>
                          <p className="font-mono text-micro whitespace-pre-wrap">{attempt.error_message}</p>
                        </div>
                      )}

                      <div className="pt-2 text-micro text-muted-foreground flex items-center justify-between border-t border-border">
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
    </SideDrawer>
  );
};
