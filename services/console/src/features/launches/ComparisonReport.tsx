import React, { useEffect, useMemo, useState } from "react";
import { ExternalLink, ShieldCheck } from "lucide-react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";
import { Button, Panel } from "../../components/ui/Primitives";
import { ComparisonCaseDrawer } from "./ComparisonCaseDrawer";

type GeneratedRunSummary = import("../../api/schema").components["schemas"]["RunSummaryResponse"];
type GeneratedComparison = import("../../api/schema").components["schemas"]["ComparisonResponse"];
type Baseline = import("../../api/schema").components["schemas"]["BaselineResponse"];
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
type ComparisonItem = {
  dataset_item_id?: string | null;
  classification: string;
  reason?: string | null;
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
};

const TERMINAL = new Set(["COMPLETED", "PARTIAL_FAILED", "FAILED", "CANCELLED"]);
const FILTERS = ["ALL", "REGRESSION", "IMPROVEMENT", "UNCHANGED", "NOT_COMPARABLE"] as const;
type ReportViewState = {
  launchId: string;
  snapshotId: string | null;
  filter: (typeof FILTERS)[number];
  selectedCase: string | null;
};

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

export const ComparisonReport: React.FC<{
  launchId: string;
  launchStatus: string;
  environment: string;
}> = ({ launchId, launchStatus, environment }) => {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const snapshotId = searchParams.get("snapshot_id");
  const [latestRequest, setLatestRequest] = useState(0);
  const [viewState, setViewState] = useState<ReportViewState>(() => ({
    launchId,
    snapshotId,
    filter: "ALL",
    selectedCase: null,
  }));
  const isViewStateCurrent = viewState.launchId === launchId && viewState.snapshotId === snapshotId;
  const filter = isViewStateCurrent ? viewState.filter : "ALL";
  const selectedCase = isViewStateCurrent ? viewState.selectedCase : null;
  const canReadResults = TERMINAL.has(launchStatus) || Boolean(snapshotId);

  useEffect(() => {
    if (isViewStateCurrent) return;
    setViewState({ launchId, snapshotId, filter: "ALL", selectedCase: null });
  }, [isViewStateCurrent, launchId, snapshotId]);

  const updateViewState = (update: Partial<Pick<ReportViewState, "filter" | "selectedCase">>) => {
    setViewState((current) => {
      const currentForRoute = current.launchId === launchId && current.snapshotId === snapshotId;
      const base = currentForRoute
        ? current
        : { launchId, snapshotId, filter: "ALL" as const, selectedCase: null };
      return { ...base, ...update, launchId, snapshotId };
    });
  };

  const summaryQuery = useQuery({
    queryKey: queryKeys.launches.summary(launchId, snapshotId, latestRequest),
    queryFn: async () => {
      const response = await api.GET("/api/v1/experiment-launches/{launch_id}/summary", {
        params: { path: { launch_id: launchId }, query: { snapshot_id: snapshotId ?? undefined } },
      });
      if (response.error) throw response.error;
      if (!response.data) throw new Error("Run summary response is empty");
      return response.data as unknown as RunSummary;
    },
    enabled: canReadResults,
    refetchInterval: (query) => query.state.data?.langfuse_score_sync_status === "PENDING" || query.state.data?.langfuse_score_sync_status === "PROCESSING" ? 3000 : false,
  });

  useEffect(() => {
    if (
      snapshotId ||
      summaryQuery.data?.launch_id !== launchId ||
      !summaryQuery.data.snapshot_id
    ) return;
    const resolvedSnapshotId = summaryQuery.data.snapshot_id;
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      if (!next.has("snapshot_id")) next.set("snapshot_id", resolvedSnapshotId);
      return next;
    }, { replace: true });
  }, [launchId, setSearchParams, snapshotId, summaryQuery.data?.launch_id, summaryQuery.data?.snapshot_id]);

  const showLatestSnapshot = () => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      next.delete("snapshot_id");
      return next;
    }, { replace: true });
    setViewState({ launchId, snapshotId: null, filter: "ALL", selectedCase: null });
    setLatestRequest((request) => request + 1);
  };

  // Baseline reads are keyed by the Agent ID embedded in the Launch manifest; the parent supplies it below.
  const agentId = summaryQuery.data?.versions.agent?.id ?? undefined;
  const fetchActiveBaseline = async (): Promise<Baseline | null> => {
    if (!agentId) return null;
    const response = await api.GET("/api/v1/agents/{agent_id}/baselines", {
      params: { path: { agent_id: agentId }, query: { environment } },
    });
    if (response.error) {
      if (response.response.status === 404) return null;
      throw response.error;
    }
    return response.data as unknown as Baseline;
  };
  const baselineQueryKey = queryKeys.baselines.detail(agentId || "", environment);
  const activeBaselineQuery = useQuery({
    queryKey: baselineQueryKey,
    queryFn: fetchActiveBaseline,
    enabled: Boolean(agentId),
  });

  const comparisonQuery = useInfiniteQuery({
    queryKey: queryKeys.launches.comparison(launchId, snapshotId ?? "unresolved", filter),
    initialPageParam: 0,
    queryFn: async ({ pageParam }) => {
      const response = await api.GET("/api/v1/experiment-launches/{launch_id}/comparison", {
        params: {
          path: { launch_id: launchId },
          query: { snapshot_id: snapshotId ?? undefined, classification: filter === "ALL" ? undefined : filter, limit: 50, cursor: pageParam },
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
    enabled: canReadResults && Boolean(snapshotId),
  });

  const setBaselineMutation = useMutation({
    mutationFn: async () => {
      if (!summaryQuery.data || !snapshotId || !agentId) throw new Error("Run result snapshot is not ready");
      const activeBaseline = await queryClient.fetchQuery({
        queryKey: baselineQueryKey,
        queryFn: fetchActiveBaseline,
        staleTime: 0,
      });
      const response = await api.POST("/api/v1/agents/{agent_id}/baselines", {
        params: { path: { agent_id: agentId } },
        body: {
          environment,
          result_snapshot_id: snapshotId,
          expected_revision: activeBaseline?.revision ?? 0,
        },
      });
      if (response.error) throw response.error;
      return response.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.baselines.detail(agentId || "", environment) });
      queryClient.invalidateQueries({ queryKey: queryKeys.launches.comparison(launchId, snapshotId ?? "unresolved", filter) });
    },
  });

  const metrics = summaryQuery.data?.summary;
  const comparison = comparisonQuery.data?.pages[0];
  const comparisonSummary = comparison?.summary as ComparisonSummary | undefined;
  const cohort = comparisonSummary?.comparable_cohort;
  const versions = comparison?.versions;
  const displayRows = useMemo(
    () => comparisonQuery.data?.pages.flatMap((page) => page.items ?? []) ?? [],
    [comparisonQuery.data],
  );
  const fullRunCandidate = comparisonSummary?.candidate ?? metrics as RunMetrics | undefined;
  const fullRunBaseline = comparisonSummary?.baseline;
  const evaluatorIds = Array.from(new Set([
    ...Object.keys(cohort?.baseline.score_means ?? {}),
    ...Object.keys(cohort?.candidate.score_means ?? {}),
  ])).sort();
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
  const costExplanations = [
    { label: "全量 Baseline", reason: fullRunBaseline?.cost_unavailable_reason },
    { label: "全量 Candidate", reason: fullRunCandidate?.cost_unavailable_reason },
    { label: "共同可比 Case", reason: comparisonSummary?.cost_comparison?.reason },
  ].filter((entry): entry is { label: string; reason: CostReason } => entry.reason != null);

  if (!canReadResults) return null;

  return (
    <Panel className="space-y-4 p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-bold text-foreground">Regression Summary / Baseline Comparison</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            结果修订 {summaryQuery.data?.revision ?? "…"} · Snapshot: {snapshotId ?? "解析中"} · Environment: {environment} · Langfuse Run Score: {summaryQuery.data?.langfuse_score_sync_status ?? "加载中"}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {snapshotId && <Button variant="secondary" className="text-xs" onClick={showLatestSnapshot}>查看最新修订</Button>}
        {launchStatus === "COMPLETED" && summaryQuery.data && (
          <Button
            variant="secondary"
            className="text-xs"
            onClick={() => setBaselineMutation.mutate()}
            disabled={setBaselineMutation.isPending}
          >
            <ShieldCheck className="h-3.5 w-3.5" />
            {setBaselineMutation.isPending ? "绑定中…" : "设为当前环境 Baseline"}
          </Button>
        )}
        </div>
      </div>

      {setBaselineMutation.error && <p role="alert" className="text-xs text-fail">{formatApiError(setBaselineMutation.error)}</p>}
      {summaryQuery.error && <p role="alert" className="text-xs text-fail">{formatApiError(summaryQuery.error)}</p>}
      {comparisonQuery.error && <p role="alert" className="text-xs text-fail">{formatApiError(comparisonQuery.error)}</p>}
      {activeBaselineQuery.error && <p role="alert" className="text-xs text-fail">Baseline 查询失败：{formatApiError(activeBaselineQuery.error)}</p>}

      {metrics && (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-2 text-xs">
            <Metric label="Regression Cases" value={String(comparison?.classification_counts.REGRESSION ?? 0)} />
            <Metric label="Improved Cases" value={String(comparison?.classification_counts.IMPROVEMENT ?? 0)} />
            <Metric label="可比 Case" value={String(comparisonSummary?.comparable_case_count ?? 0)} />
          </div>
          {healthRows.length > 0 && (
            <div className="space-y-1">
              <h3 className="text-xs font-semibold text-foreground">全量运行健康（各自 Snapshot 全部 Case）</h3>
              <div className="overflow-x-auto rounded-lg border border-border">
                <table aria-label="Baseline 与 Candidate 全量运行健康指标" className="min-w-full divide-y divide-border text-left text-xs">
                  <thead className="bg-canvas text-muted-foreground"><tr><th className="px-3 py-2">指标（全量 Case）</th><th className="px-3 py-2">Baseline</th><th className="px-3 py-2">Candidate</th><th className="px-3 py-2">差值</th></tr></thead>
                  <tbody className="divide-y divide-border bg-surface">{healthRows.map((row) => <tr key={row.label}><th scope="row" className="px-3 py-2 font-medium text-foreground">{row.label}</th><td className="px-3 py-2 font-mono">{row.baseline}</td><td className="px-3 py-2 font-mono">{row.candidate}</td><td className="px-3 py-2 font-mono">{row.delta}</td></tr>)}</tbody>
                </table>
              </div>
            </div>
          )}
          {cohort ? (
            <div className="space-y-1">
            <h3 className="text-xs font-semibold text-foreground">共同可比样本质量（{comparisonSummary?.comparable_case_count ?? 0} 个 Case）</h3>
            {/* This ratio uses a different denominator from the all-cases quality
                ratio in the Launch detail header. Name the cohort and the
                "evaluable" rule so the two are never read as one metric. */}
            <p
              data-testid="comparable-quality-pass-rate-help"
              className="pb-1 text-micro text-muted-foreground"
            >
              质量通过率说明：仅统计双方共同可比样本。质量通过率为质量 PASS 数 / 有效已评测数；有效已评测要求执行成功、评测成功且质量结论为 PASS 或 FAIL。不可比或无有效质量结论的用例不参与该比例，请结合全量运行健康指标查看评测覆盖率与错误数。
            </p>
            <div className="overflow-x-auto rounded-lg border border-border">
              <table aria-label="Baseline 与 Candidate 聚合指标对比" className="min-w-full divide-y divide-border text-left text-xs">
                <thead className="bg-canvas text-muted-foreground">
                  <tr><th className="px-3 py-2">指标（可比 Case 范围）</th><th className="px-3 py-2">Baseline</th><th className="px-3 py-2">Candidate</th><th className="px-3 py-2">Δ Candidate − Baseline</th></tr>
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
          ) : comparisonSummary && (
            <p className="rounded border border-border p-3 text-xs text-muted-foreground">无可比样本，质量差异未计算。</p>
          )}
          {costExplanations.length > 0 && (
            <p role="status" className="rounded border border-border p-3 text-xs text-muted-foreground">
              {costExplanations.map(({ label, reason }) => (
                <span key={label} className="block">
                  {label} 成本说明：{costReasonText[reason] ?? reason}
                </span>
              ))}
            </p>
          )}
        </div>
      )}

      {versions && (
        <div className="grid gap-3 md:grid-cols-2">
          <VersionCard title="Baseline" versions={versions.baseline} snapshotId={comparison?.baseline_snapshot_id ?? null} />
          <VersionCard title="Candidate" versions={versions.candidate} snapshotId={comparison?.candidate_snapshot_id ?? null} />
        </div>
      )}

      {comparison && (
        <>
          <div className="flex flex-wrap gap-2" aria-label="Regression Case 筛选">
            {FILTERS.map((value) => (
              <button
                key={value}
                type="button"
                aria-pressed={filter === value}
                onClick={() => updateViewState({ filter: value })}
                className={`rounded-md border px-2.5 py-1.5 text-xs font-semibold transition-colors ${filter === value ? "border-primary bg-primary-subtle text-primary" : "border-border text-muted-foreground hover:bg-canvas"}`}
              >
                {value === "ALL" ? "全部" : `${value} (${comparison.classification_counts[value] ?? 0})`}
              </button>
            ))}
          </div>
          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="min-w-full divide-y divide-border text-left text-xs">
              <thead className="bg-canvas text-muted-foreground">
                <tr><th className="px-3 py-2">Case</th><th className="px-3 py-2">分类 / 原因</th><th className="px-3 py-2">Baseline Score</th><th className="px-3 py-2">Candidate Score / Δ</th><th className="px-3 py-2">原始结果</th></tr>
              </thead>
              <tbody className="divide-y divide-border bg-surface">
                {displayRows.map((row) => (
                  <tr key={`${row.dataset_item_id}-${row.classification}`}>
                    <td className="px-3 py-2 font-mono">{row.dataset_item_id}</td>
                    <td className="px-3 py-2"><strong>{row.classification}</strong><span className="ml-1 text-muted-foreground">{row.reason}</span></td>
                    <td className="px-3 py-2 font-mono">{JSON.stringify(row.baseline_scores ?? {})}</td>
                    <td className="px-3 py-2 font-mono">{JSON.stringify(row.candidate_scores ?? {})}<span className="ml-1 text-muted-foreground">Δ {JSON.stringify(row.score_deltas ?? {})}</span></td>
                    <td className="px-3 py-2">
                      <div className="flex flex-wrap gap-2">
                        {row.baseline_experiment_url && <a href={row.baseline_experiment_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-primary hover:underline">Baseline Experiment <ExternalLink className="h-3 w-3" /></a>}
                        {row.baseline_trace_url && <a href={row.baseline_trace_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-primary hover:underline">Baseline Trace <ExternalLink className="h-3 w-3" /></a>}
                        {row.candidate_experiment_url && <a href={row.candidate_experiment_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-primary hover:underline">Candidate Experiment <ExternalLink className="h-3 w-3" /></a>}
                        {row.candidate_trace_url && <a href={row.candidate_trace_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-primary hover:underline">Candidate Trace <ExternalLink className="h-3 w-3" /></a>}
                        {row.dataset_item_id && row.classification === "REGRESSION" && snapshotId && <Button variant="secondary" className="text-xs" onClick={() => {
                          updateViewState({ selectedCase: row.dataset_item_id ?? null });
                        }}>查看双侧输出</Button>}
                      </div>
                    </td>
                  </tr>
                ))}
                {displayRows.length === 0 && <tr><td colSpan={5} className="px-3 py-8 text-center text-muted-foreground">当前筛选没有 Regression Case。</td></tr>}
              </tbody>
            </table>
          </div>
          {comparisonQuery.hasNextPage && (
            <div className="flex justify-center">
              <Button variant="secondary" className="text-xs" onClick={() => comparisonQuery.fetchNextPage()} disabled={comparisonQuery.isFetchingNextPage}>
                {comparisonQuery.isFetchingNextPage ? "加载中…" : `加载更多用例（已显示 ${displayRows.length} 条）`}
              </Button>
            </div>
          )}
          <p className="text-micro text-muted-foreground">
            只在同一 Dataset、相同 Case 内容与 Evaluator 契约上分类；全量运行健康与共同样本质量分别统计。成本差值仅对共同可比 Case 且双方完整覆盖、币种与口径兼容时计算；部分覆盖仅表示已有证据 Case 的均值，缺失成本不会按 0 计入。Case 输出按当前固定 Snapshot 延迟读取；历史页面使用冻结 Snapshot，不可用指标显示为 —。
          </p>
        </>
      )}
      {selectedCase && snapshotId && <ComparisonCaseDrawer
        launchId={launchId}
        snapshotId={snapshotId}
        datasetItemId={selectedCase}
        onClose={() => updateViewState({ selectedCase: null })}
      />}
    </Panel>
  );
};

const Metric: React.FC<{ label: string; value: string; detail?: string }> = ({ label, value, detail }) => (
  <div className="rounded-lg border border-border bg-canvas/70 p-3">
    <div className="text-micro font-medium text-muted-foreground">{label}</div>
    <div className="mt-1 text-lg font-semibold text-foreground">{value}</div>
    {detail && <div className="mt-0.5 text-micro text-muted-foreground">{detail}</div>}
  </div>
);

const VersionCard: React.FC<{ title: string; versions: any; snapshotId: string | null }> = ({ title, versions, snapshotId }) => (
  <div className="rounded-lg border border-border bg-canvas/60 p-3 text-xs">
    <strong>{title}</strong>
    {versions ? (
      // token-lint-ignore: intrinsic two-column definition list, not a design token.
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        <dt className="text-muted-foreground">Agent</dt><dd className="truncate font-mono">{versions.agent?.id}@{versions.agent?.version}</dd>
        <dt className="text-muted-foreground">Dataset</dt><dd className="truncate font-mono">{versions.dataset?.name}@{versions.dataset?.version}</dd>
        <dt className="text-muted-foreground">Evaluators</dt><dd className="font-mono">{Array.isArray(versions.evaluators) && versions.evaluators.length > 0 ? versions.evaluators.map((item: any) => `${item.id}@${item.version}`).join(", ") : "—"}</dd>
        <dt className="text-muted-foreground">Runner</dt><dd className="truncate font-mono">{versions.runner?.runner_version} ({versions.runner?.build_id})</dd>
        <dt className="text-muted-foreground">Snapshot</dt><dd className="truncate font-mono">{snapshotId ?? "未绑定"}</dd>
      </dl>
    ) : <p className="mt-2 text-muted-foreground">当前 Launch 创建时未解析到 Baseline。</p>}
  </div>
);
