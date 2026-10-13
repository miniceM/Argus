/**
 * Client-scoped request locks for Launch run actions (issue 119 gap F1, PR 134 review).
 *
 * A run-action request outlives the page instance that started it: the user can
 * switch Revision, go back to the list, or remount the detail page while the
 * POST is still in flight. The in-flight lock therefore belongs to the
 * QueryClient (the lifetime of the application session), not to a React
 * component instance — only the owning request may release it.
 *
 * The store deliberately knows nothing about pages: it never holds component
 * setters, modal callbacks or feedback handlers, it never performs network
 * calls, and it becomes unreachable together with its QueryClient (WeakMap).
 */
import type { QueryClient } from "@tanstack/react-query";
import type { LaunchActionKind } from "./useLaunchActionScope";

/** Unique token handed to exactly one request; the only key that releases a lock. */
export type LaunchActionLockOwner = number;

export type LaunchActionLockClient = {
  subscribe: (listener: () => void) => () => void;
  /** Stable, primitive snapshot so useSyncExternalStore never re-renders in a loop. */
  getVersion: () => number;
  /**
   * Atomically acquires the (target Launch, action) lock. Returns the owner
   * token, or null when the same target already has this action in flight —
   * which is how a duplicate POST is refused before any network call.
   */
  begin: (action: LaunchActionKind, targetLaunchId: string) => LaunchActionLockOwner | null;
  isLocked: (action: LaunchActionKind, launchId: string | null | undefined) => boolean;
  /** Releases only when `owner` still owns the lock; a late settle is a no-op. */
  release: (action: LaunchActionKind, targetLaunchId: string, owner: LaunchActionLockOwner) => void;
};

const lockKey = (targetLaunchId: string, action: LaunchActionKind) => `${targetLaunchId}::${action}`;

type LockState = {
  locks: Map<string, LaunchActionLockOwner>;
  sequence: number;
  version: number;
  listeners: Set<() => void>;
};

const createLockClient = (): LaunchActionLockClient => {
  const state: LockState = { locks: new Map(), sequence: 0, version: 0, listeners: new Set() };

  const notify = () => {
    state.version += 1;
    state.listeners.forEach((listener) => listener());
  };

  return {
    subscribe: (listener) => {
      state.listeners.add(listener);
      return () => {
        state.listeners.delete(listener);
      };
    },
    getVersion: () => state.version,
    begin: (action, targetLaunchId) => {
      if (!targetLaunchId) return null;
      const key = lockKey(targetLaunchId, action);
      if (state.locks.has(key)) return null;
      state.sequence += 1;
      const owner = state.sequence;
      state.locks.set(key, owner);
      notify();
      return owner;
    },
    isLocked: (action, launchId) => {
      if (!launchId) return false;
      return state.locks.has(lockKey(launchId, action));
    },
    release: (action, targetLaunchId, owner) => {
      const key = lockKey(targetLaunchId, action);
      if (state.locks.get(key) !== owner) return;
      state.locks.delete(key);
      notify();
    },
  };
};

const lockClients = new WeakMap<QueryClient, LaunchActionLockClient>();

/**
 * Returns the lock registry of the given QueryClient, creating it on first use.
 * Two clients never share a table; a client's table disappears with the client.
 */
export const getLaunchActionLockClient = (queryClient: QueryClient): LaunchActionLockClient => {
  const existing = lockClients.get(queryClient);
  if (existing) return existing;
  const created = createLockClient();
  lockClients.set(queryClient, created);
  return created;
};
