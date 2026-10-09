import React, { useMemo, useState } from "react";
import { ExternalLink, ShieldCheck, Sparkles } from "lucide-react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { api } from "../../../api/client";
import { queryKeys } from "../../../api/query-keys";
import { formatApiError } from "../../../api/errors";
import { Button } from "../../../components/ui/Primitives";
import { ComparisonCaseDrawer } from "../ComparisonCaseDrawer";
import { LangfuseSyncPanel } from "../langfuseSyncStatus";

type GeneratedRunSummary = import("../../../api/schema").components["schemas"]["RunSummaryResponse"];
type GeneratedComparison = import("../../../api/schema").components["schemas"]["ComparisonResponse"];
type Baseline = import("../../../api/schema").components["schemas"]["BaselineResponse"];
type FrozenVersions = {
  agent?: { id?: string | null; version?: string | null };
  dataset?: { name?: string | null; version?: string | null; digest?: string | null };
  evaluators?: Array<{ id: string; version?: string | null }>;
  runner?: { runner_version?: string | null; build_id?: string | null };
  environment?: string;
};
type RunSummary = Omit<GeneratedRunSummary, "versions"> & { versions: FrozenVersions };
type GeneratedRunMetrics = GeneratedRunSummary["summary"];
type CostReason = NonNullable<GeneratedRunMetrics["cost_unavailable_reason"]>
  | NonNullable<GeneratedComparison["summary"]["cost_comparison"]["reason"]>;
type RunMetrics = GeneratedRunMetrics & {
  pass_rate?: number | null;
  evaluation_coverage?: number | null;
  critical_failure_count?: number | null;
  execution_error_count?: number | null;
  execution_error_rate?: number | null;
  evaluator_error_count?: number | null;
  p95_latency_ms?: number | null;
  score_means?: Record<string, number | null>;
  total_cases?: number;
  evaluated_cases?: number;
};
type ComparisonSummary = GeneratedComparison["summary"] & {
  candidate: RunMetrics;
  baseline: RunMetrics | null;
  comparable_cohort: { baseline: RunMetrics; candidate: RunMetrics } | null;
};
type ContractDimension = {
  dimension: string;
  status: string;
  baseline_digest?: string | null;
  candidate_digest?: string | null;
  baseline_version?: string | null;
  candidate_version?: string | null;
};
type Comparability = {
  comparable: boolean;
  reason_codes: string[];
  provenance: string;
  dimensions: ContractDimension[];
  suggestions: string[];
};
type FormalVerdict = {
  available: boolean;
  verdict?: string | null;
  reason?: string | null;
  required_cases?: number;
  comparable_cases?: number;
  coverage?: number;
  withheld_reasons: string[];
};
type ComparisonItem = {
  dataset_item_id?: string | null;
  classification: string;
  reason?: string | null;
  basis?: string | null;
  non_numeric_evaluators?: string[] | null;
  baseline_scores?: Record<string, number> | null;
  candidate_scores?: Record<string, number> | null;
  score_deltas?: Record<string, number> | null;
  baseline_trace_url?: string | null;
  candidate_trace_url?: string | null;
  baseline_experiment_url?: string | null;
  candidate_experiment_url?: string | null;
};
type Comparison = Omit<GeneratedComparison, "versions" | "items"> & {
  versions: { baseline: FrozenVersions | null; candidate: FrozenVersions };
  items: ComparisonItem[];
  next_cursor?: number | null;
  comparability?: Comparability;
  formal?: FormalVerdict;
};

const DIMENSION_LABELS: Record<string, string> = {
  MEASUREMENT: "测量版本",
  QUALITY_POLICY: "质量策略",
  AGGREGATION_COMPARISON: "比较口径",
};
const DIMENSION_STATUS_LABELS: Record<string, string> = {
  MATCH: "一致",
  CHANGED: "已变化",
  UNKNOWN: "证据缺失",
};
const REASON_LABELS: Record<string, string> = {
  DATASET_CHANGED: "Dataset 不同",
  MEASUREMENT_CHANGED: "测量版本不同",
  QUALITY_POLICY_CHANGED: "判定规则不同",
  AGGREGATION_COMPARISON_CHANGED: "比较口径不同",
  CONTRACT_PROVENANCE_UNKNOWN: "历史契约证据缺失",
};
const WITHHELD_LABELS: Record<string, string> = {
  BASELINE_NOT_BOUND: "尚未绑定 Baseline",
  BASELINE_EVIDENCE_INCOMPLETE: "Baseline 证据不足",
  CANDIDATE_EVIDENCE_INCOMPLETE: "Candidate 证据不足",
  COVERAGE_INCOMPLETE: "证据不足",
};
const VERDICT_LABELS: Record<string, string> = {
  REGRESSION: "Regression",
  IMPROVEMENT: "Improvement",
  UNCHANGED: "无变化",
};
const reasonText = (code: string) => REASON_LABELS[code] ?? code;
const withheldText = (code: string) => WITHHELD_LABELS[code] ?? REASON_LABELS[code] ?? code;
const dimensionLabel = (dimension: string) => DIMENSION_LABELS[dimension] ?? dimension;
const dimensionStatusText = (status: string) => DIMENSION_STATUS_LABELS[status] ?? status;
const verdictText = (verdict: string) => VERDICT_LABELS[verdict] ?? verdict;
const shortDigest = (digest?: string | null) => (digest ? digest.replace(/^sha256:/, "").slice(0, 12) : "—");

const FILTERS = ["ALL", "REGRESSION", "IMPROVEMENT", "UNCHANGED", "NOT_COMPARABLE"] as const;

const percent = (value: number | null | undefined) => value == null ? "—" : `${(value * 100).toFixed(1)}%`;
const number = (value: number | null | undefined, suffix = "") => value == null ? "—" : `${value.toFixed(2)}${suffix}`;
const count = (value: number | null | undefined) => value == null ? "—" : String(value);
const money = (value: number | null | undefined, currency: string | null | undefined) => {
  if (value == null || !currency) return "—";
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency", currency, minimumFractionDigits: 2, maximumFractionDigits: 10,
    }).format(value);
  } catch {
    return `${currency} ${value.toFixed(10)}`;
  }
};
const signedMoney = (value: number | null | undefined, currency: string | null | undefined) => {
  if (value == null) return "—";
  return `${value > 0 ? "+" : value < 0 ? "-" : ""}${money(Math.abs(value), currency)}`;
};
const costCoverage = (metrics: RunMetrics | null | undefined) => {
  if (metrics?.cost_coverage == null) return "—";
  const covered = metrics.cost_case_count ?? 0;
  const total = metrics.total_cases == null ? "?" : String(metrics.total_cases);
  return `${covered}/${total} (${percent(metrics.cost_coverage)})`;
};
const costReasonText: Record<CostReason, string> = {
  BASELINE_NOT_BOUND: "尚未绑定 Baseline，暂不能计算成本差值。",
  COST_NOT_RECORDED: "未记录成本的 Case 不按 0 计入。",
  INVALID_COST_EVIDENCE: "存在无效成本证据，成本暂不可比较。",
  INCOMPLETE_ATTEMPT_COST: "至少一个 Retry Attempt 缺少成本证据，完整 Case 成本不可用。",
  MIXED_CURRENCIES: "存在多种币种，不会直接相加。",
  COST_CURRENCY_MISMATCH: "Baseline 与 Candidate 币种不同，不会直接比较。",
  COST_SCOPE_MISMATCH: "Baseline 与 Candidate 成本测量范围不同。",
  COST_POLICY_MISMATCH: "Baseline 与 Candidate 成本统计策略版本不同。",
  COST_SOURCE_MISMATCH: "Baseline 与 Candidate 成本来源不同。",
  PARTIAL_COST_COVERAGE: "覆盖不完整；Cost / Case 仅按有效覆盖展示，差值不计算。",
  NO_COMPARABLE_CASES: "没有共同可比 Case，成本差值未计算。",
};
const signedDelta = (before: number | null | undefined, after: number | null | undefined, digits = 2, suffix = "") => {
  if (before == null || after == null) return "—";
  const delta = after - before;
  return `${delta > 0 ? "+" : ""}${delta.toFixed(digits)}${suffix}`;
};
const percentagePointDelta = (before: number | null | undefined, after: number | null | undefined) => {
  if (before == null || after == null) return "—";
  const delta = (after - before) * 100;
  return `${delta > 0 ? "+" : ""}${delta.toFixed(1)} pp`;
};

interface GateComparisonTabProps {
  launchId: string;
  snapshotId: string | null;
  environment: string;
  summary: RunSummary | null | undefined;
  activeBaseline: Baseline | null;
  onSetBaselineModal?: () => void;
  onShowLatestSnapshot?: () => void;
  qualityPolicyRules?: Array<{
    evaluator_id?: string;
    operator?: string | null;
    threshold?: number | null;
    expected_value?: unknown;
    required?: boolean;
    critical?: boolean;
    result_type?: string;
  }>;
  manifestEvaluators?: Array<{
    id?: string;
    version?: string;
    direction?: string;
    result_type?: string;
    required?: boolean;
  }>;
  currentFilter?: string;
  onFilterChange?: (filter: string) => void;
  selectedCaseId?: string | null;
  onSelectedCaseChange?: (caseId: string | null) => void;
}

export const GateComparisonTab: React.FC<GateComparisonTabProps> = ({
  launchId,
  snapshotId,
  environment,
  summary,
  activeBaseline,
  onSetBaselineModal,
  onShowLatestSnapshot,
  qualityPolicyRules = [],
  manifestEvaluators = [],
  currentFilter,
  onFilterChange,
  selectedCaseId,
  onSelectedCaseChange,
}) => {
  const [internalFilter, setInternalFilter] = useState<(typeof FILTERS)[number]>("ALL");
  const filter = (currentFilter !== undefined ? currentFilter : internalFilter) as (typeof FILTERS)[number];
  const setFilter = (next: (typeof FILTERS)[number]) => {
    setInternalFilter(next);
    onFilterChange?.(next);
  };

  const [internalSelectedCase, setInternalSelectedCase] = useState<string | null>(null);
  const selectedCase = selectedCaseId !== undefined ? selectedCaseId : internalSelectedCase;
  const setSelectedCase = (next: string | null) => {
    setInternalSelectedCase(next);
    onSelectedCaseChange?.(next);
  };

  const comparisonQuery = useInfiniteQuery({
    queryKey: queryKeys.launches.comparison(launchId, snapshotId ?? "unresolved", filter),
    initialPageParam: 0,
    queryFn: async ({ pageParam }) => {
      const response = await api.GET("/api/v1/experiment-launches/{launch_id}/comparison", {
        params: {
          path: { launch_id: launchId },
          query: {
            snapshot_id: snapshotId ?? undefined,
            classification: filter === "ALL" ? undefined : filter,
            limit: 50,
            cursor: pageParam,
          },
        },
      });
      if (response.error) throw response.error;
      if (!response.data) throw new Error("Comparison response is empty");
      if (response.data.candidate_snapshot_id !== snapshotId) {
        throw new Error("Comparison page belongs to a different result snapshot; refusing to mix revisions");
      }
      return response.data as unknown as Comparison;
    },
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: Boolean(launchId && snapshotId),
  });

  const comparison = comparisonQuery.data?.pages[0];
  const comparisonSummary = comparison?.summary as ComparisonSummary | undefined;
  const cohort = comparisonSummary?.comparable_cohort;
  const versions = comparison?.versions;
  const metrics = summary?.summary;

  const displayRows = useMemo(
    () => comparisonQuery.data?.pages.flatMap((page) => page.items ?? []) ?? [],
    [comparisonQuery.data],
  );

  const fullRunCandidate = comparisonSummary?.candidate ?? (metrics as RunMetrics | undefined);
  const fullRunBaseline = comparisonSummary?.baseline;

  const evaluatorIds = Array.from(
    new Set([
      ...Object.keys(cohort?.baseline.score_means ?? {}),
      ...Object.keys(cohort?.candidate.score_means ?? {}),
    ]),
  ).sort();

  // Evaluator requirements map from quality policy
  const rulesMap = useMemo(() => {
    const map = new Map<string, string>();
    for (const rule of qualityPolicyRules) {
      if (!rule.evaluator_id) continue;
      const evalMeta = manifestEvaluators.find((e) => e.id === rule.evaluator_id);
      const isNumeric =
        rule.result_type === "numeric" ||
        evalMeta?.result_type === "numeric" ||
        rule.evaluator_id.toLowerCase().includes("cost") ||
        rule.evaluator_id.toLowerCase().includes("duration") ||
        rule.evaluator_id.toLowerCase().includes("latency") ||
        (rule.threshold != null && rule.threshold > 1);

      let exp = "";
      if (rule.operator === ">=" || rule.operator === "<=" || rule.operator === ">" || rule.operator === "<") {
        const formattedVal = rule.threshold != null
          ? isNumeric
            ? String(rule.threshold)
            : (rule.threshold * 100).toFixed(1) + "%"
          : "—";
        exp = `${rule.operator} ${formattedVal}`;
      } else if (rule.operator === "==") {
        exp = `== ${rule.expected_value != null ? String(rule.expected_value) : "—"}`;
      } else {
        exp = "记录证据";
      }
      if (rule.required) exp += " (关键)";
      map.set(rule.evaluator_id, exp);
    }
    return map;
  }, [qualityPolicyRules, manifestEvaluators]);

  // Derived regression table metrics (Evaluator-level comparison)
  const regressionMetricsRows = useMemo(() => {
    const rows = [];

    // 1. Overall Pass Rate
    if (cohort) {
      const bPass = cohort.baseline.pass_rate;
      const cPass = cohort.candidate.pass_rate;
      const deltaPp = percentagePointDelta(bPass, cPass);
      const overallRule = qualityPolicyRules.find(
        (r) => r.evaluator_id === "overall_pass_rate" || r.evaluator_id === "overall",
      );
      const reqText = overallRule?.threshold != null
        ? `${overallRule.operator ?? "≥"} ${(overallRule.threshold * 100).toFixed(1)}%`
        : "—";
      const thresholdVal = overallRule?.threshold;
      const isMet = thresholdVal != null && cPass != null ? cPass >= thresholdVal : null;
      rows.push({
        name: "综合质量通过率 (Overall Pass)",
        requirement: reqText,
        baseline: percent(bPass),
        candidate: percent(cPass),
        delta: deltaPp,
        statusLabel: isMet === true ? "达标" : isMet === false ? "未达标" : "—",
        statusTone: isMet === true ? "pass" : isMet === false ? "fail" : "neutral",
      });
    }

    // 2. Evaluator Score rows
    for (const evalId of evaluatorIds) {
      const bScore = cohort?.baseline.score_means?.[evalId];
      const cScore = cohort?.candidate.score_means?.[evalId];
      const evalMeta = manifestEvaluators.find((e) => e.id === evalId);
      const rule = qualityPolicyRules.find((r) => r.evaluator_id === evalId);
      // N05: 严格按契约声明判断类型，不使用数值大小擅自决定类型
      const isNumeric =
        rule?.result_type === "numeric" ||
        evalMeta?.result_type === "numeric" ||
        evalId.toLowerCase().includes("cost") ||
        evalId.toLowerCase().includes("duration") ||
        evalId.toLowerCase().includes("latency");

      // N05: 显式 direction 优先，名称启发式绝不覆盖明确的 higher_is_better
      let isLowerBetter = false;
      let hasKnownDirection = false;

      if (evalMeta?.direction === "higher_is_better") {
        isLowerBetter = false;
        hasKnownDirection = true;
      } else if (evalMeta?.direction === "lower_is_better") {
        isLowerBetter = true;
        hasKnownDirection = true;
      } else if (rule?.operator === ">" || rule?.operator === ">=") {
        isLowerBetter = false;
        hasKnownDirection = true;
      } else if (rule?.operator === "<" || rule?.operator === "<=") {
        isLowerBetter = true;
        hasKnownDirection = true;
      } else if (
        evalId.toLowerCase().includes("cost") ||
        evalId.toLowerCase().includes("duration") ||
        evalId.toLowerCase().includes("latency")
      ) {
        isLowerBetter = true;
        hasKnownDirection = true;
      }

      let baselineText = "—";
      let candidateText = "—";
      let deltaText = "—";

      if (isNumeric) {
        baselineText = bScore != null ? String(bScore) : "—";
        candidateText = cScore != null ? String(cScore) : "—";
        if (bScore != null && cScore != null) {
          const diff = Number((cScore - bScore).toFixed(4));
          deltaText = `${diff > 0 ? "+" : ""}${diff}`;
        }
      } else {
        baselineText = percent(bScore);
        candidateText = percent(cScore);
        deltaText = percentagePointDelta(bScore, cScore);
      }

      const req = rulesMap.get(evalId) || "—";

      let statusLabel = "—";
      let statusTone: "pass" | "fail" | "neutral" = "neutral";

      if (bScore != null && cScore != null) {
        if (cScore === bScore) {
          statusLabel = "持平";
          statusTone = "pass";
        } else if (hasKnownDirection) {
          const isImproved = isLowerBetter ? cScore < bScore : cScore > bScore;
          if (isImproved) {
            statusLabel = "提升";
            statusTone = "pass";
          } else {
            statusLabel = isLowerBetter ? "退化" : "下降";
            statusTone = "fail";
          }
        } else {
          statusLabel = "变化";
          statusTone = "neutral";
        }
      } else if (cScore != null) {
        statusLabel = "已测量";
        statusTone = "neutral";
      }

      rows.push({
        name: evalId,
        requirement: req,
        baseline: baselineText,
        candidate: candidateText,
        delta: deltaText,
        statusLabel,
        statusTone,
      });
    }

    // 3. P95 Latency
    if (cohort?.candidate.p95_latency_ms != null) {
      const bLat = cohort?.baseline.p95_latency_ms;
      const cLat = cohort.candidate.p95_latency_ms;
      const diff = bLat != null ? cLat - bLat : 0;
      const deltaText = bLat != null ? `${diff > 0 ? "+" : ""}${diff} ms` : "—";
      const latencyRule = qualityPolicyRules.find(
        (r) =>
          r.evaluator_id === "latency" ||
          r.evaluator_id === "p95_latency" ||
          r.evaluator_id === "p95_latency_ms" ||
          r.evaluator_id === "duration_ms",
      );
      let latReq = "—";
      if (latencyRule && latencyRule.threshold != null) {
        latReq = `${latencyRule.operator ?? "<="} ${latencyRule.threshold} ms`;
      }
      rows.push({
        name: "P95 响应时延 (Latency)",
        requirement: latReq,
        baseline: bLat != null ? `${Math.round(bLat)} ms` : "—",
        candidate: `${Math.round(cLat)} ms`,
        delta: deltaText,
        statusLabel: diff <= 0 ? "优于基线" : "增加",
        statusTone: diff <= 0 ? "pass" : "timeout",
      });
    }

    return rows;
  }, [cohort, evaluatorIds, rulesMap, qualityPolicyRules, manifestEvaluators]);

  // Aggregate and Health rows
  const aggregateRows = cohort ? [
    { label: "质量通过率 (Quality Pass Rate)", baseline: percent(cohort.baseline.pass_rate), candidate: percent(cohort.candidate.pass_rate), delta: percentagePointDelta(cohort.baseline.pass_rate, cohort.candidate.pass_rate) },
    { label: "Critical Failure Cases", baseline: number(cohort.baseline.critical_failure_count), candidate: number(cohort.candidate.critical_failure_count), delta: signedDelta(cohort.baseline.critical_failure_count, cohort.candidate.critical_failure_count) },
    { label: "P95 Latency", baseline: number(cohort.baseline.p95_latency_ms, " ms"), candidate: number(cohort.candidate.p95_latency_ms, " ms"), delta: signedDelta(cohort.baseline.p95_latency_ms, cohort.candidate.p95_latency_ms, 2, " ms") },
    {
      label: "Cost / Case",
      baseline: money(cohort.baseline.cost_per_case, cohort.baseline.cost_currency),
      candidate: money(cohort.candidate.cost_per_case, cohort.candidate.cost_currency),
      delta: comparisonSummary?.cost_comparison?.status === "COMPARABLE"
        ? signedMoney(comparisonSummary.cost_comparison.delta, comparisonSummary.cost_comparison.currency)
        : "—",
    },
    { label: "Cost Coverage", baseline: costCoverage(cohort.baseline), candidate: costCoverage(cohort.candidate), delta: percentagePointDelta(cohort.baseline.cost_coverage, cohort.candidate.cost_coverage) },
    ...evaluatorIds.map((id) => ({
      label: `Score · ${id}`,
      baseline: number(cohort.baseline.score_means?.[id]),
      candidate: number(cohort.candidate.score_means?.[id]),
      delta: signedDelta(cohort.baseline.score_means?.[id], cohort.candidate.score_means?.[id]),
    })),
  ] : [];

  const healthRows = fullRunCandidate ? [
    { label: "Total Cases", baseline: count(fullRunBaseline?.total_cases), candidate: count(fullRunCandidate.total_cases), delta: signedDelta(fullRunBaseline?.total_cases, fullRunCandidate.total_cases, 0) },
    { label: "Evaluated Cases", baseline: count(fullRunBaseline?.evaluated_cases), candidate: count(fullRunCandidate.evaluated_cases), delta: signedDelta(fullRunBaseline?.evaluated_cases, fullRunCandidate.evaluated_cases, 0) },
    { label: "Evaluation Coverage", baseline: percent(fullRunBaseline?.evaluation_coverage), candidate: percent(fullRunCandidate.evaluation_coverage), delta: percentagePointDelta(fullRunBaseline?.evaluation_coverage, fullRunCandidate.evaluation_coverage) },
    { label: "Execution Errors", baseline: number(fullRunBaseline?.execution_error_count), candidate: number(fullRunCandidate.execution_error_count), delta: signedDelta(fullRunBaseline?.execution_error_count, fullRunCandidate.execution_error_count) },
    { label: "Execution Error Rate", baseline: percent(fullRunBaseline?.execution_error_rate), candidate: percent(fullRunCandidate.execution_error_rate), delta: percentagePointDelta(fullRunBaseline?.execution_error_rate, fullRunCandidate.execution_error_rate) },
    { label: "Evaluator Errors", baseline: number(fullRunBaseline?.evaluator_error_count), candidate: number(fullRunCandidate.evaluator_error_count), delta: signedDelta(fullRunBaseline?.evaluator_error_count, fullRunCandidate.evaluator_error_count) },
    { label: "Run Cost / Case", baseline: money(fullRunBaseline?.cost_per_case, fullRunBaseline?.cost_currency), candidate: money(fullRunCandidate.cost_per_case, fullRunCandidate.cost_currency), delta: "—" },
    { label: "Run Cost Coverage", baseline: costCoverage(fullRunBaseline), candidate: costCoverage(fullRunCandidate), delta: "—" },
  ] : [];

  const comparability = comparison?.comparability;
  const formal = comparison?.formal;
  const costExplanations = [
    { label: "全量 Baseline", reason: fullRunBaseline?.cost_unavailable_reason },
    { label: "全量 Candidate", reason: fullRunCandidate?.cost_unavailable_reason },
    { label: "共同可比 Case", reason: comparisonSummary?.cost_comparison?.reason },
  ].filter((entry): entry is { label: string; reason: CostReason } => entry.reason != null);

  const isRegression =
    formal?.verdict === "REGRESSION" ||
    (cohort?.baseline.pass_rate != null &&
      cohort.candidate.pass_rate != null &&
      cohort.candidate.pass_rate < cohort.baseline.pass_rate);

  return (
    <div className="space-y-6">
      {/* 错误提示横幅 (Alert) */}
      {comparisonQuery.isError && (
        <div
          role="alert"
          className="p-3 text-xs bg-fail-subtle border border-fail-border rounded-lg text-fail font-medium flex items-center justify-between"
        >
          <span>{formatApiError(comparisonQuery.error)}</span>
          <Button
            variant="secondary"
            className="h-6 text-2xs px-2"
            onClick={() => comparisonQuery.refetch()}
          >
            重试
          </Button>
        </div>
      )}

      {/* 1. 核心能力跃升/变化摘要卡片（严格基于真实数据与指标） */}
      {cohort && (
        <div
          data-testid="capabilities-delta-summary"
          className="bg-surface border border-pass-border rounded-xl p-5 space-y-3 shadow-xs"
        >
          <div className="flex items-center gap-2 text-sm font-semibold text-pass">
            <Sparkles className="w-4 h-4" />
            <span>核心评测能力与指标变化摘要</span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
            <div className="bg-surface-subtle border border-border rounded-lg p-3 space-y-1">
              <span className="font-semibold text-foreground flex items-center gap-1.5">
                <span className={`w-2 h-2 rounded-full ${isRegression ? "bg-fail" : "bg-pass"}`} />
                {isRegression
                  ? "综合质量通过率下降"
                  : (cohort.candidate.pass_rate ?? 0) > (cohort.baseline.pass_rate ?? 0)
                  ? "综合质量通过率提升"
                  : "综合质量通过率持平"}
              </span>
              <p className="text-muted-foreground leading-relaxed">
                {isRegression ? (
                  <>
                    共同可比用例通过率从 {percent(cohort.baseline.pass_rate)} 下降至 {percent(cohort.candidate.pass_rate)}（{percentagePointDelta(cohort.baseline.pass_rate, cohort.candidate.pass_rate)}）。
                  </>
                ) : (cohort.candidate.pass_rate ?? 0) > (cohort.baseline.pass_rate ?? 0) ? (
                  <>
                    共同可比用例通过率从 {percent(cohort.baseline.pass_rate)} 提升至 {percent(cohort.candidate.pass_rate)}（{percentagePointDelta(cohort.baseline.pass_rate, cohort.candidate.pass_rate)}）。
                  </>
                ) : (
                  <>
                    共同可比用例通过率与基线持平（{percent(cohort.candidate.pass_rate)}）。
                  </>
                )}
              </p>
            </div>
            {evaluatorIds.length > 0 && (
              <div className="bg-surface-subtle border border-border rounded-lg p-3 space-y-1">
                <span className="font-semibold text-foreground flex items-center gap-1.5">
                  <span
                    className={`w-2 h-2 rounded-full ${
                      !formal?.available
                        ? "bg-timeout"
                        : isRegression
                        ? "bg-fail"
                        : "bg-pass"
                    }`}
                  />
                  {!formal?.available
                    ? "评测门禁结论未就绪"
                    : isRegression
                    ? "评测门禁未达标 (存在退化)"
                    : "评测规则门禁达标"}
                </span>
                <p className="text-muted-foreground leading-relaxed">
                  {!formal?.available
                    ? "由于证据不足或基线未绑定，暂无法出具正式门禁准入结论。"
                    : isRegression
                    ? `共计 ${evaluatorIds.length} 项评估指标已比对，存在退化项需排查。`
                    : `共计 ${evaluatorIds.length} 项评估指标已完成与基线对比，全部关键约束已纳入版本质量门禁监控。`}
                </p>
              </div>
            )}
            <div className="bg-surface-subtle border border-border rounded-lg p-3 space-y-1">
              <span className="font-semibold text-foreground flex items-center gap-1.5">
                <span className="w-2 h-2 rounded-full bg-pass" />
                运行健康与时延表现
              </span>
              <p className="text-muted-foreground leading-relaxed">
                候选版本 P95 响应时延为{" "}
                {cohort.candidate.p95_latency_ms != null
                  ? `${Math.round(cohort.candidate.p95_latency_ms)} ms`
                  : "—"}
                {cohort.baseline.p95_latency_ms != null && (
                  <span>（基线为 {Math.round(cohort.baseline.p95_latency_ms)} ms）</span>
                )}。
              </p>
            </div>
          </div>
        </div>
      )}

      {/* 2. 核心对比表格 (Regression Metrics) */}
      <div className="bg-surface border border-border rounded-xl overflow-hidden shadow-xs">
        <div className="px-5 py-4 border-b border-border flex items-center justify-between flex-wrap gap-2">
          <div>
            <h3 className="text-sm font-bold text-foreground">回归评测指标对比 (Regression Metrics)</h3>
            <p className="text-xs text-muted-foreground mt-0.5">
              基准环境: {environment} · 比较口径: {comparisonSummary?.comparable_case_count ?? 0} 个共同用例比对
              {summary && (
                <span className="ml-2 font-medium" data-testid="summary-evidence-state">
                  证据 {summary.evidence_state === "COMPLETE" ? "完整" : "诊断"}
                </span>
              )}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {comparability?.comparable ? (
              <span className="text-xs bg-pass-subtle text-pass px-2.5 py-1 rounded-lg border border-pass-border font-medium">
                对比口径有效 · 无环境漂移
              </span>
            ) : (
              <span className="text-xs bg-surface text-muted-foreground px-2.5 py-1 rounded-lg border border-border">
                {comparability ? "契约存在差异" : "基线解析中"}
              </span>
            )}
            {summary?.evidence_state === "COMPLETE" && onSetBaselineModal ? (
              <Button
                variant="secondary"
                className="h-7 text-xs"
                onClick={onSetBaselineModal}
                title="将当前固定版本设为该环境的 Baseline"
              >
                <ShieldCheck className="h-3.5 w-3.5" />
                <span>设为当前环境 Baseline</span>
              </Button>
            ) : summary ? (
              <span
                className="text-xs text-muted-foreground"
                data-testid="baseline-ineligible-hint"
                title={summary.evidence_reasons?.join("; ") ?? ""}
              >
                当前版本证据不足，不可设为 Baseline
              </span>
            ) : null}
            {onShowLatestSnapshot && snapshotId && (
              <Button variant="secondary" className="h-7 text-xs" onClick={onShowLatestSnapshot}>
                查看最新修订
              </Button>
            )}
          </div>
        </div>

        {/* 核心回归指标表 */}
        <div className="overflow-x-auto text-xs">
          <table className="w-full text-left border-collapse">
            <thead>
              <tr className="bg-surface-muted text-muted-foreground border-b border-border">
                <th className="py-3 px-5 font-semibold">指标项 (Evaluator)</th>
                <th className="py-3 px-5 font-semibold">门禁要求</th>
                <th className="py-3 px-5 font-semibold">基准版本 (Baseline)</th>
                <th className="py-3 px-5 font-semibold">候选版本 (Candidate)</th>
                <th className="py-3 px-5 font-semibold">变化幅值 (Delta)</th>
                <th className="py-3 px-5 font-semibold">结论</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border text-foreground">
              {regressionMetricsRows.map((row) => (
                <tr key={row.name} className="hover:bg-surface-subtle transition-colors">
                  <td className="py-3.5 px-5 font-medium">{row.name}</td>
                  <td className="py-3.5 px-5 font-mono text-muted-foreground">{row.requirement}</td>
                  <td className="py-3.5 px-5 font-mono text-muted-foreground">{row.baseline}</td>
                  <td className="py-3.5 px-5 font-mono font-semibold text-foreground">{row.candidate}</td>
                  <td
                    className={`py-3.5 px-5 font-mono font-semibold ${
                      row.statusTone === "pass"
                        ? "text-pass"
                        : row.statusTone === "fail"
                        ? "text-fail"
                        : row.statusTone === "timeout"
                        ? "text-timeout"
                        : "text-muted-foreground"
                    }`}
                  >
                    {row.delta}
                  </td>
                  <td className="py-3.5 px-5">
                    <span
                      className={`px-2 py-0.5 rounded text-xs font-medium ${
                        row.statusTone === "pass"
                          ? "bg-pass-subtle text-pass border border-pass-border"
                          : row.statusTone === "fail"
                          ? "bg-fail-subtle text-fail border border-fail-border"
                          : "bg-surface-muted text-muted-foreground"
                      }`}
                    >
                      {row.statusLabel}
                    </span>
                  </td>
                </tr>
              ))}
              {regressionMetricsRows.length === 0 && (
                <tr>
                  <td colSpan={6} className="py-8 text-center text-muted-foreground">
                    暂无可比指标数据。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* 3. Baseline 状态说明行 */}
      {activeBaseline && (
        <p className="text-xs text-muted-foreground" data-testid="baseline-revision-line">
          当前 Baseline：结果修订 Revision {activeBaseline.result_revision} · 绑定修订 {activeBaseline.revision} ·
          Snapshot {activeBaseline.result_snapshot_id?.slice(0, 8)}… · 证据{" "}
          {activeBaseline.result_evidence_state === "COMPLETE" ? "完整" : "诊断"}
        </p>
      )}

      {/* 4. Comparability Banner */}
      {comparability && !comparability.comparable && (
        <div
          data-testid="comparison-comparability-banner"
          role="status"
          className="space-y-3 rounded-lg border border-warning-border bg-warning-subtle p-4 text-warning-strong"
        >
          <div>
            <h3 className="text-sm font-semibold">判定规则不同，无法正式比较</h3>
            <p className="mt-1 text-xs">
              以下维度在两侧不一致，本次结果不会给出正式 Regression / Improvement 结论，也不会被当作「无回归」。
            </p>
          </div>
          <ul className="flex flex-wrap gap-2">
            {comparability.reason_codes.map((code) => (
              <li
                key={code}
                data-testid={`comparability-reason-${code}`}
                className="rounded-full border border-border bg-surface px-2 py-0.5 text-micro text-foreground"
              >
                {reasonText(code)}
              </li>
            ))}
          </ul>
          <div className="space-y-1">
            {comparability.dimensions.map((dim) => (
              <div
                key={dim.dimension}
                data-testid={`comparability-dimension-${dim.dimension}`}
                className="flex flex-wrap items-center gap-2 text-micro"
              >
                <span className="font-medium text-foreground">{dimensionLabel(dim.dimension)}</span>
                <span className={dim.status === "MATCH" ? "text-muted-foreground" : "text-timeout"}>
                  {dimensionStatusText(dim.status)}
                </span>
                <span className="font-mono text-muted-foreground">
                  {dim.baseline_version ?? "—"} → {dim.candidate_version ?? "—"}
                </span>
                {dim.status === "CHANGED" && (
                  <span className="font-mono text-muted-foreground">
                    {shortDigest(dim.baseline_digest)} → {shortDigest(dim.candidate_digest)}
                  </span>
                )}
              </div>
            ))}
          </div>
          {comparability.suggestions.length > 0 && (
            <ul className="space-y-1">
              {comparability.suggestions.map((hint, index) => (
                <li key={hint} data-testid={`comparability-suggestion-${index}`} className="text-micro">
                  建议：{hint}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* 5. Formal Verdict */}
      {formal && (
        <div
          data-testid="comparison-formal-verdict"
          className="rounded-lg border border-border bg-surface p-4"
        >
          <div className="text-micro font-medium text-muted-foreground">正式比较裁决</div>
          {formal.available ? (
            <div className="mt-1 flex flex-wrap items-baseline gap-2">
              <span className="text-lg font-semibold text-foreground">
                {verdictText(formal.verdict ?? "")}
              </span>
              <span className="text-micro text-muted-foreground">
                全部 {formal.required_cases ?? 0} 个必要 Case 均在同一契约下判定
              </span>
            </div>
          ) : (
            <div className="mt-1 space-y-1">
              <div className="text-sm font-semibold text-foreground">无法给出正式结论</div>
              <ul className="flex flex-wrap gap-2">
                {(formal.withheld_reasons ?? []).map((code) => (
                  <li
                    key={code}
                    data-testid={`formal-withheld-${code}`}
                    className="rounded-full border border-border bg-surface-muted px-2 py-0.5 text-micro text-muted-foreground"
                  >
                    {withheldText(code)}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {comparison && !formal?.available && (
        <p data-testid="comparison-diagnostic-label" className="text-micro text-muted-foreground">
          以下 Case 分类仅供诊断，不作为正式发布比较。
        </p>
      )}

      {/* 6. 全量运行健康与共同样本质量 */}
      {healthRows.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-xs font-semibold text-foreground">全量运行健康（各自 Snapshot 全部 Case）</h3>
          <div className="overflow-x-auto rounded-lg border border-border">
            <table aria-label="Baseline 与 Candidate 全量运行健康指标" className="min-w-full divide-y divide-border text-left text-xs">
              <thead className="bg-surface-muted text-muted-foreground">
                <tr>
                  <th className="px-3 py-2">指标（全量 Case）</th>
                  <th className="px-3 py-2">Baseline</th>
                  <th className="px-3 py-2">Candidate</th>
                  <th className="px-3 py-2">差值</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border bg-surface">
                {healthRows.map((row) => (
                  <tr key={row.label}>
                    <th scope="row" className="px-3 py-2 font-medium text-foreground">{row.label}</th>
                    <td className="px-3 py-2 font-mono">{row.baseline}</td>
                    <td className="px-3 py-2 font-mono">{row.candidate}</td>
                    <td className="px-3 py-2 font-mono">{row.delta}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {cohort && (
        <div className="space-y-2">
          <h3 className="text-xs font-semibold text-foreground">
            共同可比样本质量（{comparisonSummary?.comparable_case_count ?? 0} 个 Case）
          </h3>
          <p
            data-testid="comparable-quality-pass-rate-help"
            className="pb-1 text-micro text-muted-foreground"
          >
            质量通过率说明：仅统计双方共同可比样本。质量通过率为质量 PASS 数 / 有效已评测数；有效已评测要求执行成功、评测成功且质量结论为 PASS 或 FAIL。不可比或无有效质量结论的用例不参与该比例，请结合全量运行健康指标查看评测覆盖率与错误数。
          </p>
          <div className="overflow-x-auto rounded-lg border border-border">
            <table aria-label="Baseline 与 Candidate 聚合指标对比" className="min-w-full divide-y divide-border text-left text-xs">
              <thead className="bg-surface-muted text-muted-foreground">
                <tr>
                  <th className="px-3 py-2">指标（可比 Case 范围）</th>
                  <th className="px-3 py-2">Baseline</th>
                  <th className="px-3 py-2">Candidate</th>
                  <th className="px-3 py-2">Δ Candidate − Baseline</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border bg-surface">
                {aggregateRows.map((row) => (
                  <tr key={row.label}>
                    <th scope="row" className="px-3 py-2 font-medium text-foreground">{row.label}</th>
                    <td className="px-3 py-2 font-mono">{row.baseline}</td>
                    <td className="px-3 py-2 font-mono">{row.candidate}</td>
                    <td className="px-3 py-2 font-mono">{row.delta}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {summary?.langfuse_sync && (
        <LangfuseSyncPanel sync={summary.langfuse_sync} />
      )}

      {costExplanations.length > 0 && (
        <p role="status" className="rounded-lg border border-border bg-surface p-3 text-xs text-muted-foreground">
          {costExplanations.map(({ label, reason }) => (
            <span key={label} className="block">
              {label} 成本说明：{costReasonText[reason] ?? reason}
            </span>
          ))}
        </p>
      )}

      {/* 7. 双侧版本卡片 */}
      {versions && (
        <div className="grid gap-3 md:grid-cols-2">
          <VersionCard title="Baseline" versions={versions.baseline} snapshotId={comparison?.baseline_snapshot_id ?? null} />
          <VersionCard title="Candidate" versions={versions.candidate} snapshotId={comparison?.candidate_snapshot_id ?? null} />
        </div>
      )}

      {/* 8. Regression Case 筛选与列表 */}
      {comparison && (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-2" aria-label="Regression Case 筛选">
            {FILTERS.map((value) => (
              <button
                key={value}
                type="button"
                aria-pressed={filter === value}
                onClick={() => setFilter(value)}
                className={`rounded-md border px-2.5 py-1.5 text-xs font-semibold transition-colors cursor-pointer ${
                  filter === value
                    ? "border-primary bg-primary-subtle text-primary"
                    : "border-border text-muted-foreground hover:bg-surface-muted"
                }`}
              >
                {value === "ALL" ? "全部" : `${value} (${comparison.classification_counts[value] ?? 0})`}
              </button>
            ))}
          </div>

          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="min-w-full divide-y divide-border text-left text-xs">
              <thead className="bg-surface-muted text-muted-foreground">
                <tr>
                  <th className="px-3 py-2">Case</th>
                  <th className="px-3 py-2">分类 / 原因</th>
                  <th className="px-3 py-2">Baseline Score</th>
                  <th className="px-3 py-2">Candidate Score / Δ</th>
                  <th className="px-3 py-2">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border bg-surface">
                {displayRows.map((row) => (
                  <tr key={`${row.dataset_item_id}-${row.classification}`}>
                    <td className="px-3 py-2 font-mono">{row.dataset_item_id}</td>
                    <td className="px-3 py-2">
                      <strong>{row.classification}</strong>
                      <span className="ml-1 text-muted-foreground">{row.reason}</span>
                      {row.basis && row.basis !== "FORMAL" && (
                        <span
                          data-testid={`comparison-item-basis-${row.basis}`}
                          className="ml-2 rounded-full border border-border bg-surface-muted px-2 py-0.5 text-micro text-muted-foreground"
                        >
                          仅诊断
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2 font-mono">{JSON.stringify(row.baseline_scores ?? {})}</td>
                    <td className="px-3 py-2 font-mono">
                      {JSON.stringify(row.candidate_scores ?? {})}
                      <span className="ml-1 text-muted-foreground">Δ {JSON.stringify(row.score_deltas ?? {})}</span>
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex flex-wrap gap-2">
                        {row.baseline_experiment_url && (
                          <a
                            href={row.baseline_experiment_url}
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex items-center gap-1 text-primary hover:underline"
                          >
                            Baseline Experiment <ExternalLink className="h-3 w-3" />
                          </a>
                        )}
                        {row.baseline_trace_url && (
                          <a
                            href={row.baseline_trace_url}
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex items-center gap-1 text-primary hover:underline"
                          >
                            Baseline Trace <ExternalLink className="h-3 w-3" />
                          </a>
                        )}
                        {row.candidate_experiment_url && (
                          <a
                            href={row.candidate_experiment_url}
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex items-center gap-1 text-primary hover:underline"
                          >
                            Candidate Experiment <ExternalLink className="h-3 w-3" />
                          </a>
                        )}
                        {row.candidate_trace_url && (
                          <a
                            href={row.candidate_trace_url}
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex items-center gap-1 text-primary hover:underline"
                          >
                            Candidate Trace <ExternalLink className="h-3 w-3" />
                          </a>
                        )}
                        {row.dataset_item_id && snapshotId && (
                          <Button
                            variant="secondary"
                            className="text-xs"
                            onClick={() => setSelectedCase(row.dataset_item_id ?? null)}
                          >
                            查看双侧输出
                          </Button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
                {displayRows.length === 0 && (
                  <tr>
                    <td colSpan={5} className="px-3 py-8 text-center text-muted-foreground">
                      当前筛选没有用例。
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {comparisonQuery.hasNextPage && (
            <div className="flex justify-center">
              <Button
                variant="secondary"
                className="text-xs"
                onClick={() => comparisonQuery.fetchNextPage()}
                disabled={comparisonQuery.isFetchingNextPage}
              >
                {comparisonQuery.isFetchingNextPage ? "加载中…" : `加载更多用例（已显示 ${displayRows.length} 条）`}
              </Button>
            </div>
          )}
        </div>
      )}

      {/* 9. 双侧输出抽屉 */}
      {selectedCase && snapshotId && (
        <ComparisonCaseDrawer
          launchId={launchId}
          snapshotId={snapshotId}
          datasetItemId={selectedCase}
          onClose={() => setSelectedCase(null)}
        />
      )}
    </div>
  );
};

const VersionCard: React.FC<{ title: string; versions: any; snapshotId: string | null }> = ({
  title,
  versions,
  snapshotId,
}) => (
  <div className="rounded-lg border border-border bg-surface p-3 text-xs">
    <strong className="text-foreground">{title}</strong>
    {versions ? (
      // token-lint-ignore: intrinsic two-column definition list, not a design token.
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        <dt className="text-muted-foreground">Agent</dt>
        <dd className="truncate font-mono">{versions.agent?.id}@{versions.agent?.version}</dd>
        <dt className="text-muted-foreground">Dataset</dt>
        <dd className="truncate font-mono">{versions.dataset?.name}@{versions.dataset?.version}</dd>
        <dt className="text-muted-foreground">Evaluators</dt>
        <dd className="font-mono">
          {Array.isArray(versions.evaluators) && versions.evaluators.length > 0
            ? versions.evaluators.map((item: any) => `${item.id}@${item.version}`).join(", ")
            : "—"}
        </dd>
        <dt className="text-muted-foreground">Runner</dt>
        <dd className="truncate font-mono">{versions.runner?.runner_version} ({versions.runner?.build_id})</dd>
        <dt className="text-muted-foreground">Snapshot</dt>
        <dd className="truncate font-mono">{snapshotId ?? "未绑定"}</dd>
      </dl>
    ) : (
      <p className="mt-2 text-muted-foreground">当前 Launch 创建时未解析到 Baseline。</p>
    )}
  </div>
);
