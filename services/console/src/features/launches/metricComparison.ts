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

export interface FrozenMetricRule {
  operator?: string | null;
  threshold?: number | null;
  expected_value?: unknown;
}

/**
 * Resolve whether a candidate measurement satisfies the frozen policy rule.
 * Returns null when there is no executable rule or the value/rule is not comparable;
 * callers may then show a neutral trend diagnostic without inventing a gate result.
 */
export function evaluateMetricRule(
  candidateScore: number | null | undefined,
  rule?: FrozenMetricRule | null,
): MetricStatusResult | null {
  if (candidateScore == null || !Number.isFinite(candidateScore) || !rule?.operator) {
    return null;
  }

  let met: boolean | null = null;
  switch (rule.operator) {
    case ">=":
      if (typeof rule.threshold === "number" && Number.isFinite(rule.threshold)) met = candidateScore >= rule.threshold;
      break;
    case "<=":
      if (typeof rule.threshold === "number" && Number.isFinite(rule.threshold)) met = candidateScore <= rule.threshold;
      break;
    case ">":
      if (typeof rule.threshold === "number" && Number.isFinite(rule.threshold)) met = candidateScore > rule.threshold;
      break;
    case "<":
      if (typeof rule.threshold === "number" && Number.isFinite(rule.threshold)) met = candidateScore < rule.threshold;
      break;
    case "==":
      // Strict equality matches the backend rule evaluator; do not coerce booleans or strings.
      if (typeof rule.expected_value === "number" && Number.isFinite(rule.expected_value)) {
        met = candidateScore === rule.expected_value;
      }
      break;
    default:
      break;
  }

  if (met == null) return null;
  return {
    statusLabel: met ? "达标" : "未达标",
    statusTone: met ? "pass" : "fail",
  };
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
