/**
 * Page-session scope for Launch run actions (issue 119 gap U3, PR 134 review gap F1).
 *
 * Clicking run / cancel / resume / retry_failed / retry_evaluation starts a network
 * request that outlives the page identity it was started on: the user can switch
 * Revision or Launch, navigate A→B→A, go back to the list and remount the detail
 * page, or unmount while the request is in flight.
 *
 * This hook keeps three lifetimes apart:
 *   1. the **target** of the request (fixed at click time — the request must never
 *      move to whichever launch happens to be displayed when it settles),
 *   2. the **in-flight lock**, keyed by (target Launch, action) and owned by the
 *      request itself. It lives with the QueryClient (see launchActionLocks.ts),
 *      so leaving or remounting a page can neither duplicate nor silently drop a
 *      pending request, and only its owner releases it, and
 *   3. the **page session** allowed to write feedback: this hook instance
 *      (per mount) plus the view epoch and view identity captured at click time.
 *      A remounted page may observe a pending request but never inherits the old
 *      page's right to show notices, errors or modal closes.
 */
import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  getLaunchActionLockClient,
  type LaunchActionLockOwner,
} from "./launchActionLocks";

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
  /** Client-scoped request sequence; does not restart per page mount. */
  requestId: number;
  /** Exclusive token of the in-flight lock; only its owner may release it. */
  lockOwner: LaunchActionLockOwner;
  /** Identity of the page instance that started the request (per mount). */
  viewSessionOwner: object;
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

export type LaunchActionScope = {
  /**
   * Registers a new request. Returns null when the same target Launch already has
   * this action in flight — across Revision switches, page remounts and the whole
   * QueryClient lifetime — which is how a duplicate POST is refused without
   * touching the server.
   */
  begin: (action: LaunchActionKind, targetLaunchId: string) => LaunchActionContext | null;
  /** True only when this mounted page session may still write feedback. */
  canWriteFeedback: (context: LaunchActionContext) => boolean;
  /** Pending state of one action, resolved against its own target Launch. */
  isPending: (action: LaunchActionKind, launchId: string | null | undefined) => boolean;
  /** Releases only this request's lock, from any page state including unmounted. */
  settle: (context: LaunchActionContext) => void;
};

export function useLaunchActionScope(view: LaunchActionView): LaunchActionScope {
  // Locks belong to the current provider's QueryClient: not to this hook
  // instance, and never to a module-global table shared across clients.
  const queryClient = useQueryClient();
  const lockClient = getLaunchActionLockClient(queryClient);
  const lockVersion = useSyncExternalStore(
    lockClient.subscribe,
    lockClient.getVersion,
    lockClient.getVersion,
  );

  // One identity per mounted page instance. Two mounts of the same route keep
  // the same epoch and view, so the instance owner is what tells an old click
  // from a new page.
  const sessionOwner = useRef<object>({});
  const mounted = useRef(true);
  // The setup must re-arm the flag: main.tsx mounts the app inside
  // React.StrictMode, whose mount -> unmount -> mount cycle re-runs this effect.
  // Re-arming on setup is what keeps a remounted page both able to write its own
  // feedback and unable to inherit the previous page's.
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const latestView = useRef(view);
  latestView.current = view;

  const begin = useCallback<LaunchActionScope["begin"]>((action, targetLaunchId) => {
    const lockOwner = lockClient.begin(action, targetLaunchId);
    if (lockOwner === null) return null;
    const current = latestView.current;
    return {
      targetLaunchId,
      action,
      requestId: lockOwner,
      lockOwner,
      viewSessionOwner: sessionOwner.current,
      viewLaunchId: current.launchId ?? "",
      viewSnapshotId: current.snapshotId,
      epoch: current.epoch,
    };
  }, [lockClient]);

  const settle = useCallback<LaunchActionScope["settle"]>(
    (context) => {
      lockClient.release(context.action, context.targetLaunchId, context.lockOwner);
    },
    [lockClient],
  );

  const canWriteFeedback = useCallback<LaunchActionScope["canWriteFeedback"]>((context) => {
    if (!mounted.current) return false;
    if (context.viewSessionOwner !== sessionOwner.current) return false;
    const current = latestView.current;
    return (
      context.epoch === current.epoch &&
      context.viewLaunchId === (current.launchId ?? "") &&
      context.viewSnapshotId === current.snapshotId
    );
  }, []);

  // Read during render: every lock change bumps the store version, which is what
  // re-renders the buttons. A different Launch's pending request must not disable
  // this Launch's action, hence the per-target key.
  const isPending: LaunchActionScope["isPending"] = (action, launchId) => {
    void lockVersion;
    return lockClient.isLocked(action, launchId);
  };

  return { begin, settle, canWriteFeedback, isPending };
};
