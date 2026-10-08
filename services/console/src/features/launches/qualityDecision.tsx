/**
 * Issue #83 — per-rule quality explanations.
 *
 * A single PASS / FAIL / UNKNOWN badge cannot answer "why?". This panel shows
 * every frozen rule with its expected condition, the observed value and a
 * plain-language reason, so "证据不足 UNKNOWN" is visibly different from a
 * real rule violation "不通过 FAIL".
 */

import React from "react";
import { Badge } from "../../components/Badge";
import { QualityBadge } from "../../components/QualityBadge";

export type QualityRuleEvaluation = {
  evaluator_id: string;
  result_type?: string | null;
  required: boolean;
  critical: boolean;
  operator?: string | null;
  expected?: unknown;
  observed_value?: unknown;
  observed_status?: string | null;
  conclusion: string;
  reason_code?: string | null;
  explanation?: string | null;
};

export type QualityEvaluation = {
  conclusion: string;
  policy_id?: string | null;
  policy_version?: string | null;
  policy_digest?: string | null;
  decided_by?: string | null;
  releasable: boolean;
  unknown_reasons?: string[];
  rules?: QualityRuleEvaluation[];
};

/** Reason codes the server emits, plus a fallback for evaluator error codes. */
const REASON_LABELS: Record<string, string> = {
  QUALITY_EVIDENCE_MISSING: "缺少结果",
  QUALITY_EVIDENCE_FAILED: "评测执行失败",
  QUALITY_EVIDENCE_SKIPPED: "本次未评测",
  QUALITY_EVIDENCE_NO_RESULT: "评测无结果",
  QUALITY_EVIDENCE_VALUELESS: "结果值缺失",
  QUALITY_EVIDENCE_MISSING_STATUS: "结果状态缺失",
  QUALITY_RULE_VIOLATED: "违反判定规则",
};

const reasonLabel = (code: string | null | undefined): string | null => {
  if (!code) return null;
  return REASON_LABELS[code] ?? `评测错误 ${code}`;
};

const renderValue = (value: unknown): string => {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return value === "" ? "—" : value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
};

const expectedText = (rule: QualityRuleEvaluation): string => {
  if (rule.operator === ">=" || rule.operator === "<=") {
    return `${rule.operator} ${renderValue(rule.expected)}`;
  }
  if (rule.operator === "==") return `== ${renderValue(rule.expected)}`;
  return "仅作为证据";
};

const observedText = (rule: QualityRuleEvaluation): string => {
  if (rule.observed_status && rule.observed_status !== "succeeded") {
    return `${renderValue(rule.observed_value)}（${rule.observed_status}）`;
  }
  return renderValue(rule.observed_value);
};

const conclusionTone = (conclusion: string): string => {
  const normalized = (conclusion || "unknown").toLowerCase();
  if (normalized === "pass") return "text-pass";
  if (normalized === "fail") return "text-fail";
  return "text-timeout";
};

export const QualityRulePanel: React.FC<{
  evaluation?: QualityEvaluation | null;
  /** Compact mode is used inside the item table rows. */
  compact?: boolean;
}> = ({ evaluation, compact = false }) => {
  if (!evaluation) return null;

  const rules = evaluation.rules ?? [];
  if (rules.length === 0) {
    return (
      <p className="text-micro text-muted-foreground" data-testid="quality-rules-empty">
        该用例没有逐条判定记录（可能来自历史契约）。
      </p>
    );
  }

  return (
    <div className={compact ? "space-y-1.5" : "space-y-2.5"} data-testid="quality-rule-panel">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold text-foreground">逐条判定依据</span>
        <QualityBadge quality={evaluation.conclusion} />
        {evaluation.policy_id && (
          <span className="font-mono text-micro text-muted-foreground break-all">
            {evaluation.policy_id}
            {evaluation.policy_version ? `@${evaluation.policy_version}` : ""}
          </span>
        )}
        {evaluation.policy_digest && (
          <span className="font-mono text-micro text-muted-foreground break-all" title="策略摘要">
            {evaluation.policy_digest.slice(0, 19)}…
          </span>
        )}
        {evaluation.decided_by && (
          <span className="text-micro text-muted-foreground">判定来源 {evaluation.decided_by}</span>
        )}
      </div>

      <ul className={compact ? "space-y-1" : "space-y-1.5"}>
        {rules.map((rule) => {
          const label = reasonLabel(rule.reason_code);
          return (
            <li
              key={`${rule.evaluator_id}-${rule.operator ?? "evidence"}`}
              data-testid={`quality-rule-result-${rule.evaluator_id}`}
              data-conclusion={rule.conclusion?.toLowerCase()}
              className={`rounded border px-2 py-1.5 ${
                rule.conclusion?.toLowerCase() === "fail"
                  ? "border-fail-border bg-fail-subtle/40"
                  : rule.conclusion?.toLowerCase() === "unknown"
                    ? "border-timeout-border bg-timeout-subtle/40"
                    : "border-border bg-surface-muted/50"
              }`}
            >
              <div className="flex flex-wrap items-center gap-2 text-micro">
                <span className="font-mono font-semibold text-foreground">{rule.evaluator_id}</span>
                <span className="font-mono text-muted-foreground">条件 {expectedText(rule)}</span>
                <span className="font-mono text-muted-foreground">实测 {observedText(rule)}</span>
                <span className={`font-mono font-bold uppercase ${conclusionTone(rule.conclusion)}`}>
                  {rule.conclusion?.toLowerCase()}
                </span>
                <Badge tone={rule.required ? "pass" : "neutral"}>
                  {rule.required ? "必要" : "可选诊断"}
                </Badge>
                {rule.critical && <Badge tone="timeout">关键</Badge>}
                {label && <span className="text-muted-foreground">原因：{label}</span>}
              </div>
              {rule.explanation && (
                <p className="text-micro text-foreground-secondary mt-0.5 leading-relaxed">
                  {rule.explanation}
                </p>
              )}
            </li>
          );
        })}
      </ul>

      {!evaluation.releasable && evaluation.unknown_reasons && evaluation.unknown_reasons.length > 0 && (
        <p className="text-micro text-timeout-strong" data-testid="quality-unknown-reasons">
          证据不足，不可用于发布门禁：
          {evaluation.unknown_reasons.join("；")}
        </p>
      )}
    </div>
  );
};
