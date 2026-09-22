import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { FieldHelp } from "../FieldHelp";

describe("FieldHelp Component", () => {
  const defaultProps = {
    title: "版本号 (Tag)",
    meaning: "定义该 Agent 规格快照的唯一版本标识。",
    rules: "必填，同一 Agent 下版本号不可重复。",
    example: "1.0.0 或 v2",
  };

  const originalInnerWidth = window.innerWidth;

  beforeEach(() => {
    window.innerWidth = 1024;
  });

  afterEach(() => {
    window.innerWidth = originalInnerWidth;
  });

  it("renders trigger button and initially hides help card", () => {
    render(<FieldHelp {...defaultProps} />);
    const trigger = screen.getByRole("button", { name: "查看「版本号 (Tag)」说明" });
    expect(trigger).toBeInTheDocument();
    expect(screen.queryByText("定义该 Agent 规格快照的唯一版本标识。")).not.toBeInTheDocument();
  });

  it("toggles help card open and closed on trigger button click", () => {
    render(<FieldHelp {...defaultProps} />);
    const trigger = screen.getByRole("button", { name: "查看「版本号 (Tag)」说明" });

    // Open
    fireEvent.click(trigger);
    expect(screen.getByText("定义该 Agent 规格快照的唯一版本标识。")).toBeInTheDocument();
    expect(screen.getByText("必填，同一 Agent 下版本号不可重复。")).toBeInTheDocument();
    expect(screen.getByText("1.0.0 或 v2")).toBeInTheDocument();

    // Close on second click
    fireEvent.click(trigger);
    expect(screen.queryByText("定义该 Agent 规格快照的唯一版本标识。")).not.toBeInTheDocument();
  });

  it("closes help card when clicking close button inside popover", () => {
    render(<FieldHelp {...defaultProps} />);
    const trigger = screen.getByRole("button", { name: "查看「版本号 (Tag)」说明" });
    fireEvent.click(trigger);
    expect(screen.getByText("定义该 Agent 规格快照的唯一版本标识。")).toBeInTheDocument();

    const closeBtn = screen.getByRole("button", { name: "关闭说明" });
    fireEvent.click(closeBtn);
    expect(screen.queryByText("定义该 Agent 规格快照的唯一版本标识。")).not.toBeInTheDocument();
  });

  it("closes help card when pressing Escape key", () => {
    render(<FieldHelp {...defaultProps} />);
    const trigger = screen.getByRole("button", { name: "查看「版本号 (Tag)」说明" });
    fireEvent.click(trigger);
    expect(screen.getByText("定义该 Agent 规格快照的唯一版本标识。")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByText("定义该 Agent 规格快照的唯一版本标识。")).not.toBeInTheDocument();
  });

  it("closes help card when clicking outside", () => {
    render(
      <div>
        <div data-testid="outside-element">外部区域</div>
        <FieldHelp {...defaultProps} />
      </div>
    );
    const trigger = screen.getByRole("button", { name: "查看「版本号 (Tag)」说明" });
    fireEvent.click(trigger);
    expect(screen.getByText("定义该 Agent 规格快照的唯一版本标识。")).toBeInTheDocument();

    const outside = screen.getByTestId("outside-element");
    fireEvent.mouseDown(outside);
    expect(screen.queryByText("定义该 Agent 规格快照的唯一版本标识。")).not.toBeInTheDocument();
  });

  it("renders mobile-friendly sheet via portal on viewports below sm breakpoint", () => {
    // Simulate mobile viewport (< 640px)
    window.innerWidth = 375;
    render(<FieldHelp {...defaultProps} placement="bottom-right" />);

    const trigger = screen.getByRole("button", { name: "查看「版本号 (Tag)」说明" });
    fireEvent.click(trigger);

    // Dialog should be present and have aria-modal on mobile
    const dialog = screen.getByRole("dialog");
    expect(dialog).toBeInTheDocument();
    expect(dialog).toHaveAttribute("aria-modal", "true");

    // Content is readable and rendered into document.body portal
    expect(document.body.contains(dialog)).toBe(true);
    expect(screen.getByText("定义该 Agent 规格快照的唯一版本标识。")).toBeInTheDocument();
  });
});
