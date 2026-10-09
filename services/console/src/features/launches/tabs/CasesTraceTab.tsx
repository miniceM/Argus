import React, { useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clock,
  ExternalLink,
  Eye,
  Filter,
  Layers,
  RefreshCw,
  XCircle,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../../api/client";
import { queryKeys } from "../../../api/query-keys";
import { formatApiError } from "../../../api/errors";
import { Button } from "../../../components/ui/Primitives";
import { JsonViewer } from "../../../components/JsonViewer";
import { AttemptDrawer } from "../AttemptDrawer";
import { EvaluationResultList } from "../EvaluationResultList";
import { frozenFailureRecovery, isFrozenIdentityFailure } from "../frozenIdentity";
import { EvaluationRecoveryBadge, recoveryExplanation } from "../evaluationRecovery";
import type { QualityEvaluation } from "../qualityDecision";
import type { EvaluationResult } from "../evaluationResults";

type ItemExecution = import("../../../api/schema").components["schemas"]["ExperimentItemExecutionResponse"];
type ComparisonCaseOutput = import("../../../api/schema").components["schemas"]["ComparisonCaseOutputResponse"];

interface CasesTraceTabProps {
  launchId: string;
  snapshotId: string | null;
  items: ItemExecution[];
  initialFilter?: string;
  manifestDataset?: any;
  snapshotCounts?: {
    pass?: number | null;
    fail?: number | null;
    unknown?: number | null;
    total?: number | null;
  } | null;
  currentFilter?: string;
  onFilterChange?: (filter: string) => void;
  expandedCaseId?: string | null;
  onExpandedCaseChange?: (caseId: string | null) => void;
}

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

export const CasesTraceTab: React.FC<CasesTraceTabProps> = ({
  launchId,
  snapshotId,
  items,
  initialFilter = "ALL",
  manifestDataset,
  snapshotCounts,
  currentFilter,
  onFilterChange,
  expandedCaseId,
  onExpandedCaseChange,
}) => {
  const [internalFilter, setInternalFilter] = useState<string>(initialFilter);
  const filterQuality = currentFilter ?? internalFilter;
  const setFilterQuality = (filter: string) => {
    setInternalFilter(filter);
    onFilterChange?.(filter);
  };
  const [internalExpandedCaseId, setInternalExpandedCaseId] = useState<string | null>(null);
  const activeExpandedCaseId = expandedCaseId !== undefined ? expandedCaseId : internalExpandedCaseId;

  const [selectedAttemptItem, setSelectedAttemptItem] = useState<{
    id: string;
    caseId: string;
    evaluationResults?: EvaluationResult[] | null;
    traceUrl?: string | null;
    qualityEvaluation?: QualityEvaluation | null;
  } | null>(null);

  // Filter items
  const filteredItems = items.filter((item) => {
    const q = (item.quality_conclusion || "unknown").toLowerCase();
    const st = (item.execution_status || "").toLowerCase();
    if (filterQuality === "PASS") return q === "pass";
    if (filterQuality === "FAIL") return q === "fail";
    if (filterQuality === "UNKNOWN") return q === "unknown";
    if (filterQuality === "FAILED") return st === "failed" || st === "timed_out";
    if (filterQuality === "RETRY_WAIT") return st === "retry_wait";
    if (filterQuality === "CANCELLED") return st === "cancelled";
    return true;
  });

  const passCount = snapshotCounts?.pass != null
    ? snapshotCounts.pass
    : items.filter((i) => i.quality_conclusion?.toLowerCase() === "pass").length;
  const failCount = snapshotCounts?.fail != null
    ? snapshotCounts.fail
    : items.filter((i) => i.quality_conclusion?.toLowerCase() === "fail").length;
  const unknownCount = snapshotCounts?.unknown != null
    ? snapshotCounts.unknown
    : items.filter((i) => (i.quality_conclusion || "unknown").toLowerCase() === "unknown").length;
  const totalCount = snapshotCounts?.total != null
    ? snapshotCounts.total
    : items.length;
  const failedCount = items.filter((i) => ["failed", "timed_out"].includes((i.execution_status || "").toLowerCase())).length;
  const retryWaitCount = items.filter((i) => (i.execution_status || "").toLowerCase() === "retry_wait").length;
  const cancelledCount = items.filter((i) => (i.execution_status || "").toLowerCase() === "cancelled").length;

  // Toggle case expansion
  const toggleExpand = (caseId: string) => {
    const next = activeExpandedCaseId === caseId ? null : caseId;
    setInternalExpandedCaseId(next);
    onExpandedCaseChange?.(next);
  };

  return (
    <div className="space-y-4">
      {/* 筛选条与状态总览 */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 bg-surface border border-border rounded-xl p-4 shadow-xs">
        <div className="flex flex-wrap items-center gap-2">
          <Filter aria-hidden="true" className="w-3.5 h-3.5 text-muted-foreground" />
          <Button
            type="button"
            variant={filterQuality === "ALL" ? "primary" : "secondary"}
            aria-pressed={filterQuality === "ALL"}
            onClick={() => setFilterQuality("ALL")}
            className="min-h-7 px-3 py-1 text-xs"
          >
            全部用例 ({totalCount})
          </Button>
          <Button
            type="button"
            variant={filterQuality === "PASS" ? "primary" : "secondary"}
            aria-pressed={filterQuality === "PASS"}
            onClick={() => setFilterQuality("PASS")}
            className="min-h-7 px-3 py-1 text-xs"
          >
            已通过 ({passCount})
          </Button>
          <Button
            type="button"
            variant={filterQuality === "FAIL" ? "primary" : "secondary"}
            aria-pressed={filterQuality === "FAIL"}
            onClick={() => setFilterQuality("FAIL")}
            className="min-h-7 px-3 py-1 text-xs"
          >
            未通过 ({failCount})
          </Button>
          <Button
            type="button"
            variant={filterQuality === "UNKNOWN" ? "primary" : "secondary"}
            aria-pressed={filterQuality === "UNKNOWN"}
            onClick={() => setFilterQuality("UNKNOWN")}
            className="min-h-7 px-3 py-1 text-xs"
          >
            证据不足 ({unknownCount})
          </Button>
          {failedCount > 0 && (
            <Button
              type="button"
              variant={filterQuality === "FAILED" ? "primary" : "secondary"}
              aria-pressed={filterQuality === "FAILED"}
              onClick={() => setFilterQuality("FAILED")}
              className="min-h-7 px-3 py-1 text-xs"
            >
              执行失败/超时 ({failedCount})
            </Button>
          )}
          {retryWaitCount > 0 && (
            <Button
              type="button"
              variant={filterQuality === "RETRY_WAIT" ? "primary" : "secondary"}
              aria-pressed={filterQuality === "RETRY_WAIT"}
              onClick={() => setFilterQuality("RETRY_WAIT")}
              className="min-h-7 px-3 py-1 text-xs"
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
              className="min-h-7 px-3 py-1 text-xs"
            >
              已取消 ({cancelledCount})
            </Button>
          )}
        </div>
        <span className="text-xs text-muted-foreground">
          点击用例卡片可展开查看输入、输出及评估扣分详情
        </span>
      </div>

      {/* 用例列表卡片容器 */}
      <div className="bg-surface border border-border rounded-xl overflow-hidden shadow-xs divide-y divide-border text-xs">
        {filteredItems.map((item) => {
          const isExpanded = activeExpandedCaseId === item.dataset_item_id;
          const conclusion = (item.quality_conclusion || "unknown").toLowerCase();
          const isPass = conclusion === "pass";
          const isFail = conclusion === "fail";
          const errorText = item.execution_error || item.eval_error;

          return (
            <div
              key={item.dataset_item_id || item.id}
              className="p-4 hover:bg-surface-subtle transition-colors"
            >
              {/* 卡片头部行 */}
              <div
                className="flex items-center justify-between gap-3 cursor-pointer"
                onClick={() => toggleExpand(item.dataset_item_id)}
                data-testid={`case-row-expand-${item.dataset_item_id}`}
              >
                <div className="flex items-center gap-3 min-w-0">
                  {/* 状态图标 */}
                  <span
                    className={`w-6 h-6 rounded-full flex items-center justify-center font-bold shrink-0 ${
                      isPass
                        ? "bg-pass-subtle text-pass"
                        : isFail
                        ? "bg-fail-subtle text-fail"
                        : "bg-surface-muted text-muted-foreground"
                    }`}
                  >
                    {isPass ? (
                      <CheckCircle2 className="w-4 h-4" />
                    ) : isFail ? (
                      <XCircle className="w-4 h-4" />
                    ) : (
                      <AlertCircle className="w-4 h-4 text-timeout" />
                    )}
                  </span>

                  {/* 用例标题与 ID */}
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-semibold text-foreground font-mono">
                        {item.dataset_item_id}
                      </span>
                      {item.dispatch_generation && item.dispatch_generation > 1 && (
                        <span className="px-1.5 py-0.2 rounded text-2xs font-semibold bg-primary-subtle text-primary border border-primary-border font-mono">
                          gen #{item.dispatch_generation}
                        </span>
                      )}
                      <EvaluationRecoveryBadge status={item.evaluation_status} />
                    </div>
                    {item.quality_evaluation && conclusion !== "pass" && (
                      <p
                        className="text-micro text-muted-foreground truncate max-w-md mt-0.5"
                        data-testid={`quality-summary-${item.dataset_item_id}`}
                      >
                        {ruleSummary(item.quality_evaluation as QualityEvaluation)}
                      </p>
                    )}
                    {errorText && (
                      <p className="text-micro text-fail truncate max-w-md mt-0.5" title={errorText}>
                        {errorText}
                      </p>
                    )}
                    {isFrozenIdentityFailure(errorText) && (
                      <p
                        className="text-micro text-warning font-normal max-w-sm mt-0.5"
                        data-testid={`frozen-recovery-${item.dataset_item_id}`}
                      >
                        {frozenFailureRecovery(errorText)}
                      </p>
                    )}
                    {recoveryExplanation(item) && (
                      <p
                        className="text-micro text-warning font-normal max-w-sm mt-0.5"
                        data-testid={`evaluation-recovery-${item.dataset_item_id}`}
                      >
                        {recoveryExplanation(item)}
                      </p>
                    )}
                    <div className="flex flex-wrap items-center gap-1.5 mt-1.5">
                      {item.evaluation_results && item.evaluation_results.length > 0 ? (
                        <EvaluationResultList results={item.evaluation_results as EvaluationResult[]} />
                      ) : item.scores && Object.keys(item.scores).length > 0 ? (
                        <EvaluationResultList
                          results={Object.entries(item.scores).map(([id, value]) => ({
                            evaluator_id: id,
                            result_type: "numeric",
                            status: "succeeded",
                            value,
                          }))}
                        />
                      ) : null}
                    </div>
                  </div>
                </div>

                {/* 右侧指标与操作链接 */}
                <div className="flex items-center gap-3 shrink-0">
                  {(() => {
                    const latency = (item as any).latency_ms != null ? (item as any).latency_ms : item.final_attempt_latency_ms;
                    return latency != null ? (
                      <span className="text-muted-foreground font-mono flex items-center gap-1" data-testid="case-latency">
                        <Clock className="w-3 h-3 text-muted-foreground" />
                        <span>{latency}ms</span>
                      </span>
                    ) : null;
                  })()}
                  {item.final_attempt_http_status != null && (
                    <span
                      className={`font-mono font-semibold px-1.5 py-0.5 rounded text-micro ${
                        item.final_attempt_http_status >= 200 && item.final_attempt_http_status < 300
                          ? "bg-pass-subtle text-pass"
                          : "bg-fail-subtle text-fail"
                      }`}
                    >
                      HTTP {item.final_attempt_http_status}
                    </span>
                  )}

                  {/* Trace 链接 (N02: 优先读取冻结 trace_url) */}
                  {(() => {
                    const trace = (item as any).trace_url || item.langfuse_trace_url;
                    return trace ? (
                      <a
                        href={trace}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-primary hover:underline inline-flex items-center gap-1 font-medium"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <span>Trace</span>
                        <ExternalLink className="w-3 h-3" />
                      </a>
                    ) : (
                      <span className="text-muted-foreground text-micro">无 Trace</span>
                    );
                  })()}

                  {(() => {
                    const attempts = (item as any).cost_evidence?.attempt_count != null
                      ? (item as any).cost_evidence.attempt_count
                      : item.attempt_count;
                    const canOpenAttempts = Boolean(item.id);
                    return (
                      <Button
                        type="button"
                        variant="secondary"
                        disabled={!canOpenAttempts}
                        onClick={(e) => {
                          e.stopPropagation();
                          if (!item.id) return;
                          setSelectedAttemptItem({
                            id: item.id,
                            caseId: item.dataset_item_id,
                            evaluationResults: (item.evaluation_results as EvaluationResult[] | undefined) ?? null,
                            traceUrl: (item as any).trace_url || item.langfuse_trace_url || null,
                            qualityEvaluation: (item.quality_evaluation as QualityEvaluation | undefined) ?? null,
                          });
                        }}
                        className="min-h-7 px-2.5 py-1 text-xs font-mono disabled:opacity-50 disabled:cursor-not-allowed"
                        title={canOpenAttempts ? "查看 Attempt 调用历史" : "历史快照无实时 Attempt 执行记录"}
                      >
                        <span>{attempts != null ? `${attempts} 次尝试` : "—"}</span>
                        <Eye aria-hidden="true" className="h-3.5 w-3.5 ml-1" />
                      </Button>
                    );
                  })()}

                  <Button
                    type="button"
                    variant="secondary"
                    onClick={(e) => {
                      e.stopPropagation();
                      setSelectedAttemptItem({
                        id: item.id,
                        caseId: item.dataset_item_id,
                        evaluationResults: (item.evaluation_results as EvaluationResult[] | undefined) ?? null,
                        traceUrl: item.langfuse_trace_url ?? null,
                        qualityEvaluation: (item.quality_evaluation as QualityEvaluation | undefined) ?? null,
                      });
                    }}
                    className="min-h-7 px-2.5 py-1 text-xs"
                  >
                    明细
                  </Button>

                  <button
                    type="button"
                    className="text-muted-foreground hover:text-foreground p-1 rounded-sm focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
                    aria-label={isExpanded ? "收起" : "展开"}
                  >
                    {isExpanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                  </button>
                </div>
              </div>

              {/* 行内展开详情面板 (Accordion) */}
              {isExpanded && (
                <CaseDetailPanel
                  launchId={launchId}
                  snapshotId={snapshotId}
                  item={item}
                  manifestDataset={manifestDataset}
                  onOpenAttemptDrawer={() =>
                    setSelectedAttemptItem({
                      id: item.id,
                      caseId: item.dataset_item_id,
                      evaluationResults: (item.evaluation_results as EvaluationResult[] | undefined) ?? null,
                      traceUrl: item.langfuse_trace_url ?? null,
                      qualityEvaluation: (item.quality_evaluation as QualityEvaluation | undefined) ?? null,
                    })
                  }
                />
              )}
            </div>
          );
        })}

        {filteredItems.length === 0 && (
          <div className="p-8 text-center text-muted-foreground">
            当前筛选条件下没有用例记录。
          </div>
        )}
      </div>

      {/* Attempt 历史抽屉 */}
      <AttemptDrawer
        isOpen={Boolean(selectedAttemptItem)}
        onClose={() => setSelectedAttemptItem(null)}
        itemExecutionId={selectedAttemptItem?.id || null}
        caseId={selectedAttemptItem?.caseId || null}
        evaluationResults={selectedAttemptItem?.evaluationResults}
        traceUrl={selectedAttemptItem?.traceUrl}
        qualityEvaluation={selectedAttemptItem?.qualityEvaluation}
      />
    </div>
  );
};

/**
 * 懒加载获取真实 Case 输出与评估详情
 */
const CaseDetailPanel: React.FC<{
  launchId: string;
  snapshotId: string | null;
  item: ItemExecution;
  manifestDataset?: any;
  onOpenAttemptDrawer: () => void;
}> = ({ launchId, snapshotId, item, manifestDataset, onOpenAttemptDrawer }) => {
  // Lazy query comparison case for output
  const caseOutputQuery = useQuery({
    queryKey: queryKeys.launches.case(launchId, snapshotId ?? "live", item.dataset_item_id),
    queryFn: async () => {
      if (!snapshotId) return null;
      const res = await api.GET("/api/v1/experiment-launches/{launch_id}/comparison/case", {
        params: {
          path: { launch_id: launchId },
          query: { snapshot_id: snapshotId, dataset_item_id: item.dataset_item_id },
        },
      });
      if (res.error) throw res.error;
      return res.data as ComparisonCaseOutput | null;
    },
    enabled: Boolean(launchId && snapshotId),
  });

  // Extract frozen input if available in manifest
  const datasetItems = manifestDataset?.items || [];
  const datasetItem = datasetItems.find((d: any) => d.id === item.dataset_item_id);
  const frozenInput = datasetItem?.input;

  const baselineOutput = caseOutputQuery.data?.baseline;
  const candidateOutput = caseOutputQuery.data?.candidate;
  const outputStatus = candidateOutput?.output_status;
  const baselineOutputStatus = baselineOutput?.output_status;

  return (
    <div className="mt-4 pt-4 border-t border-border space-y-4 text-muted-foreground">
      {/* 冻结输入面板 */}
      <div className="p-3 bg-surface-subtle rounded-lg border border-border space-y-2">
        <div className="flex items-center justify-between">
          <span className="font-semibold text-foreground">用户提问 / 冻结输入 (Input)</span>
          <span className="text-micro text-muted-foreground font-mono">
            {frozenInput ? "已冻结" : "未在快照中嵌入输入"}
          </span>
        </div>
        {frozenInput ? (
          <div className="text-foreground">
            {typeof frozenInput === "string" ? (
              <p className="leading-relaxed">{frozenInput}</p>
            ) : (
              <JsonViewer data={frozenInput} title="Input JSON" />
            )}
          </div>
        ) : (
          <p className="text-micro text-muted-foreground italic">
            原始输入存储在测试数据集中（Dataset: {item.dataset_item_id}）。
          </p>
        )}
      </div>

      {/* Baseline 与 Candidate 双侧输出对照网格 */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {/* 基准版本输出面板 (Baseline Output) */}
        <div className="p-3 bg-surface-subtle rounded-lg border border-border space-y-2">
          <div className="flex items-center justify-between">
            <span className="font-semibold text-foreground">基准版本输出 (Baseline Output)</span>
            {baselineOutputStatus && (
              <span className="px-1.5 py-0.5 rounded text-micro font-mono bg-surface border border-border">
                {baselineOutputStatus}
              </span>
            )}
          </div>

          {caseOutputQuery.isLoading && (
            <div className="flex items-center gap-2 text-micro text-muted-foreground py-2">
              <RefreshCw className="w-3.5 h-3.5 animate-spin text-primary" />
              <span>正在按快照引用懒加载基准输出...</span>
            </div>
          )}

          {baselineOutput && (
            <div className="space-y-2">
              {baselineOutput.reason && baselineOutputStatus !== "FETCH_FAILED" && (
                <p className="text-micro text-muted-foreground">{baselineOutput.reason}</p>
              )}
              {baselineOutputStatus === "AVAILABLE" && baselineOutput.output != null ? (
                <div className="text-foreground">
                  {typeof baselineOutput.output === "string" ? (
                    <p className="leading-relaxed font-mono whitespace-pre-wrap text-xs">
                      {baselineOutput.output}
                    </p>
                  ) : (
                    <JsonViewer data={baselineOutput.output} title="Baseline Output JSON" />
                  )}
                </div>
              ) : baselineOutputStatus === "NO_REFERENCE" ? (
                <p className="text-micro text-muted-foreground italic">
                  未记录基准输出引用。
                </p>
              ) : baselineOutputStatus === "NOT_FOUND" ? (
                <p className="text-micro text-muted-foreground italic">
                  基准版本远程 Observation 已过期或不存在。
                </p>
              ) : baselineOutputStatus === "FETCH_FAILED" ? (
                <div className="space-y-1.5" data-testid="baseline-fetch-failed">
                  <p className="text-fail text-micro">
                    获取基准输出失败{baselineOutput.reason ? `: ${baselineOutput.reason}` : ""}
                  </p>
                  {(baselineOutput.retryable ?? true) && (
                    <Button
                      variant="secondary"
                      className="h-6 text-2xs px-2"
                      onClick={() => caseOutputQuery.refetch()}
                    >
                      重试读取基准输出
                    </Button>
                  )}
                </div>
              ) : (
                <p className="text-micro text-muted-foreground italic">基准输出暂不可用。</p>
              )}
            </div>
          )}

          {!caseOutputQuery.isLoading && !baselineOutput && (
            <p className="text-micro text-muted-foreground italic">
              当前未关联基准版本输出。
            </p>
          )}
        </div>

        {/* 候选输出面板 (Candidate Output) */}
        <div className="p-3 bg-surface-subtle rounded-lg border border-border space-y-2">
          <div className="flex items-center justify-between">
            <span className="font-semibold text-foreground">Agent 真实答复 (Candidate Output)</span>
            {outputStatus && (
              <span className="px-1.5 py-0.5 rounded text-micro font-mono bg-surface border border-border">
                {outputStatus}
              </span>
            )}
          </div>

          {caseOutputQuery.isLoading && (
            <div className="flex items-center gap-2 text-micro text-muted-foreground py-2">
              <RefreshCw className="w-3.5 h-3.5 animate-spin text-primary" />
              <span>正在按快照引用懒加载候选输出...</span>
            </div>
          )}

          {caseOutputQuery.error && (
            <div className="space-y-1.5">
              <p className="text-fail text-micro">
                读取输出失败: {formatApiError(caseOutputQuery.error)}
              </p>
              <Button
                variant="secondary"
                className="h-6 text-2xs px-2"
                onClick={() => caseOutputQuery.refetch()}
              >
                重试读取
              </Button>
            </div>
          )}

          {candidateOutput && (
            <div className="space-y-2">
              {candidateOutput.truncated && (
                <p className="text-micro text-timeout">输出超过大小限制，已截断显示。</p>
              )}
              {candidateOutput.reason && (
                <p className="text-micro text-muted-foreground">{candidateOutput.reason}</p>
              )}
              {outputStatus === "AVAILABLE" && candidateOutput.output != null ? (
                <div className="text-foreground">
                  {typeof candidateOutput.output === "string" ? (
                    <p className="leading-relaxed font-mono whitespace-pre-wrap text-xs">
                      {candidateOutput.output}
                    </p>
                  ) : (
                    <JsonViewer data={candidateOutput.output} title="Agent Output JSON" />
                  )}
                </div>
              ) : outputStatus === "NO_REFERENCE" ? (
                <p className="text-micro text-muted-foreground italic">
                  未记录输出引用（无 Observation 关联）。
                </p>
              ) : outputStatus === "NOT_FOUND" ? (
                <p className="text-micro text-muted-foreground italic">
                  远程 Observation 已过期或不存在。
                </p>
              ) : (
                <p className="text-micro text-muted-foreground italic">输出暂不可用。</p>
              )}

              {candidateOutput.retryable && (
                <Button
                  variant="secondary"
                  className="h-6 text-2xs px-2"
                  onClick={() => caseOutputQuery.refetch()}
                >
                  重试读取不可用输出
                </Button>
              )}
            </div>
          )}

          {!snapshotId && (
            <p className="text-micro text-muted-foreground italic">
              待评测完成冻结快照后可查看真实输出。
            </p>
          )}
        </div>
      </div>

      {/* 评估扣分与指标详情 */}
      <div className="space-y-2">
        <span className="font-semibold text-foreground block">
          类型化评测结果 (Evaluation Results)
        </span>
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
          <span className="text-xs text-muted-foreground font-mono">无评测结果记录</span>
        )}
      </div>

      {/* 底部操作与 Attempt 入口 */}
      <div className="flex items-center justify-between pt-2 border-t border-border">
        <span className="text-micro font-mono">
          Item Execution ID: {item.id}
        </span>
        <Button
          type="button"
          variant="secondary"
          onClick={onOpenAttemptDrawer}
          className="h-7 text-xs font-mono"
        >
          <Layers className="w-3.5 h-3.5 text-primary" />
          <span>查看调用历史 ({item.attempt_count} 次尝试)</span>
          <Eye className="w-3.5 h-3.5 text-muted-foreground" />
        </Button>
      </div>
    </div>
  );
};
