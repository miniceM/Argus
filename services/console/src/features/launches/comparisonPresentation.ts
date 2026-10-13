/**
 * Shared presentation rules for the formal comparison (issue 119 U2, PR 134 review gap F2).
 *
 * A formal relative verdict and a formal regression count are two claims that rest on
 * two *independent* server eligibility fields:
 *
 *   - `comparability.comparable` — the two sides are measured under the same contract;
 *   - `formal.available` — the backend assembled the formal evidence.
 *
 * The KPI card, the comparison tab summary and the formal verdict block used to
 * check different subsets of those fields, so a payload that answered only one of
 * them could still render a formal count (or even a formal "no regression" zero).
 * Every consumer now reads the same rules from here.
 *
 * Rules, applied verbatim:
 *   - a missing / null / non-boolean-true field is *not* eligibility — never
 *     `Boolean(x)` and never `?? true`;
 *   - only a real, non-negative integer is a count;
 *   - a missing or malformed count stays unknown instead of becoming a green zero.
 *
 * This module is presentation-only: it never changes a server conclusion, never
 * invents a count and never relaxes baseline eligibility.
 */

type GeneratedComparison = import("../../api/schema").components["schemas"]["ComparisonResponse"];
type GeneratedComparability = GeneratedComparison["comparability"];
type GeneratedFormalVerdict = GeneratedComparison["formal"];

export type RegressionCountState =
  | { kind: "value"; value: number }
  | { kind: "missing" }
  | { kind: "invalid" };

/**
 * Reads the formal REGRESSION count with unknown-safe semantics.
 *
 * Only a real, non-negative integer is a count. A missing key, null, a numeric
 * string, a negative value, a fraction or a non-finite value is *unknown*: it
 * must never be rendered as a green "no regression" zero, because the backend
 * contract only types `classification_counts` as a dictionary.
 */
export function readRegressionCount(
  classificationCounts: Record<string, unknown> | null | undefined,
): RegressionCountState {
  if (classificationCounts == null) return { kind: "missing" };
  if (!("REGRESSION" in classificationCounts)) return { kind: "missing" };
  const raw = classificationCounts.REGRESSION;
  if (raw == null) return { kind: "missing" };
  if (typeof raw !== "number") return { kind: "invalid" };
  // Number.isInteger rejects NaN and the infinities as well.
  if (!Number.isInteger(raw) || raw < 0) return { kind: "invalid" };
  return { kind: "value", value: raw };
}

/**
 * Server eligibility fields, as returned by the comparison read. Both are optional
 * at the call site so a caller can pass a partial payload; the generated DTO itself
 * types them as required, which is why a runtime absence is a contract violation
 * rather than a legal "not comparable" answer.
 */
export type FormalEligibility = {
  comparability?: Pick<GeneratedComparability, "comparable"> | null;
  formal?: Pick<GeneratedFormalVerdict, "available" | "verdict"> | null;
};

/**
 * A formal relative verdict may only be surfaced when the backend declared the
 * comparison comparable *and* the formal verdict available. Any other combination
 * is unknown and must be withheld from both the KPI card and the summary.
 */
export function isFormalComparisonUsable(eligibility: FormalEligibility): boolean {
  return eligibility.comparability?.comparable === true && eligibility.formal?.available === true;
}

export type FormalComparisonCountState = {
  /** Whether a formal relative verdict may be shown at all. */
  usable: boolean;
  /** Whether the formal regression count may be shown next to that verdict. */
  countUsable: boolean;
  count: RegressionCountState;
};

/**
 * Combined read shared by every consumer that shows both the verdict and the count.
 * A legal count still needs `usable`: a legal REGRESSION value on a payload whose
 * eligibility fields were not both true is not a formal regression count.
 */
export function readFormalComparisonCount(
  input: FormalEligibility & {
    classificationCounts?: Record<string, unknown> | null;
  },
): FormalComparisonCountState {
  const usable = isFormalComparisonUsable(input);
  const count = readRegressionCount(input.classificationCounts);
  return { usable, countUsable: usable && count.kind === "value", count };
}

export type FormalVerdictTone = "pass" | "fail" | "timeout" | "muted";

export type FormalVerdictPresentation = {
  /** Whether the server's verdict and count may be surfaced. */
  usable: boolean;
  /** The raw server verdict, or null when it is absent or withheld. */
  verdict: string | null;
  /** Short label for the "Baseline 相对变化" line. */
  label: string;
  /** Semantic tone key; consumers map it to their own design tokens. */
  tone: FormalVerdictTone;
  /** One-sentence explanation that always states what is (not) being claimed. */
  detail: string;
};

/**
 * One place that turns the server verdict into a label, a tone and an explanation.
 *
 * The explanation is deliberately explicit about scope: a relative change is never
 * a release-gate result, and a withheld verdict never reads as "no regression".
 */
export function readFormalVerdictPresentation(
  input: FormalEligibility & {
    classificationCounts?: Record<string, unknown> | null;
  },
): FormalVerdictPresentation {
  const { usable, countUsable, count } = readFormalComparisonCount(input);
  const rawVerdict = input.formal?.verdict;
  const verdict = typeof rawVerdict === "string" && rawVerdict.trim().length > 0 ? rawVerdict : null;

  if (!usable) {
    const comparabilityProvided = input.comparability != null;
    const detail = !comparabilityProvided
      ? "比较资格证据未返回，暂无法给出相对变化结论。"
      : input.comparability?.comparable === true
        ? "由于比较证据不足或 Baseline 未绑定，暂无法给出相对变化结论。"
        : "比较前提不满足（契约或测量版本不可比），暂无法给出相对变化结论。";
    return { usable, verdict, label: "暂不可判定", tone: "timeout", detail };
  }

  if (verdict === "REGRESSION") {
    return {
      usable,
      verdict,
      label: "存在回归",
      tone: "fail",
      detail: countUsable && count.kind === "value"
        ? `正式比较发现 ${count.value} 个回归 Case；此结论只描述相对变化。`
        : "正式比较判定存在回归，但回归计数未返回或不可用；此结论只描述相对变化。",
    };
  }
  if (verdict === "IMPROVEMENT") {
    return {
      usable,
      verdict,
      label: "有所改善",
      tone: "pass",
      detail: "正式比较显示相对基线有所改善；Candidate 是否达到门禁仍以其冻结质量结论为准。",
    };
  }
  if (verdict === "UNCHANGED") {
    return {
      usable,
      verdict,
      label: "无变化",
      tone: "muted",
      detail: "正式比较显示相对基线无变化；未进一步退化不等于 Candidate 达到发布门禁。",
    };
  }
  return {
    usable,
    verdict,
    label: "暂不可判定",
    tone: "timeout",
    detail: "正式比较 verdict 缺失或未知，暂不能说明相对变化。",
  };
}
