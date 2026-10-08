import { useCallback, useRef } from "react";
import {
  isLaunchExecutionActive,
  isLaunchSyncActive,
  normalizeSyncStatus,
} from "./launchState";
import { isSafeLangfuseUrl, type LangfuseLinkLaunch } from "./langfuseLink";

/**
 * Bounded waiting window for a link that arrives after the launch already
 * reported SYNCED. It keeps polling for a while, then stops so a permanently
 * misconfigured environment cannot cause infinite browser requests.
 */
export const LINK_BACKFILL_WINDOW_MS = 60_000;

export type BackfillTracker = Map<string, { key: string; deadline: number }>;

export const createBackfillTracker = (): BackfillTracker => new Map();

export const backfillKeyFor = (launch: LangfuseLinkLaunch): string | null => {
  const runId = (launch.langfuse_experiment_id ?? "").trim();
  if (!runId) return null;
  if (isLaunchExecutionActive(launch.status)) return null;
  if (isLaunchSyncActive(launch.langfuse_sync_status)) return null;
  if (normalizeSyncStatus(launch.langfuse_sync_status) !== "SYNCED") return null;
  if (isSafeLangfuseUrl(launch.langfuse_experiment_url)) return null;
  return `${launch.id ?? ""}::${runId}`;
};

export interface LaunchPollingDecision {
  executionActive: boolean;
  syncActive: boolean;
  backfilling: boolean;
  shouldPoll: boolean;
}

/**
 * Pure polling decision, shared by the list and the detail page.
 *
 * The backfill window is keyed by `launch_id + run_id`: fresh responses, new
 * object identities or `dataUpdatedAt` changes never extend it. Only a new run
 * id, or the launch becoming active again, starts a new window.
 */
export const evaluateLaunchPolling = (
  tracker: BackfillTracker,
  launches: readonly (LangfuseLinkLaunch | null | undefined)[],
  now: number,
  windowMs: number = LINK_BACKFILL_WINDOW_MS,
  loaded: boolean = true
): LaunchPollingDecision => {
  let executionActive = false;
  let syncActive = false;
  let backfilling = false;
  const seen = new Set<string>();

  for (const launch of launches) {
    if (!launch) continue;
    const launchId = launch.id ?? "";
    seen.add(launchId);

    if (isLaunchExecutionActive(launch.status) || isLaunchSyncActive(launch.langfuse_sync_status)) {
      executionActive = executionActive || isLaunchExecutionActive(launch.status);
      syncActive = syncActive || isLaunchSyncActive(launch.langfuse_sync_status);
      // Re-entering an active state re-arms the window once the launch settles.
      tracker.delete(launchId);
      continue;
    }

    const key = backfillKeyFor(launch);
    if (!key) {
      tracker.delete(launchId);
      continue;
    }
    const entry = tracker.get(launchId);
    if (!entry || entry.key !== key) {
      tracker.set(launchId, { key, deadline: now + windowMs });
      backfilling = true;
      continue;
    }
    if (now < entry.deadline) {
      backfilling = true;
    } else {
      tracker.set(launchId, entry);
    }
  }

  // Filtering or navigating away drops launches from `data`; their windows must not
  // survive, otherwise re-entering a filtered launch could never open a new one.
  //
  // Only an unresolved query counts as "not loaded yet". A loaded result that happens
  // to be empty is a real answer -- a filter matching nothing -- and must prune: those
  // windows have long expired, and keeping them means resetting the filter reuses a
  // stale deadline and the launch stops polling for a link that arrives later.
  if (loaded) {
    for (const launchId of [...tracker.keys()]) {
      if (!seen.has(launchId)) tracker.delete(launchId);
    }
  }

  return {
    executionActive,
    syncActive,
    backfilling,
    shouldPoll: executionActive || syncActive || backfilling,
  };
};

/**
 * Returns a `refetchInterval` callback that keeps polling while the launch is
 * executing, while Langfuse sync is still running, or while a bounded link
 * backfill window is open.
 */
export const useLaunchPolling = <T extends LangfuseLinkLaunch>(
  intervalMs: number,
  windowMs: number = LINK_BACKFILL_WINDOW_MS
) => {
  const trackerRef = useRef<BackfillTracker | null>(null);
  if (trackerRef.current === null) {
    trackerRef.current = createBackfillTracker();
  }

  return useCallback(
    (data: T | readonly T[] | undefined) => {
      const launches = Array.isArray(data) ? data : data ? [data as T] : [];
      return evaluateLaunchPolling(
        trackerRef.current!,
        launches,
        Date.now(),
        windowMs,
        data !== undefined
      ).shouldPoll
        ? intervalMs
        : false;
    },
    [intervalMs, windowMs]
  );
};
