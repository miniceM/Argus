import React from "react";
import clsx from "clsx";
import { ArrowDown, ArrowUp, AlertCircle, CheckCircle2, Minus } from "lucide-react";

type RunMetrics = {
  pass_rate?: number | null;
  p95_latency_ms?: number | null;
  cost_per_case?: number | null;
  cost_currency?: string | null;
  cost_coverage?: number | null;
  cost_case_count?: number | null;
  cost_unavailable_reason?: string | null;
  total_cases?: number;
};

type ComparisonSummary = {
  comparable_case_count?: number;
  candidate?: RunMetrics;
  baseline?: RunMetrics | null;
  comparable_cohort?: { baseline: RunMetrics; candidate: RunMetrics } | null;
  cost_comparison?: {
    status?: string;
    delta?: number | null;
    currency?: string | null;
    reason?: string | null;
  } | null;
};

interface LaunchKpiOverviewProps {
  totalItems: number | null;
  passedItems: number | null;
  decisionCounts: { pass: number; fail: number; unknown: number } | null;
  decidedPassRate: string | null;
  decisionCoverage: number | null;
  itemsError?: boolean;
  comparisonSummary?: ComparisonSummary | null;
  classificationCounts?: Record<string, number>;
  comparability?: { comparable: boolean; reason_codes: string[] } | null;
  formal?: { available: boolean; verdict?: string | null; withheld_reasons?: string[] } | null;
  onNavigateTab?: (tabId: string, filter?: string) => void;
}

const formatMoney = (value: number | null | undefined, currency: string | null | undefined = "USD") => {
  if (value == null) return "—";
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: currency || "USD",
      minimumFractionDigits: 2,
      maximumFractionDigits: 4,
    }).format(value);
  } catch {
    return `${currency || "$"} ${value.toFixed(4)}`;
  }
};

export const LaunchKpiOverview: React.FC<LaunchKpiOverviewProps> = ({
  totalItems,
  passedItems,
  decisionCounts,
  decidedPassRate,
  decisionCoverage,
  itemsError = false,
  comparisonSummary,
  classificationCounts,
  comparability,
  formal,
  onNavigateTab,
}) => {
  // 1. Quality Pass Rate calculation
  const total = totalItems ?? 0;
  const passed = passedItems ?? 0;
  const unknown = decisionCounts?.unknown ?? 0;
  const allUnknown = total > 0 && unknown === total;

  let passRateText = "—";
  let passRateFraction = "";
  if (totalItems !== null) {
    if (total === 0) {
      passRateText = "—";
      passRateFraction = "0/0";
    } else if (allUnknown) {
      passRateText = "0%";
      passRateFraction = `0/${total} 例 (全部证据不足)`;
    } else {
      const pct = ((passed / total) * 100).toFixed(1);
      passRateText = `${pct}%`;
      passRateFraction = `${passed}/${total} 例`;
    }
  }

  // Delta relative to baseline
  const cohort = comparisonSummary?.comparable_cohort;
  let passRateDeltaText: string | null = null;
  let isPassRateImproved: boolean | null = null;

  const baselinePassRate = cohort?.baseline.pass_rate;
  const candidatePassRate = cohort?.candidate.pass_rate;
  if (
    typeof baselinePassRate === "number" && Number.isFinite(baselinePassRate) &&
    typeof candidatePassRate === "number" && Number.isFinite(candidatePassRate)
  ) {
    const diff = (candidatePassRate - baselinePassRate) * 100;
    const diffText = `${diff > 0 ? "+" : ""}${diff.toFixed(1)} pp`;
    if (diff > 0) {
      passRateDeltaText = `较基线提升 ${diffText}`;
      isPassRateImproved = true;
    } else if (diff < 0) {
      passRateDeltaText = `较基线下降 ${diffText}`;
      isPassRateImproved = false;
    } else {
      passRateDeltaText = `较基线持平 (0.0 pp)`;
      isPassRateImproved = null;
    }
  }

  // 2. Regression items calculation
  const isComparable = comparability?.comparable ?? false;
  const isFormalAvailable = formal?.available ?? false;
  let regressionValueText = "—";
  let regressionSubText = "基线未绑定或不可比";
  let regressionStatusTone: "pass" | "timeout" | "neutral" = "neutral";

  if (!isComparable && comparability?.reason_codes?.length) {
    regressionValueText = "不可比";
    regressionSubText = "判定规则或测量版本不同";
    regressionStatusTone = "timeout";
  } else if (!isFormalAvailable) {
    if (formal?.withheld_reasons?.includes("BASELINE_NOT_BOUND")) {
      regressionValueText = "—";
      regressionSubText = "尚未绑定当前环境 Baseline";
      regressionStatusTone = "neutral";
    } else {
      regressionValueText = "不可用";
      regressionSubText = "证据不足，暂无法给出正式结论";
      regressionStatusTone = "timeout";
    }
  } else {
    const regCount = classificationCounts?.REGRESSION ?? 0;
    regressionValueText = String(regCount);
    if (regCount === 0) {
      regressionSubText = "历史通过项全部保持绿灯";
      regressionStatusTone = "pass";
    } else {
      regressionSubText = `${regCount} 项性能退化，需排查`;
      regressionStatusTone = "timeout";
    }
  }

  // 3. P95 Latency
  const candidateP95 = cohort?.candidate.p95_latency_ms ?? comparisonSummary?.candidate?.p95_latency_ms;
  const baselineP95 = cohort?.baseline.p95_latency_ms ?? comparisonSummary?.baseline?.p95_latency_ms;
  const hasCandidateP95 = typeof candidateP95 === "number" && Number.isFinite(candidateP95);
  const hasBaselineP95 = typeof baselineP95 === "number" && Number.isFinite(baselineP95);
  let p95Text = "—";
  let p95DeltaText: string | null = null;
  let isLatencyImproved: boolean | null = null;

  if (hasCandidateP95) {
    p95Text = String(Math.round(candidateP95));
    if (hasBaselineP95) {
      const diff = Math.round(candidateP95 - baselineP95);
      if (diff < 0) {
        p95DeltaText = `较基线缩短 ${Math.abs(diff)}ms`;
        isLatencyImproved = true;
      } else if (diff > 0) {
        p95DeltaText = `较基线增加 ${diff}ms`;
        isLatencyImproved = false;
      } else {
        p95DeltaText = "与基线持平";
      }
    } else {
      p95DeltaText = "单版本测量";
    }
  }

  // 4. Cost per case
  const candidateCost = cohort?.candidate.cost_per_case ?? comparisonSummary?.candidate?.cost_per_case;
  const costCurrency = cohort?.candidate.cost_currency ?? comparisonSummary?.candidate?.cost_currency ?? "USD";
  const costReason = comparisonSummary?.candidate?.cost_unavailable_reason ?? comparisonSummary?.cost_comparison?.reason;
  let costText = "—";
  let costSubText = "暂无可比较的成本对比";

  if (candidateCost != null) {
    costText = formatMoney(candidateCost, costCurrency);
    const costDelta = comparisonSummary?.cost_comparison?.delta;
    if (
      comparisonSummary?.cost_comparison?.status === "COMPARABLE" &&
      typeof costDelta === "number" && Number.isFinite(costDelta)
    ) {
      const delta = costDelta;
      if (delta > 0) {
        costSubText = `较基线增加 ${formatMoney(delta, costCurrency)}`;
      } else if (delta < 0) {
        costSubText = `较基线节省 ${formatMoney(Math.abs(delta), costCurrency)}`;
      } else {
        costSubText = "单例成本与基线持平";
      }
    } else if (costReason) {
      costSubText = "成本证据不足，无法比较";
    }
  } else if (costReason) {
    costText = "未记录";
    costSubText = "未记录成本，不按0补齐";
  }

  return (
    <section aria-label="核心决策指标" className="space-y-3">
      {/* 顶部标题 */}
      <div className="text-xs font-semibold text-foreground px-1">
        核心决策指标 (KPI Overview)
      </div>

      {/* 核心指标微卡片 */}
      <div data-testid="kpi-cards-grid" className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
          {/* KPI 1: 综合质量通过率 */}
          <div
            data-testid="quality-pass-rate"
            role="button"
            tabIndex={0}
            className="bg-surface border border-border rounded-xl p-3.5 flex flex-col justify-between shadow-xs hover:border-border-strong transition cursor-pointer focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
            onClick={() => onNavigateTab?.("cases")}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onNavigateTab?.("cases");
              }
            }}
            title="点击前往用例排查"
            aria-label="综合质量通过率，点击前往用例排查"
          >
            <div>
              <div id="quality-pass-rate-label" aria-describedby="quality-pass-rate-help" className="text-xs font-medium text-muted-foreground mb-2 flex items-center justify-between">
                <span>综合质量通过率</span>
                <span className="sr-only">质量判定汇总 (Quality Decision Summary)</span>
                {decisionCounts && !itemsError && (
                  <span className="flex items-center gap-1 font-mono text-micro font-bold">
                    <span className="text-pass" data-testid="quality-count-pass">PASS {decisionCounts.pass}</span>
                    <span className="text-fail" data-testid="quality-count-fail">FAIL {decisionCounts.fail}</span>
                    <span className={decisionCounts.unknown > 0 ? "text-timeout" : "text-muted-foreground"} data-testid="quality-count-unknown">
                      UNKNOWN {decisionCounts.unknown}
                    </span>
                  </span>
                )}
              </div>
              {itemsError || !decisionCounts ? (
                <span className="text-xs text-fail">暂不可用</span>
              ) : (
                <div className="flex items-baseline gap-2">
                  <span className="text-2xl font-bold font-mono tracking-tight text-foreground">{passRateText}</span>
                  <span className="text-xs font-mono text-muted-foreground font-normal">{passRateFraction}</span>
                </div>
              )}
            </div>
            <div className="mt-3 pt-2.5 border-t border-border space-y-1">
              <div className="flex items-center gap-1.5 text-xs">
                {isPassRateImproved === true ? (
                  <span className="inline-flex items-center gap-1 text-pass font-medium">
                    <ArrowUp className="w-3.5 h-3.5" />
                    <span>{passRateDeltaText}</span>
                  </span>
                ) : isPassRateImproved === false ? (
                  <span className="inline-flex items-center gap-1 text-fail font-medium">
                    <ArrowDown className="w-3.5 h-3.5" />
                    <span>{passRateDeltaText}</span>
                  </span>
                ) : (
                  <span className="text-muted-foreground flex items-center gap-1">
                    <Minus className="w-3.5 h-3.5" />
                    <span>{passRateDeltaText || "暂无基线对比"}</span>
                  </span>
                )}
              </div>
              <div className="text-micro text-muted-foreground font-mono">
                已判定通过率：<span data-testid="decided-pass-rate">{decidedPassRate ? `${decidedPassRate}% (已判定)` : "—"}</span>
                {" · "}
                判定覆盖率：<span data-testid="decision-coverage">{decisionCoverage != null ? `${decisionCoverage.toFixed(1)}%` : "—"}</span>
              </div>
            </div>
          </div>

          {/* KPI 2: 正式回归用例数 */}
          <div
            role="button"
            tabIndex={0}
            className="bg-surface border border-border rounded-xl p-3.5 flex flex-col justify-between shadow-xs hover:border-border-strong transition cursor-pointer focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
            onClick={() => onNavigateTab?.("compare")}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onNavigateTab?.("compare");
              }
            }}
            title="点击前往门禁对比"
            aria-label="正式回归用例数，点击前往门禁对比"
          >
            <div>
              <div className="text-xs font-medium text-muted-foreground mb-2">正式回归用例数</div>
              <div className="flex items-baseline gap-2">
                <span
                  className={clsx(
                    "text-2xl font-bold font-mono tracking-tight",
                    regressionStatusTone === "pass" ? "text-pass" : regressionStatusTone === "timeout" ? "text-timeout" : "text-foreground"
                  )}
                >
                  {regressionValueText}
                </span>
                {regressionValueText !== "—" && !isNaN(Number(regressionValueText)) && (
                  <span className="text-xs text-muted-foreground font-normal">项性能退化</span>
                )}
              </div>
            </div>
            <div className="mt-3 pt-2.5 border-t border-border flex items-center gap-1.5 text-xs text-muted-foreground">
              {regressionStatusTone === "pass" ? (
                <span className="inline-flex items-center gap-1 text-pass">
                  <CheckCircle2 className="w-3.5 h-3.5" />
                  <span>{regressionSubText}</span>
                </span>
              ) : regressionStatusTone === "timeout" ? (
                <span className="inline-flex items-center gap-1 text-timeout">
                  <AlertCircle className="w-3.5 h-3.5" />
                  <span>{regressionSubText}</span>
                </span>
              ) : (
                <span>{regressionSubText}</span>
              )}
            </div>
          </div>

          {/* KPI 3: P95 响应时延 */}
          <div className="bg-surface border border-border rounded-xl p-3.5 flex flex-col justify-between shadow-xs hover:border-border-strong transition">
            <div>
              <div className="text-xs font-medium text-muted-foreground mb-2">P95 响应时延</div>
              <div className="flex items-baseline gap-1.5">
                <span className="text-2xl font-bold font-mono tracking-tight text-foreground">{p95Text}</span>
                {p95Text !== "—" && <span className="text-xs font-mono text-muted-foreground">ms</span>}
              </div>
            </div>
            <div className="mt-3 pt-2.5 border-t border-border flex items-center gap-1.5 text-xs text-muted-foreground">
              {isLatencyImproved === true ? (
                <span className="inline-flex items-center gap-1 text-primary font-medium">
                  <ArrowDown className="w-3.5 h-3.5" />
                  <span>{p95DeltaText}</span>
                </span>
              ) : isLatencyImproved === false ? (
                <span className="inline-flex items-center gap-1 text-fail font-medium">
                  <ArrowUp className="w-3.5 h-3.5" />
                  <span>{p95DeltaText}</span>
                </span>
              ) : (
                <span>{p95DeltaText || "暂无时延数据"}</span>
              )}
            </div>
          </div>

          {/* KPI 4: 单用例平均成本 */}
          <div className="bg-surface border border-border rounded-xl p-3.5 flex flex-col justify-between shadow-xs hover:border-border-strong transition">
            <div>
              <div className="text-xs font-medium text-muted-foreground mb-2">单用例平均成本</div>
              <div className="flex items-baseline gap-1">
                <span className="text-2xl font-bold font-mono tracking-tight text-foreground">{costText}</span>
              </div>
            </div>
            <div className="mt-3 pt-2.5 border-t border-border flex items-center gap-1.5 text-xs text-muted-foreground">
              <span>{costSubText}</span>
            </div>
          </div>
        </div>

      {/* 辅助说明段落 */}
      <div className="space-y-1 pt-1">
        <p
          id="quality-pass-rate-help"
          data-testid="quality-pass-rate-help"
          className="text-micro text-muted-foreground"
        >
          质量判定汇总说明：PASS / FAIL / UNKNOWN 分别统计三类质量结论，UNKNOWN 表示证据不足（必要指标缺失、失败、被跳过或无结果），既不是通过也不是不通过。
          「已判定通过率」的分母只含有明确结论的用例，因此必须与 UNKNOWN 数量一起阅读；「全部用例 PASS 占比」的分母包含全部用例，两者的差异即证据不足的规模。该比例不是执行成功率。
        </p>

        <p className="text-micro text-muted-foreground" data-testid="quality-all-cases-ratio">
          全部用例 PASS 占比：
          {itemsError || totalItems === null || totalItems === 0 ? (
            <span className="text-muted-foreground">尚未统计</span>
          ) : totalItems > 0 ? (
            <span className="font-mono">
              {passedItems} / {totalItems} ({(((passedItems ?? 0) / totalItems) * 100).toFixed(1)}%)
            </span>
          ) : (
            <span className="text-muted-foreground">—</span>
          )}
          <span className="ml-1">（分母为全部用例，含 UNKNOWN）</span>
        </p>
      </div>
    </section>
  );
};
