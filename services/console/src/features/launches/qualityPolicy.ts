/**
 * Issue #83 — client-side model of the frozen quality policy.
 *
 * The rules the user confirms here are submitted verbatim and re-validated by
 * the server at Launch creation, so an illegal rule can never be frozen. This
 * module mirrors the server contract (services/eval-runner/app/quality_policy.py)
 * closely enough to block the obvious mistakes before the request is sent.
 */

type VersionInfo = import("../../api/schema").components["schemas"]["EvaluatorVersionInfo"];
type QualityRuleRequest = import("../../api/schema").components["schemas"]["QualityRuleRequest"];

export type QualityOperator = ">=" | "<=" | "==";

export interface RuleDraft {
  /** 参与判定（必要规则）；关闭后该指标只作为可选诊断证据。 */
  required: boolean;
  /** 空字符串表示"仅作为证据"，不参与判定。 */
  operator: QualityOperator | "";
  /** numeric 使用阈值原文，boolean / categorical 使用期望取值原文。 */
  value: string;
  /** 关键规则：在明细中高亮提示。 */
  critical: boolean;
  /** result_type 为 text 的指标永远只能作为证据。 */
  resultType: string;
  note: string;
}

export interface RuleIssue {
  evaluatorId: string;
  message: string;
}

export const isEvidenceOnly = (resultType: string): boolean => resultType === "text";

export const allowedOperators = (resultType: string): QualityOperator[] => {
  if (resultType === "numeric") return [">=", "<="];
  if (resultType === "boolean" || resultType === "categorical") return ["=="];
  return [];
};

/** The rule a metric starts from: its own frozen direction and threshold. */
export const defaultRuleDraft = (version: VersionInfo): RuleDraft => {
  if (isEvidenceOnly(version.result_type)) {
    return {
      required: false,
      operator: "",
      value: "",
      critical: false,
      resultType: version.result_type,
      note: "",
    };
  }
  if (version.result_type === "numeric") {
    return {
      required: true,
      operator: version.direction === "lower_is_better" ? "<=" : ">=",
      value: String(version.threshold),
      critical: Boolean(version.critical),
      resultType: "numeric",
      note: "",
    };
  }
  return {
    required: true,
    operator: "==",
    value: "",
    critical: Boolean(version.critical),
    resultType: version.result_type,
    note: "",
  };
};

const parseNumeric = (raw: string): number | null => {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
};

const parseTypedValue = (resultType: string, raw: string): unknown | null => {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  if (resultType === "boolean") {
    if (trimmed === "true") return true;
    if (trimmed === "false") return false;
    return undefined; // present but not a boolean
  }
  return trimmed;
};

/**
 * Validate one draft. Returns a message when the rule cannot be frozen, so the
 * form can block submission before the request is sent.
 */
export const validateRuleDraft = (
  draft: RuleDraft,
  version: VersionInfo | undefined,
): string | null => {
  if (isEvidenceOnly(draft.resultType)) {
    // text is listed as evidence only; it can never decide quality.
    return null;
  }
  if (draft.operator === "") {
    if (draft.required) return "必要规则必须选择一个比较运算符，或取消勾选「参与判定」改为仅记录证据。";
    return null;
  }
  if (!allowedOperators(draft.resultType).includes(draft.operator)) {
    return `${draft.resultType} 类型不支持运算符 ${draft.operator}。`;
  }
  if (draft.resultType === "numeric") {
    const parsed = parseNumeric(draft.value);
    if (parsed === null) return "数值规则必须填写一个有限数值阈值。";
    return null;
  }
  const parsed = parseTypedValue(draft.resultType, draft.value);
  if (parsed === null) {
    return draft.resultType === "boolean"
      ? "布尔规则必须显式选择期望取值 true 或 false。"
      : "分类规则必须显式填写期望取值。";
  }
  if (parsed === undefined) {
    return "布尔规则的期望取值必须是 true 或 false，不会把 1 / \"true\" 之类的取值当作 true。";
  }
  if (draft.resultType === "categorical") {
    const allowed = version?.category_values ?? [];
    if (allowed.length > 0 && !allowed.includes(String(parsed))) {
      return `期望取值「${String(parsed)}」不在该指标的分类枚举内（${allowed.join("、")}）。`;
    }
  }
  return null;
};

/** Validate the whole rule set, including the "at least one required rule" gate. */
export const validateRuleDrafts = (
  drafts: Record<string, RuleDraft>,
  versionsById: Record<string, VersionInfo | undefined>,
): RuleIssue[] => {
  const issues: RuleIssue[] = [];
  Object.entries(drafts).forEach(([evaluatorId, draft]) => {
    const message = validateRuleDraft(draft, versionsById[evaluatorId]);
    if (message) issues.push({ evaluatorId, message });
  });
  if (!Object.values(drafts).some((draft) => draft.required && !isEvidenceOnly(draft.resultType))) {
    issues.push({
      evaluatorId: "",
      message: "质量策略至少需要一条参与判定的必要规则，否则无法得出质量结论。",
    });
  }
  return issues;
};

/** Convert drafts into the create-Launch request body. */
export const toQualityPolicyRequest = (
  drafts: Record<string, RuleDraft>,
): { rules: QualityRuleRequest[] } => {
  const rules: QualityRuleRequest[] = Object.entries(drafts).map(([evaluatorId, draft]) => {
    const rule: QualityRuleRequest = {
      evaluator_id: evaluatorId,
      operator: draft.operator === "" ? null : draft.operator,
      threshold: null,
      expected_value: null,
      result_type: draft.resultType,
      required: draft.required,
      critical: draft.critical,
      note: draft.note.trim() || null,
    };
    if (draft.resultType === "numeric") {
      rule.threshold = parseNumeric(draft.value);
    } else if (draft.operator !== "") {
      rule.expected_value = parseTypedValue(draft.resultType, draft.value);
    }
    return rule;
  });
  return { rules };
};

/** Plain-language summary used in the confirmation panel. */
export const describeRuleDraft = (draft: RuleDraft): string => {
  if (isEvidenceOnly(draft.resultType)) return "仅作为证据记录，不参与判定";
  if (draft.operator === "") return "仅作为证据记录，不参与判定";
  if (draft.resultType === "numeric") return `数值 ${draft.operator} ${draft.value.trim() || "—"}`;
  return `显式匹配 == ${draft.value.trim() || "—"}`;
};
