import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { EvaluationResultList } from "../../features/launches/EvaluationResultList";
import {
  formatResultValue,
  isAggregatable,
  resultErrorReason,
  resultStatusMeta,
  resultTypeLabel,
  type EvaluationResult,
} from "../../features/launches/evaluationResults";

const numericZero: EvaluationResult = {
  evaluator_id: "intent_match",
  result_type: "numeric",
  status: "succeeded",
  value: 0,
  normalized_value: 0,
  comment: "expected=refund; actual=other",
  duration_ms: 1.2,
  provenance: { binding_id: "bind_1", contract_status: "FROZEN_VERIFIED" },
};

const booleanFalse: EvaluationResult = {
  evaluator_id: "answer_present",
  result_type: "boolean",
  status: "succeeded",
  value: false,
  normalized_value: 0,
};

const categorical: EvaluationResult = {
  evaluator_id: "resolution_bucket",
  result_type: "categorical",
  status: "succeeded",
  value: "review",
  normalized_value: 1,
};

const text: EvaluationResult = {
  evaluator_id: "answer_excerpt",
  result_type: "text",
  status: "succeeded",
  value: "意图=refund；升级=False",
  normalized_value: null,
};

const failed: EvaluationResult = {
  evaluator_id: "raising_evaluator",
  result_type: "numeric",
  status: "failed",
  value: null,
  error_code: "EVALUATION_FAILED",
  error_message: "确定性测试 Provider：模拟评测执行异常",
};

const noResult: EvaluationResult = {
  evaluator_id: "missing_value_evaluator",
  result_type: "numeric",
  status: "no_result",
  value: null,
  error_code: "EVALUATION_VALUE_MISSING",
};

describe("Issue #82 typed evaluation results", () => {
  it("renders a real numeric zero as 0, not as a dash", () => {
    render(<EvaluationResultList results={[numericZero]} />);
    const badge = screen.getByTestId("typed-result-intent_match");
    expect(within(badge).getByText("0")).toBeInTheDocument();
    expect(within(badge).queryByText("—")).not.toBeInTheDocument();
  });

  it("keeps boolean false distinct from zero", () => {
    render(<EvaluationResultList results={[booleanFalse]} />);
    const badge = screen.getByTestId("typed-result-answer_present");
    expect(within(badge).getByText("false")).toBeInTheDocument();
    expect(within(badge).getByText(/布尔/)).toBeInTheDocument();
  });

  it("renders categorical and text values with their own type label", () => {
    render(<EvaluationResultList results={[categorical, text]} />);
    expect(within(screen.getByTestId("typed-result-resolution_bucket")).getByText("review")).toBeInTheDocument();
    expect(within(screen.getByTestId("typed-result-answer_excerpt")).getByText(/意图=refund/)).toBeInTheDocument();
    expect(screen.getByTestId("typed-result-answer_excerpt")).toHaveAttribute("data-result-type", "text");
  });

  it("shows a failure with its reason instead of a zero", () => {
    render(<EvaluationResultList results={[failed]} />);
    const badge = screen.getByTestId("typed-result-raising_evaluator");
    expect(within(badge).getByText("评测失败")).toBeInTheDocument();
    // The value renders as an em dash, never as 0.
    expect(within(badge).getByText("—")).toBeInTheDocument();
    expect(within(badge).queryByText("0")).not.toBeInTheDocument();
  });

  it("distinguishes no_result from failed and skipped", () => {
    render(<EvaluationResultList results={[noResult, failed]} />);
    expect(screen.getByTestId("typed-result-missing_value_evaluator")).toHaveAttribute("data-result-status", "no_result");
    expect(screen.getByTestId("typed-result-raising_evaluator")).toHaveAttribute("data-result-status", "failed");
    expect(resultStatusMeta("no_result").label).toBe("无结果");
    expect(resultStatusMeta("skipped").label).toBe("已跳过");
  });

  it("shows type, explanation, duration and provenance in the detail variant", () => {
    render(<EvaluationResultList results={[numericZero]} variant="detail" />);
    const card = screen.getByTestId("typed-result-intent_match");
    expect(within(card).getByText("数值")).toBeInTheDocument();
    expect(within(card).getByText("成功")).toBeInTheDocument();
    expect(within(card).getByText(/expected=refund/)).toBeInTheDocument();
    expect(within(card).getByText(/耗时 1.2 ms/)).toBeInTheDocument();
    expect(within(card).getByText(/bind_1/)).toBeInTheDocument();
  });

  it("never numericizes text, so it is excluded from aggregation", () => {
    expect(isAggregatable(text)).toBe(false);
    expect(isAggregatable(booleanFalse)).toBe(false);
    expect(isAggregatable(numericZero)).toBe(true);
    expect(formatResultValue(text)).toBe("意图=refund；升级=False");
  });

  it("explains a non-measurement using the structured reason", () => {
    expect(resultErrorReason(failed)).toContain("模拟评测执行异常");
    expect(resultErrorReason(noResult)).toContain("没有返回任何结果值");
    expect(resultErrorReason(numericZero)).toBeNull();
  });

  it("labels every supported result type", () => {
    expect(resultTypeLabel("numeric")).toBe("数值");
    expect(resultTypeLabel("boolean")).toBe("布尔");
    expect(resultTypeLabel("categorical")).toBe("分类");
    expect(resultTypeLabel("text")).toBe("文本");
  });
});
