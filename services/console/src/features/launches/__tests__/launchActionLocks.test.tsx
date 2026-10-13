/**
 * Issue #119 PR #134 review gap F1: launch action locks must live with the
 * QueryClient, not with a page instance.
 *
 * A run-action request outlives the detail page it was started on. Leaving the
 * page (switch Revision, back to the list, remount) must not release the lock,
 * and a remounted page must neither duplicate the POST nor inherit the old
 * page's feedback rights. Locks are also per client: another Launch, another
 * action, or another QueryClient must never be disabled by this lock.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useLaunchActionScope, type LaunchActionKind, type LaunchActionView } from "../useLaunchActionScope";

const LAUNCH_A = "ff04d66b-3d00-4d67-8cf4-d36772fff708";
const LAUNCH_B = "aaaaaaaa-3d00-4d67-8cf4-d36772fff708";

const ACTIONS: LaunchActionKind[] = ["run", "cancel", "resume", "retry_failed", "retry_evaluation"];

const newClient = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });

const view: LaunchActionView = { launchId: LAUNCH_A, snapshotId: "snap-v1-001", epoch: 0 };

const renderScope = (client: QueryClient, initialView: LaunchActionView = view) => {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(({ next }: { next: LaunchActionView }) => useLaunchActionScope(next), {
    wrapper,
    initialProps: { next: initialView },
  });
};

afterEach(() => {
  vi.clearAllMocks();
});

describe("launch action locks are client-scoped, not page-scoped", () => {
  it("acquires one exclusive lock per (target Launch, action) for all five actions", () => {
    const client = newClient();
    const { result } = renderScope(client);
    for (const action of ACTIONS) {
      const first = result.current.begin(action, LAUNCH_A);
      expect(first, `${action} must acquire the first lock`).not.toBeNull();
      expect(result.current.isPending(action, LAUNCH_A)).toBe(true);
      expect(result.current.begin(action, LAUNCH_A), `${action} must refuse a second lock`).toBeNull();
    }
  });

  it("keeps the lock when the page unmounts and remounts on the same QueryClient", () => {
    const client = newClient();
    const first = renderScope(client);
    const context = first.result.current.begin("retry_evaluation", LAUNCH_A);
    expect(context).not.toBeNull();
    first.unmount();

    const second = renderScope(client);
    expect(second.result.current.isPending("retry_evaluation", LAUNCH_A), "a remounted page still sees the pending request").toBe(true);
    expect(second.result.current.begin("retry_evaluation", LAUNCH_A), "a remounted page must not start a second request").toBeNull();
  });

  it("does not cross-lock a different Launch or a different action of the same Launch", () => {
    const client = newClient();
    const { result } = renderScope(client);
    expect(result.current.begin("run", LAUNCH_A)).not.toBeNull();
    expect(result.current.begin("cancel", LAUNCH_A), "another action is a different request").not.toBeNull();
    expect(result.current.begin("run", LAUNCH_B), "another Launch is a different request").not.toBeNull();
    expect(result.current.isPending("run", LAUNCH_B)).toBe(true);
    expect(result.current.isPending("retry_evaluation", LAUNCH_A)).toBe(false);
  });

  it("isolates the lock table per QueryClient", () => {
    const clientOne = newClient();
    const clientTwo = newClient();
    const first = renderScope(clientOne);
    expect(first.result.current.begin("retry_evaluation", LAUNCH_A)).not.toBeNull();

    const second = renderScope(clientTwo);
    expect(second.result.current.isPending("retry_evaluation", LAUNCH_A), "another client cannot see the lock").toBe(false);
    expect(second.result.current.begin("retry_evaluation", LAUNCH_A), "another client must not be blocked by it").not.toBeNull();
  });

  it("releases only the owning request, so a late settle cannot clear a newer lock", () => {
    const client = newClient();
    const { result } = renderScope(client);
    const first = result.current.begin("retry_evaluation", LAUNCH_A);
    expect(first).not.toBeNull();
    act(() => result.current.settle(first!));
    expect(result.current.isPending("retry_evaluation", LAUNCH_A)).toBe(false);

    const second = result.current.begin("retry_evaluation", LAUNCH_A);
    expect(second).not.toBeNull();
    act(() => result.current.settle(first!));
    expect(result.current.isPending("retry_evaluation", LAUNCH_A), "the stale settle must not release the new lock").toBe(true);
    act(() => result.current.settle(second!));
    expect(result.current.isPending("retry_evaluation", LAUNCH_A)).toBe(false);
  });

  it("releases the lock from an unmounted owner and lets the next page act", () => {
    const client = newClient();
    const first = renderScope(client);
    const context = first.result.current.begin("retry_evaluation", LAUNCH_A);
    expect(context).not.toBeNull();
    first.unmount();

    // The network request settles with no page subscribed: the lock must still
    // be released by the owning request, or the next page could never act.
    const second = renderScope(client);
    act(() => second.result.current.settle(context!));
    expect(second.result.current.isPending("retry_evaluation", LAUNCH_A)).toBe(false);
    expect(second.result.current.begin("retry_evaluation", LAUNCH_A)).not.toBeNull();
  });

  it("never lets a remounted page receive the old page's feedback", () => {
    const client = newClient();
    const first = renderScope(client);
    const context = first.result.current.begin("retry_evaluation", LAUNCH_A);
    expect(context).not.toBeNull();
    first.unmount();

    // The remounted page shows the same Launch, snapshot and epoch; only the
    // per-instance session owner distinguishes it from the page that clicked.
    const second = renderScope(client);
    expect(second.result.current.canWriteFeedback(context!), "the old click must not write to the new page").toBe(false);
    const fresh = second.result.current.begin("cancel", LAUNCH_A);
    expect(second.result.current.canWriteFeedback(fresh!)).toBe(true);
  });
});
