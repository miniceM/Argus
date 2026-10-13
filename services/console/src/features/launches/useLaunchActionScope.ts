/**
 * Page-session scope for Launch run actions (issue 119 gap U3).
 *
 * Clicking run / cancel / resume / retry_failed / retry_evaluation starts a network
 * request that outlives the page identity it was started on: the user can switch
 * Revision or Launch, navigate A→B→A, or unmount while the request is in flight.
 *
 * This hook keeps three things apart:
 *   1. the **target** of the request (fixed at click time — the request must never
 *      move to whichever launch happens to be displayed when it settles),
 *   2. the **page session** allowed to write feedback (epoch + view identity
 *      captured at click time), and
 *   3. the **in-flight lock**, tracked per (target Launch, action) and released
 *      only by its own request when it settles.
 *
 * Identity changes clear page feedback but deliberately do not release locks:
 * "the user switched away" is not "the request finished".
 */
import { useCallback, useRef, useState } from "react";

export type LaunchActionKind =
  | "run"
  | "cancel"
  | "resume"
  | "retry_failed"
  | "retry_evaluation";

export type LaunchActionContext = {
  /** Launch the request was issued for; never re-derived after the click. */
  targetLaunchId: string;
  action: LaunchActionKind;
  requestId: number;
  /** Page session captured at click time. */
  viewLaunchId: string;
  viewSnapshotId: string | null;
  /** Monotonic page epoch; advanced on every page identity change. */
  epoch: number;
};

export type LaunchActionView = {
  launchId: string | null | undefined;
  snapshotId: string | null;
  epoch: number;
};

const lockKey = (targetLaunchId: string, action: LaunchActionKind) =>
  `${targetLaunchId}::${action}`;

export type LaunchActionScope = {
  /**
   * Registers a new request. Returns null when the same target Launch already has
   * this action in flight, which is how a duplicate POST is refused without
   * touching the server.
   */
  begin: (action: LaunchActionKind, targetLaunchId: string) => LaunchActionContext | null;
  /** True only when the request may still write feedback to the current page. */
  canWriteFeedback: (context: LaunchActionContext) => boolean;
  /** Pending state of one action, resolved against its own target Launch. */
  isPending: (action: LaunchActionKind, launchId: string | null | undefined) => boolean;
  /** Releases only this request's lock. */
  settle: (context: LaunchActionContext) => void;
};

export function useLaunchActionScope(view: LaunchActionView): LaunchActionScope {
  const locks = useRef<Map<string, number>>(new Map());
  const nextRequestId = useRef(1);
  const [lockVersion, setLockVersion] = useState(0);
  const latestView = useRef(view);
  latestView.current = view;

  const begin = useCallback<LaunchActionScope["begin"]>((action, targetLaunchId) => {
    if (!targetLaunchId) return null;
    const key = lockKey(targetLaunchId, action);
    if (locks.current.has(key)) return null;
    const requestId = nextRequestId.current++;
    locks.current.set(key, requestId);
    setLockVersion((version) => version + 1);
    const current = latestView.current;
    return {
      targetLaunchId,
      action,
      requestId,
      viewLaunchId: current.launchId ?? "",
      viewSnapshotId: current.snapshotId,
      epoch: current.epoch,
    };
  }, []);

  const settle = useCallback<LaunchActionScope["settle"]>((context) => {
    const key = lockKey(context.targetLaunchId, context.action);
    // Only the owning request may release the lock, so a late settle can never
    // clear a newer request's pending state.
    if (locks.current.get(key) !== context.requestId) return;
    locks.current.delete(key);
    setLockVersion((version) => version + 1);
  }, []);

  const canWriteFeedback = useCallback<LaunchActionScope["canWriteFeedback"]>((context) => {
    const current = latestView.current;
    return (
      context.epoch === current.epoch &&
      context.viewLaunchId === (current.launchId ?? "") &&
      context.viewSnapshotId === current.snapshotId
    );
  }, []);

  // Read during render: every lock change also bumps `lockVersion`, which is what
  // re-renders the buttons. A different Launch's pending request must not disable
  // this Launch's action, hence the per-target key.
  const isPending: LaunchActionScope["isPending"] = (action, launchId) => {
    void lockVersion;
    if (!launchId) return false;
    return locks.current.has(lockKey(launchId, action));
  };

  return { begin, settle, canWriteFeedback, isPending };
}
