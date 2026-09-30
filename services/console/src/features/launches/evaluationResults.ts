/**
 * Issue #82 — typed evaluation results in the Console.
 *
 * A measurement is only meaningful together with its type and its status. The
 * Console therefore never infers a number: a real `0` renders as `0`, while a
 * missing value, a failed Evaluator and a skipped metric each render with their
 * own status and reason instead of being flattened to zero.
 */

import type { BadgeTone } from "../../components/Badge";

export type EvaluationResult = {
  evaluator_id: string;
  evaluator_version?: string | null;
  result_type: string;
  status: string;
  value?: unknown;
  normalized_value?: number | null;
  comment?: string | null;
  evidence?: Record<string, unknown> | null;
  duration_ms?: number | null;
  error_code?: string | null;
  error_message?: string | null;
  provenance?: {
    binding_id?: string | null;
    definition_digest?: string | null;
    manifest_schema_version?: string | null;
    contract_status?: string | null;
  } | null;
};

export type ResultTone = BadgeTone;

export const RESULT_TYPE_LABELS: Record<string, string> = {
  boolean: "布尔",
  numeric: "数值",
  categorical: "分类",
  text: "文本",
  unknown: "未知",
};

/**
 * Status → label + tone. Every status carries a text label, so a result is
 * never distinguished by colour alone (SC 1.4.1).
 */
export const RESULT_STATUS_META: Record<string, { label: string; tone: ResultTone }> = {
  succeeded: { label: "成功", tone: "pass" },
  failed: { label: "评测失败", tone: "fail" },
  skipped: { label: "已跳过", tone: "neutral" },
  no_result: { label: "无结果", tone: "timeout" },
};

/**
 * Structured failure reasons, mirroring the backend's RESULT_ERROR_CODES so the
 * Console can explain a non-measurement without inventing its own wording.
 */
export const RESULT_ERROR_LABELS: Record<string, string> = {
  EVALUATION_VALUE_MISSING: "评测没有返回任何结果值。",
  EVALUATION_VALUE_NOT_FINITE: "评测返回了非有限数值（NaN/Infinity），不能作为测量。",
  EVALUATION_TYPE_MISMATCH: "评测返回的类型与冻结契约声明的类型不一致。",
  EVALUATION_CATEGORY_NOT_ALLOWED: "评测返回的分类取值不在冻结契约的枚举范围内。",
  EVALUATION_BOOLEAN_COERCED: "布尔结果不会被隐式当作数字使用。",
  EVALUATION_FAILED: "评测执行失败。",
  EVALUATION_SKIPPED: "该指标本次未评测。",
  EVALUATION_PROVENANCE_UNKNOWN: "历史记录缺少新的冻结证据来源，仅按旧 numeric 兼容读取。",
};

export function resultTypeLabel(resultType: string | undefined): string {
  return RESULT_TYPE_LABELS[resultType ?? ""] ?? "未知";
}

export function resultStatusMeta(status: string | undefined): {
  label: string;
  tone: ResultTone;
} {
  return (
    RESULT_STATUS_META[status ?? ""] ?? { label: "未知状态", tone: "neutral" }
  );
}

export function resultErrorReason(result: EvaluationResult): string | null {
  if (result.status === "succeeded") return null;
  if (result.error_message) return result.error_message;
  if (result.error_code) return RESULT_ERROR_LABELS[result.error_code] ?? result.error_code;
  return resultStatusMeta(result.status).label;
}

/** Only a succeeded numeric result carries an aggregatable measurement. */
export function isAggregatable(result: EvaluationResult): boolean {
  return result.status === "succeeded" && result.result_type === "numeric";
}

/**
 * Render the typed value without coercion:
 * - a real `0` stays `0` (never blanked, never turned into "-"),
 * - `false` stays `false` (not 0),
 * - text and categories keep their original string.
 * A result with no value renders an em dash together with its status label, so
 * "no measurement" is never confused with "measured zero".
 */
export function formatResultValue(result: EvaluationResult): string {
  if (result.status !== "succeeded" || result.value === null || result.value === undefined) {
    return "—";
  }
  const { value } = result;
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
  }
  if (typeof value === "string") {
    return value.length > 120 ? `${value.slice(0, 120)}…` : value;
  }
  return String(value);
}

/** True when the value is a genuine numeric measurement (for tone colouring). */
export function isNumericValue(result: EvaluationResult): boolean {
  return result.result_type === "numeric" && typeof result.value === "number";
}
