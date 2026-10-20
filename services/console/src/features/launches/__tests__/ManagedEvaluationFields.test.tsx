import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { ManagedEvaluationFields, newManagedDraft } from "../ManagedEvaluationFields";

function Editor() {
  const [value, setValue] = useState(newManagedDraft);
  return <><ManagedEvaluationFields value={value} onChange={setValue} /><output data-testid="draft">{JSON.stringify(value)}</output></>;
}
const draft = () => JSON.parse(screen.getByTestId("draft").textContent!);
afterEach(cleanup);

describe("Managed Score 与既有 QualityPolicy 对齐", () => {
  it("类型切换不会保留旧类型阈值，text 仅为可选证据", () => {
    render(<Editor />);
    fireEvent.change(screen.getByLabelText("Score 名称 1"), { target: { value: "accuracy" } });
    fireEvent.change(screen.getByLabelText("Score 类型 1"), { target: { value: "numeric" } });
    fireEvent.change(screen.getByLabelText("阈值 1"), { target: { value: "0.8" } });
    expect(draft().rules).toMatchObject([{ evaluator_id: "accuracy", operator: ">=", threshold: 0.8 }]);
    expect(draft().rules[0]).not.toHaveProperty("expected_value");
    fireEvent.change(screen.getByLabelText("Score 类型 1"), { target: { value: "text" } });
    expect(screen.getByRole("checkbox")).toBeDisabled();
    expect(draft().contract.scores[0].required).toBe(false);
    expect(draft().rules).toEqual([]);
  });
  it("配置分类与 required；删除 Score 同时移除对应规则", () => {
    render(<Editor />);
    fireEvent.change(screen.getByLabelText("Score 名称 1"), { target: { value: "category" } });
    fireEvent.change(screen.getByLabelText("Score 类型 1"), { target: { value: "categorical" } });
    fireEvent.change(screen.getByLabelText("分类取值 1"), { target: { value: "safe,unsafe" } });
    fireEvent.change(screen.getByLabelText("期望值 1"), { target: { value: "safe" } });
    fireEvent.click(screen.getByRole("checkbox"));
    expect(draft().rules[0]).toMatchObject({ result_type: "categorical", expected_value: "safe", required: false });
    expect(draft().contract.scores[0].category_values).toEqual(["safe", "unsafe"]);
    fireEvent.click(screen.getByRole("button", { name: "添加外部 Score" }));
    fireEvent.click(screen.getByRole("button", { name: "移除 Score 1" }));
    expect(draft().contract.scores).toHaveLength(1);
    expect(draft().rules).not.toContainEqual(expect.objectContaining({ evaluator_id: "category" }));
  });
  it("临时重名后修正或删除，不串用另一项 Score 的规则", () => {
    render(<Editor />);
    fireEvent.change(screen.getByLabelText("Score 名称 1"), { target: { value: "first" } });
    fireEvent.click(screen.getByRole("button", { name: "添加外部 Score" }));
    fireEvent.change(screen.getByLabelText("Score 名称 2"), { target: { value: "second" } });
    fireEvent.change(screen.getByLabelText("Score 类型 2"), { target: { value: "numeric" } });
    fireEvent.change(screen.getByLabelText("阈值 2"), { target: { value: "0.7" } });
    fireEvent.change(screen.getByLabelText("Score 名称 2"), { target: { value: "first" } });
    fireEvent.change(screen.getByLabelText("Score 名称 2"), { target: { value: "corrected" } });
    expect(draft().rules[1]).toMatchObject({ evaluator_id: "corrected", result_type: "numeric", threshold: 0.7 });
    fireEvent.change(screen.getByLabelText("Score 名称 2"), { target: { value: "first" } });
    fireEvent.click(screen.getByRole("button", { name: "移除 Score 2" }));
    expect(draft().rules).toMatchObject([{ evaluator_id: "first", result_type: "boolean", expected_value: true }]);
  });
  it("没有隐藏网络访问、Rule 创建或远程执行入口", () => {
    const onChange = vi.fn();
    render(<ManagedEvaluationFields value={newManagedDraft()} onChange={onChange} />);
    expect(screen.getByText(/远程执行实现并未冻结/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /执行|创建 Rule|编辑 Prompt/ })).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });
});
