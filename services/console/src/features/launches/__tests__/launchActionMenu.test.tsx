/**
 * Issue #119 main-gap fix — U4: accessible compact launch actions (narrow).
 *
 * The narrow layout keeps a compact primary action and an icon refresh, and
 * moves every remaining capability into a keyboard-operable "more actions"
 * menu. Permission, pending, label and handler wiring must be identical to the
 * desktop inline layout — the menu changes placement, never policy.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { useState } from "react";
import { render, screen, fireEvent, within, act } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { LaunchActionMenu, type LaunchActionMenuItem } from "../LaunchActionMenu";
import { LaunchHeader } from "../LaunchHeader";

const launchFixture = {
  id: "ff04d66b-3d00-4d67-8cf4-d36772fff708",
  name: "banking-agent-v2",
  status: "COMPLETED",
  quality_conclusion: "pass",
  dataset_name: "banking-agent-regression",
  langfuse_sync_status: "SYNCED",
  langfuse_experiment_url: null,
  created_at: "2026-10-08T16:08:13Z",
  started_at: "2026-10-08T16:08:14Z",
  completed_at: "2026-10-08T16:08:18.82Z",
  allowed_actions: ["run", "cancel", "resume", "retry_failed", "retry_evaluation"],
  manifest: {
    agent: { id: "banking-agent", version: "v2" },
    dataset: { dataset_name: "banking-agent-regression", items_count: 6 },
  },
} as any;

const completeSnapshot = {
  snapshot_id: "snap-v2-001",
  revision: 2,
  is_latest: true,
  evidence_state: "COMPLETE",
  quality_pass_count: 6,
  quality_fail_count: 0,
  quality_unknown_count: 0,
} as any;

const stubViewport = (desktop: boolean, onAddListener?: (listener: (event: MediaQueryListEvent) => void) => void) => {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: desktop,
    media: query,
    onchange: null,
    addEventListener: (_event: string, listener: () => void) => onAddListener?.(listener),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("LaunchActionMenu (U4 menu contract)", () => {
  const baseItems = (
    overrides: { runSelect?: () => void; cancelSelect?: () => void; retrySelect?: () => void; pending?: boolean } = {},
  ): LaunchActionMenuItem[] => [
    { kind: "action", id: "run", label: "启动评测 (Run)", testId: "run-button", onSelect: overrides.runSelect ?? vi.fn() },
    { kind: "action", id: "cancel", label: "取消评测 (Cancel)", testId: "cancel-button", pending: overrides.pending, pendingLabel: "正在取消...", onSelect: overrides.cancelSelect ?? vi.fn() },
    { kind: "action", id: "retry_evaluation", label: "重试评测 (Retry Eval)", testId: "retry-evaluation-button", onSelect: overrides.retrySelect ?? vi.fn() },
    { kind: "link", id: "langfuse", label: "Langfuse Trace", href: "https://cloud.langfuse.com/trace/abc", detailLabel: "Langfuse Trace" },
    { kind: "note", id: "langfuse-reason", label: "尚未创建 Langfuse Run" },
  ];

  const renderMenu = (props: Partial<React.ComponentProps<typeof LaunchActionMenu>> = {}) => {
    const items = (props.items ?? baseItems()) as LaunchActionMenuItem[];
    const onOpenChange = props.onOpenChange ?? vi.fn();
    const utils = render(
      <LaunchActionMenu {...props} items={items} onOpenChange={onOpenChange} open={props.open ?? false} />,
    );
    return { ...utils, items, onOpenChange };
  };

  it("trigger exposes menu semantics and opens on click", () => {
    const onOpenChange = vi.fn();
    renderMenu({ onOpenChange });
    const trigger = screen.getByTestId("launch-more-actions");
    expect(trigger).toHaveTextContent("更多操作");
    expect(trigger).toHaveAttribute("aria-haspopup", "menu");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();

    fireEvent.click(trigger);
    expect(onOpenChange).toHaveBeenCalledWith(true);
  });

  it("open menu has role=menu, a label and an aria-controls link to the trigger", () => {
    renderMenu({ open: true });
    const trigger = screen.getByTestId("launch-more-actions");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    const controlledBy = trigger.getAttribute("aria-controls");
    expect(controlledBy).toBeTruthy();
    const menu = screen.getByRole("menu");
    expect(menu).toHaveAttribute("aria-label", "当前 Launch 操作");
    expect(menu.id).toBe(controlledBy);
    // read-only explanations are not menu items
    const note = screen.getByText("尚未创建 Langfuse Run");
    expect(note).not.toHaveAttribute("role", "menuitem");
  });

  it("opening the menu moves focus to the first executable item", () => {
    renderMenu({ open: true });
    expect(document.activeElement).toBe(screen.getByTestId("run-button"));
  });

  it("ArrowDown opens the menu from the trigger without a mouse", () => {
    const onOpenChange = vi.fn();
    renderMenu({ onOpenChange });
    const trigger = screen.getByTestId("launch-more-actions");
    act(() => trigger.focus());
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    expect(onOpenChange).toHaveBeenCalledWith(true);
  });

  it("ArrowUp/ArrowDown/Home/End cycle enabled items and skip pending ones", () => {
    renderMenu({ items: baseItems({ pending: true }), open: true });
    const menu = screen.getByRole("menu");
    const run = screen.getByTestId("run-button");
    const retry = screen.getByTestId("retry-evaluation-button");
    const pending = screen.getByTestId("cancel-button");
    expect(pending).toHaveAttribute("aria-disabled", "true");
    const link = within(menu).getByRole("menuitem", { name: /Langfuse Trace/ });

    expect(document.activeElement).toBe(run);
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement).toBe(retry);
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement).toBe(link);
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement).toBe(run);
    fireEvent.keyDown(menu, { key: "ArrowUp" });
    expect(document.activeElement).toBe(link);
    fireEvent.keyDown(menu, { key: "Home" });
    expect(document.activeElement).toBe(run);
    fireEvent.keyDown(menu, { key: "End" });
    expect(document.activeElement).toBe(link);
  });

  it("a pending menu item is disabled and never executes its handler", () => {
    const cancelSelect = vi.fn();
    renderMenu({ items: baseItems({ pending: true, cancelSelect }), open: true });
    const pending = screen.getByTestId("cancel-button");
    expect(pending).toBeDisabled();
    expect(pending).toHaveTextContent("正在取消...");
    fireEvent.click(pending);
    expect(cancelSelect).not.toHaveBeenCalled();
  });

  it("Escape closes the menu and restores focus to the trigger", () => {
    const onOpenChange = vi.fn();
    renderMenu({ onOpenChange, open: true });
    const menu = screen.getByRole("menu");
    const trigger = screen.getByTestId("launch-more-actions");
    fireEvent.keyDown(menu, { key: "Escape" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(document.activeElement).toBe(trigger);
  });

  it("Tab closes the menu and lets focus leave", () => {
    const onOpenChange = vi.fn();
    renderMenu({ onOpenChange, open: true });
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Tab" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("an outside pointer-down closes the menu", () => {
    const onOpenChange = vi.fn();
    renderMenu({ onOpenChange, open: true });
    fireEvent.mouseDown(document.body);
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("selecting an action closes the menu and restores trigger focus before the handler runs", () => {
    let activeWhenHandled: Element | null = null;
    const runSelect = vi.fn(() => {
      activeWhenHandled = document.activeElement;
    });
    const onOpenChange = vi.fn();
    // Stateful harness: mirrors LaunchHeader's controlled usage so the menu
    // really unmounts when the component asks its parent to close.
    const Harness = () => {
      const [open, setOpen] = useState(true);
      return (
        <LaunchActionMenu
          items={baseItems({ runSelect })}
          open={open}
          onOpenChange={(next) => {
            onOpenChange(next);
            setOpen(next);
          }}
        />
      );
    };
    render(<Harness />);
    const trigger = screen.getByTestId("launch-more-actions");
    fireEvent.click(screen.getByTestId("run-button"));
    expect(runSelect).toHaveBeenCalledTimes(1);
    expect(activeWhenHandled).toBe(trigger);
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(document.activeElement).toBe(trigger);
  });

  it("link items are real safe external links, never placeholder hrefs", () => {
    renderMenu({ open: true });
    const link = within(screen.getByRole("menu")).getByRole("menuitem", { name: /Langfuse Trace/ });
    expect(link.tagName).toBe("A");
    expect(link).toHaveAttribute("href", "https://cloud.langfuse.com/trace/abc");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
    expect(link.getAttribute("href")).not.toBe("#");
  });
});

describe("LaunchHeader layout parity (U4)", () => {
  const headerProps = (overrides: Record<string, unknown> = {}) => ({
    launch: launchFixture,
    activeSnapshot: completeSnapshot,
    allowedActions: ["run", "cancel", "resume", "retry_failed", "retry_evaluation"],
    langfuseLink: { kind: "reason" as const, label: "尚未创建 Langfuse Run", title: "not created" },
    isFetching: false,
    qualityConclusion: "pass",
    onRefresh: vi.fn(),
    onRun: vi.fn(),
    onCancel: vi.fn(),
    onResume: vi.fn(),
    onRetryFailed: vi.fn(),
    onRetryEvaluation: vi.fn(),
    onOpenBaselineModal: vi.fn(),
    ...overrides,
  });

  const renderHeader = (props: Record<string, unknown>) =>
    render(
      <MemoryRouter>
        <LaunchHeader {...(props as any)} />
      </MemoryRouter>,
    );

  const openMenu = () => {
    fireEvent.click(screen.getByTestId("launch-more-actions"));
  };

  it("narrow layout: compact baseline + icon refresh stay visible, run actions live in the 更多操作 menu", () => {
    stubViewport(false);
    renderHeader(headerProps());
    expect(screen.getByRole("button", { name: "设为新 Baseline" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新" })).toBeInTheDocument();
    expect(screen.getByTestId("launch-more-actions")).toHaveTextContent("更多操作");
    expect(screen.queryByRole("button", { name: "启动评测 (Run)" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "取消评测 (Cancel)" })).not.toBeInTheDocument();
    // the Langfuse reason stays readable on narrow
    expect(screen.getByTestId("langfuse-link-reason")).toHaveTextContent("尚未创建 Langfuse Run");
  });

  it("narrow menu exposes every allowed action with the desktop labels and test ids", () => {
    stubViewport(false);
    renderHeader(headerProps());
    openMenu();
    const menu = screen.getByRole("menu");
    expect(within(menu).getByRole("menuitem", { name: "启动评测 (Run)" })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "取消评测 (Cancel)" })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "断点恢复 (Resume)" })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "重试失败用例 (Retry Failed)" })).toBeInTheDocument();
    expect(within(menu).getByTestId("retry-evaluation-button")).toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: /Langfuse Trace/ })).not.toBeInTheDocument();
  });

  it("narrow menu item runs the same handler as the desktop inline button", () => {
    stubViewport(false);
    const onCancel = vi.fn();
    renderHeader(headerProps({ onCancel }));
    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "取消评测 (Cancel)" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("narrow pending matches desktop: the pending menu item is not executable", () => {
    stubViewport(false);
    const onCancel = vi.fn();
    renderHeader(headerProps({ onCancel, isCancelPending: true }));
    openMenu();
    const pending = within(screen.getByRole("menu")).getByRole("menuitem", { name: /取消/ });
    expect(pending).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(pending);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("narrow menu never turns a disallowed action into an executable item", () => {
    stubViewport(false);
    renderHeader(headerProps({ allowedActions: [] }));
    openMenu();
    const menu = screen.getByRole("menu");
    expect(within(menu).queryByRole("menuitem", { name: /启动评测/ })).not.toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: /取消评测/ })).not.toBeInTheDocument();
    expect(within(menu).getByText("尚未创建 Langfuse Run")).toBeInTheDocument();
  });

  it("desktop layout keeps inline secondary buttons and no 更多操作 trigger", () => {
    stubViewport(true);
    renderHeader(headerProps());
    expect(screen.getByRole("button", { name: "启动评测 (Run)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "取消评测 (Cancel)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "断点恢复 (Resume)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试失败用例 (Retry Failed)" })).toBeInTheDocument();
    expect(screen.getByTestId("retry-evaluation-button")).toBeInTheDocument();
    expect(screen.queryByTestId("launch-more-actions")).not.toBeInTheDocument();
  });

  it("crossing the breakpoint back to narrow does not resurrect the menu", () => {
    let changeListener: ((event: MediaQueryListEvent) => void) | null = null;
    stubViewport(false, (listener) => {
      changeListener = listener;
    });
    renderHeader(headerProps());
    openMenu();
    expect(screen.getByRole("menu")).toBeInTheDocument();
    act(() => changeListener?.({ matches: true } as MediaQueryListEvent));
    // desktop layout renders inline actions and no menu trigger
    expect(screen.queryByTestId("launch-more-actions")).not.toBeInTheDocument();
    act(() => changeListener?.({ matches: false } as MediaQueryListEvent));
    expect(screen.getByTestId("launch-more-actions")).toBeInTheDocument();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });
});
