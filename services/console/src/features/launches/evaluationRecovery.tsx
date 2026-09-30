import React from "react";
import { Badge, type BadgeTone } from "../../components/Badge";

/**
 * Issue #84 — evaluation-only recovery.
 *
 * An Agent that already answered is never called again. The UI states the
 * three facts a reviewer needs, kept deliberately separate:
 *   1. execution succeeded (the Agent answered),
 *   2. the evaluation failed / is being re-judged / was recovered,
 *   3. the quality verdict is PASS / FAIL / UNKNOWN.
 *
 * `UNKNOWN` is an evidence statement, not a product failure, so the copy never
 * presents a re-judgement as a retry of the business call.
 */

export type EvaluationStatus = "none" | "evaluating" | "recovered" | "failed" | string;

const toneFor = (status: string): BadgeTone => {
  switch ((status || "").toLowerCase()) {
    case "evaluating":
      return "running";
    case "recovered":
      return "pass";
    case "failed":
      return "fail";
    default:
      return "neutral";
  }
};

const labelFor = (status: string): string => {
  switch ((status || "").toLowerCase()) {
    case "evaluating":
      return "重评中";
    case "recovered":
      return "已恢复";
    case "failed":
      return "重评失败";
    default:
      return "未重评";
  }
};

export const EvaluationRecoveryBadge: React.FC<{ status?: string | null; className?: string }> = ({
  status,
  className = "",
}) => {
  const norm = (status || "none").toLowerCase();
  if (norm === "none" || norm === "") return null;
  return (
    <Badge tone={toneFor(norm)} className={className} data-testid="evaluation-recovery-badge" data-tone={toneFor(norm)}>
      {labelFor(norm)}
    </Badge>
  );
};

const REUSE_MESSAGE = "复用原 Agent 输出，不会再次调用 Agent。";

/** Short, honest explanation of what a re-judgement reuses. */
export const recoveryExplanation = (item: {
  evaluation_status?: string | null;
  evaluation_recoverable?: boolean | null;
  evaluation_error?: string | null;
}): string => {
  const err = item.evaluation_error?.trim();
  if (err) return err;
  const status = (item.evaluation_status || "none").toLowerCase();
  if (status === "evaluating") return `正在仅重试评测：${REUSE_MESSAGE}`;
  if (item.evaluation_recoverable) return `可仅重试评测：${REUSE_MESSAGE}`;
  return "";
};
