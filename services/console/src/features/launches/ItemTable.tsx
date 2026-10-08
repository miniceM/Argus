import React, { useState } from "react";
import { Clock, Eye, Filter, Layers } from "lucide-react";
import { StatusBadge } from "../../components/StatusBadge";
import { QualityBadge } from "../../components/QualityBadge";
import { Button, Panel } from "../../components/ui/Primitives";
import { AttemptDrawer } from "./AttemptDrawer";
import { EvaluationResultList } from "./EvaluationResultList";
import type { QualityEvaluation } from "./qualityDecision";
import type { EvaluationResult } from "./evaluationResults";
import { frozenFailureRecovery, isFrozenIdentityFailure } from "./frozenIdentity";
import { EvaluationRecoveryBadge, recoveryExplanation } from "./evaluationRecovery";

type ItemExecution = import("../../api/schema").components["schemas"]["ExperimentItemExecutionResponse"];

interface ItemTableProps {
  items: ItemExecution[];
}

/**
 * Issue #83 — a one-line reason for the row: which rules decided it, and
 * whether the cause was a real violation or missing evidence.
 */
const ruleSummary = (evaluation: QualityEvaluation): string => {
  const rules = evaluation.rules ?? [];
  if (rules.length === 0) return "无逐条判定记录";
  const conclusion = (evaluation.conclusion || "unknown").toLowerCase();
  const named = (rule: { evaluator_id: string; required?: boolean }) =>
    `${rule.evaluator_id}${rule.required === false ? "（可选）" : ""}`;
  if (conclusion === "unknown") {
    const missing = rules.filter((r) => (r.conclusion || "").toLowerCase() === "unknown");
    return `证据不足：${missing.map(named).join("、") || "必要规则"}`;
  }
  const violated = rules.filter((r) => (r.conclusion || "").toLowerCase() === "fail");
  return `违反规则：${violated.map(named).join("、")}`;
};

export const ItemTable: React.FC<ItemTableProps> = ({ items }) => {
  const [selectedItem, setSelectedItem] = useState<{
    id: string;
    caseId: string;
    evaluationResults?: EvaluationResult[] | null;
    traceUrl?: string | null;
    qualityEvaluation?: QualityEvaluation | null;
  } | null>(null);
  const [filterQuality, setFilterQuality] = useState<string>("ALL");

  const filteredItems = items.filter((item) => {
    const q = item.quality_conclusion?.toLowerCase();
    const st = item.execution_status?.toLowerCase();
    if (filterQuality === "PASS") return q === "pass";
    if (filterQuality === "FAIL") return q === "fail";
    if (filterQuality === "UNKNOWN") return q === "unknown";
    if (filterQuality === "FAILED") return st === "failed" || st === "timed_out";
    if (filterQuality === "RETRY_WAIT") return st === "retry_wait";
    if (filterQuality === "CANCELLED") return st === "cancelled";
    return true;
  });

  const retryWaitCount = items.filter((i) => i.execution_status?.toLowerCase() === "retry_wait").length;
  const failedCount = items.filter((i) => ["failed", "timed_out"].includes(i.execution_status?.toLowerCase() || "")).length;
  const cancelledCount = items.filter((i) => i.execution_status?.toLowerCase() === "cancelled").length;

  return (
    <div className="space-y-4">
      {/* Table Sub-header & Filter */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Layers className="w-4 h-4 text-primary" />
          <h3 className="text-sm font-bold text-foreground">
            评测用例明细 (Dataset Items & Evaluations)
          </h3>
          <span className="text-xs text-muted-foreground">
            共 {items.length} 个用例
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Filter aria-hidden="true" className="w-3.5 h-3.5 text-muted-foreground" />
          <Button
            type="button"
            variant={filterQuality === "ALL" ? "primary" : "secondary"}
            aria-pressed={filterQuality === "ALL"}
            onClick={() => setFilterQuality("ALL")}
            className="min-h-7 px-2.5 py-1 text-xs"
          >
            全部 ({items.length})
          </Button>
          <Button
            type="button"
            variant={filterQuality === "PASS" ? "primary" : "secondary"}
            aria-pressed={filterQuality === "PASS"}
            onClick={() => setFilterQuality("PASS")}
            className="min-h-7 px-2.5 py-1 text-xs"
          >
            质量通过 ({items.filter((i) => i.quality_conclusion?.toLowerCase() === "pass").length})
          </Button>
          <Button
            type="button"
            variant={filterQuality === "FAIL" ? "primary" : "secondary"}
            aria-pressed={filterQuality === "FAIL"}
            onClick={() => setFilterQuality("FAIL")}
            className="min-h-7 px-2.5 py-1 text-xs"
          >
            未通过 ({items.filter((i) => i.quality_conclusion?.toLowerCase() === "fail").length})
          </Button>
          {/* Issue #83: 证据不足 is its own bucket. Hiding it inside 未通过 would
              turn "we could not tell" into "the agent failed the requirement". */}
          <Button
            type="button"
            variant={filterQuality === "UNKNOWN" ? "primary" : "secondary"}
            aria-pressed={filterQuality === "UNKNOWN"}
            onClick={() => setFilterQuality("UNKNOWN")}
            className="min-h-7 px-2.5 py-1 text-xs"
          >
            证据不足 ({items.filter((i) => (i.quality_conclusion || "unknown").toLowerCase() === "unknown").length})
          </Button>
          {failedCount > 0 && (
            <Button
              type="button"
              variant={filterQuality === "FAILED" ? "primary" : "secondary"}
              aria-pressed={filterQuality === "FAILED"}
              onClick={() => setFilterQuality("FAILED")}
              className="min-h-7 px-2.5 py-1 text-xs"
            >
              失败/超时 ({failedCount})
            </Button>
          )}
          {retryWaitCount > 0 && (
            <Button
              type="button"
              variant={filterQuality === "RETRY_WAIT" ? "primary" : "secondary"}
              aria-pressed={filterQuality === "RETRY_WAIT"}
              onClick={() => setFilterQuality("RETRY_WAIT")}
              className="min-h-7 px-2.5 py-1 text-xs"
            >
              等待重试 ({retryWaitCount})
            </Button>
          )}
          {cancelledCount > 0 && (
            <Button
              type="button"
              variant={filterQuality === "CANCELLED" ? "primary" : "secondary"}
              aria-pressed={filterQuality === "CANCELLED"}
              onClick={() => setFilterQuality("CANCELLED")}
              className="min-h-7 px-2.5 py-1 text-xs"
            >
              已取消 ({cancelledCount})
            </Button>
          )}
        </div>
      </div>

      {/* Table */}
      <Panel className="ui-table-shell">
        <div className="overflow-x-auto">
          <table className="ui-table min-w-table-xl text-sm text-foreground-secondary">
            <thead className="bg-canvas/75 border-b border-border text-xs font-semibold text-muted-foreground uppercase tracking-wider">
              <tr>
                <th className="px-5 py-3.5">用例标识 (Dataset Item ID)</th>
                <th className="px-5 py-3.5">执行状态 (Execution)</th>
                <th className="px-5 py-3.5">质量门禁 (Quality)</th>
                <th className="px-5 py-3.5">评测恢复 (Evaluation Recovery)</th>
                <th className="px-5 py-3.5">评测结果 (Evaluation Results)</th>
                <th className="px-5 py-3.5">最终 HTTP</th>
                <th className="px-5 py-3.5">最终耗时</th>
                <th className="px-5 py-3.5">尝试次数 (Attempts)</th>
                <th className="px-5 py-3.5 text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {filteredItems.map((item) => {
                const errorText = item.execution_error || item.eval_error;

                return (
                  <tr
                    key={item.id}
                    className="hover:bg-surface-muted/80 transition-colors"
                  >
                    <td className="px-5 py-3.5 font-mono text-xs font-bold text-foreground">
                      <div className="flex items-center gap-1.5">
                        <span>{item.dataset_item_id}</span>
                        {item.dispatch_generation && item.dispatch_generation > 1 && (
                          <span className="px-1.5 py-0.2 rounded text-2xs font-semibold bg-primary-subtle text-primary-strong border border-primary-border">
                            gen #{item.dispatch_generation}
                          </span>
                        )}
                      </div>
                      {errorText && (
                        <p className="text-micro text-fail font-normal truncate max-w-xs mt-0.5" title={errorText}>
                          {errorText}
                        </p>
                      )}
                      {/* Issue #81: a frozen-identity failure names the stable error
                          code and the recovery path, instead of only a raw string. */}
                      {isFrozenIdentityFailure(errorText) && (
                        <p
                          className="text-micro text-timeout font-normal max-w-sm mt-0.5"
                          data-testid={`frozen-recovery-${item.dataset_item_id}`}
                        >
                          {frozenFailureRecovery(errorText)}
                        </p>
                      )}
                    </td>

                    <td className="px-5 py-3.5">
                      <StatusBadge status={item.execution_status} />
                    </td>

                    <td className="px-5 py-3.5">
                      <QualityBadge quality={item.quality_conclusion} />
                      {/* Issue #83: name the cause in the row itself, so a
                          证据不足 case is never read as a plain 不通过. */}
                      {item.quality_evaluation &&
                        (item.quality_conclusion || "unknown").toLowerCase() !== "pass" && (
                          <p
                            className="text-micro text-muted-foreground mt-0.5 max-w-56"
                            data-testid={`quality-summary-${item.dataset_item_id}`}
                          >
                            {ruleSummary(item.quality_evaluation as QualityEvaluation)}
                          </p>
                        )}
                    </td>

                    {/* Issue #84: evaluation recovery is shown separately from
                        execution and quality, and always says it reuses the
                        original Agent output. */}
                    <td className="px-5 py-3.5">
                      <EvaluationRecoveryBadge status={item.evaluation_status} />
                      {recoveryExplanation(item) && (
                        <p
                          className="text-micro text-muted-foreground mt-0.5 max-w-56"
                          data-testid={`evaluation-recovery-${item.dataset_item_id}`}
                        >
                          {recoveryExplanation(item)}
                        </p>
                      )}
                    </td>

                    <td className="px-5 py-3.5">
                      {item.evaluation_results && item.evaluation_results.length > 0 ? (
                        <EvaluationResultList results={item.evaluation_results} />
                      ) : item.scores && Object.keys(item.scores).length > 0 ? (
                        <EvaluationResultList
                          results={Object.entries(item.scores).map(([id, value]) => ({
                            evaluator_id: id,
                            result_type: "numeric",
                            status: "succeeded",
                            value,
                          }))}
                        />
                      ) : (
                        <span className="text-xs text-muted-foreground font-mono">—</span>
                      )}
                    </td>

                    <td className="px-5 py-3.5 font-mono text-xs">
                      {item.final_attempt_http_status ? (
                        <span
                          className={`font-semibold ${
                            item.final_attempt_http_status >= 200 &&
                            item.final_attempt_http_status < 300
                              ? "text-pass"
                              : "text-fail"
                          }`}
                        >
                          {item.final_attempt_http_status}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">-</span>
                      )}
                    </td>

                    <td className="px-5 py-3.5 font-mono text-xs text-muted-foreground">
                      {item.final_attempt_latency_ms !== null &&
                      item.final_attempt_latency_ms !== undefined ? (
                        <span className="flex items-center gap-1">
                          <Clock className="w-3.5 h-3.5 text-muted-foreground" />
                          <span>{item.final_attempt_latency_ms} ms</span>
                        </span>
                      ) : (
                        <span className="text-muted-foreground">-</span>
                      )}
                    </td>

                    <td className="px-5 py-3.5">
                      <Button
                        type="button"
                        variant="secondary"
                        onClick={() =>
                          setSelectedItem({
                          id: item.id,
                          caseId: item.dataset_item_id,
                          evaluationResults:
                            (item.evaluation_results as EvaluationResult[] | undefined) ?? null,
                          traceUrl: item.langfuse_trace_url ?? null,
                          qualityEvaluation: (item.quality_evaluation as QualityEvaluation | undefined) ?? null,
                        })
                        }
                        className="min-h-7 px-2.5 py-1 text-xs font-mono"
                        title="查看 Attempt 调用历史"
                      >
                        <span>{item.attempt_count} 次尝试</span>
                        <Eye aria-hidden="true" className="h-3.5 w-3.5" />
                      </Button>
                    </td>

                    <td className="px-5 py-3.5 text-right">
                      <Button
                        type="button"
                        variant="quiet"
                        onClick={() =>
                          setSelectedItem({
                          id: item.id,
                          caseId: item.dataset_item_id,
                          evaluationResults:
                            (item.evaluation_results as EvaluationResult[] | undefined) ?? null,
                          traceUrl: item.langfuse_trace_url ?? null,
                          qualityEvaluation: (item.quality_evaluation as QualityEvaluation | undefined) ?? null,
                        })
                        }
                        className="min-h-7 px-2 text-xs"
                      >
                        明细
                      </Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Panel>

      {/* Attempt Drawer Modal */}
      <AttemptDrawer
        isOpen={Boolean(selectedItem)}
        onClose={() => setSelectedItem(null)}
        itemExecutionId={selectedItem?.id || null}
        caseId={selectedItem?.caseId || null}
        evaluationResults={selectedItem?.evaluationResults}
        traceUrl={selectedItem?.traceUrl}
        qualityEvaluation={selectedItem?.qualityEvaluation}
      />
    </div>
  );
};
