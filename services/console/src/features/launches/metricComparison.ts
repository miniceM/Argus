/**
 * Pure helper for metric direction, formatting, and verdict resolution.
 * Strictly respects declared metadata; never guesses types or directions from metric names.
 */

export interface EvaluatorMetricMeta {
  id?: string;
  result_type?: string;
  direction?: "higher_is_better" | "lower_is_better" | string;
}

export interface MetricDirectionResult {
  isNumeric: boolean;
  hasKnownDirection: boolean;
  isLowerBetter: boolean;
}

export function resolveMetricDirection(
  evalMeta?: EvaluatorMetricMeta,
  rule?: { result_type?: string },
): MetricDirectionResult {
  // Only declared contract specifies if it is numeric
  const isNumeric = rule?.result_type === "numeric" || evalMeta?.result_type === "numeric";

  let hasKnownDirection = false;
  let isLowerBetter = false;

  if (evalMeta?.direction === "higher_is_better") {
    hasKnownDirection = true;
    isLowerBetter = false;
  } else if (evalMeta?.direction === "lower_is_better") {
    hasKnownDirection = true;
    isLowerBetter = true;
  }

  return { isNumeric, hasKnownDirection, isLowerBetter };
}

export interface MetricStatusResult {
  statusLabel: string;
  statusTone: "pass" | "fail" | "neutral";
}

export function evaluateMetricChange(
  baselineScore: number | null | undefined,
  candidateScore: number | null | undefined,
  direction: MetricDirectionResult,
): MetricStatusResult {
  if (baselineScore == null || candidateScore == null) {
    return { statusLabel: "—", statusTone: "neutral" };
  }

  if (candidateScore === baselineScore) {
    return { statusLabel: "持平", statusTone: "pass" };
  }

  if (!direction.hasKnownDirection) {
    return { statusLabel: "变化", statusTone: "neutral" };
  }

  const isImproved = direction.isLowerBetter
    ? candidateScore < baselineScore
    : candidateScore > baselineScore;

  if (isImproved) {
    return { statusLabel: "提升", statusTone: "pass" };
  } else {
    return {
      statusLabel: direction.isLowerBetter ? "退化" : "下降",
      statusTone: "fail",
    };
  }
}
