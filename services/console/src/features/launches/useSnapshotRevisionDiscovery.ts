import { useCallback, useEffect, useRef, useState } from "react";

/** Poll cadence while looking for a revision that has just been frozen. */
export const SNAPSHOT_DISCOVERY_INTERVAL_MS = 1500;

/**
 * Bounded window for "the run just finished, the frozen revision may not be visible yet".
 *
 * A fixed window, started once per (launch, transition out of activity), is what keeps this
 * from turning into permanent polling for every terminal launch.
 */
export const SNAPSHOT_DISCOVERY_WINDOW_MS = 30_000;

export interface SnapshotRevisionDiscoveryInput {
  launchId: string | null | undefined;
  /** Execution is still running (launch status is a non-terminal execution state). */
  executionActive: boolean;
  /** Live items are still being evaluated, even though execution already finished. */
  evaluationActive: boolean;
  /** The history query reached a terminal state with or without data. */
  historySettled: boolean;
  /** Newest revision currently visible, if any. */
  latestSnapshotId: string | null;
  /** Refetch the shared history query. */
  refresh: () => void;
}

interface DiscoveryWindow {
  /** Launch this window belongs to; a change discards the window. */
  launchId: string;
  /** Newest revision visible when the window opened; `null` means "none yet". */
  baselineLatest: string | null;
  /** Wall-clock start; the window length is measured from here and never extended. */
  startedAt: number;
}

interface ExplicitDiscoveryBaseline {
  launchId: string;
  baselineLatest: string | null;
  startedAt: number;
}

export interface SnapshotRevisionDiscoveryResult {
  /** True while the bounded discovery window is open. */
  isDiscovering: boolean;
  /** Explicitly look for a revision newer than the snapshot visible before a result-changing action. */
  startDiscovery: (baselineLatest: string | null) => void;
}

/**
 * Watches for the moment a Launch stops being active and re-reads the revision directory, so
 * a just-frozen result report appears without a manual refresh or window refocus.
 *
 * This hook owns the only refresh of that query key. The launch itself is already polled
 * while it executes; the snapshot directory must not keep polling afterwards, otherwise
 * every terminal launch would poll forever.
 */
export function useSnapshotRevisionDiscovery(
  input: SnapshotRevisionDiscoveryInput,
): SnapshotRevisionDiscoveryResult {
  const {
    launchId,
    executionActive,
    evaluationActive,
    historySettled,
    latestSnapshotId,
    refresh,
  } = input;

  const isActive = Boolean(executionActive || evaluationActive);
  const [activeWindow, setActiveWindow] = useState<DiscoveryWindow | null>(null);

  const observedActiveRef = useRef(false);
  const observedActiveBaselineRef = useRef<string | null>(null);
  const explicitDiscoveryBaselineRef = useRef<ExplicitDiscoveryBaseline | null>(null);
  // Launch identity for which the "no revision yet" window has already run once.
  const initialCycleRef = useRef<string | null>(null);

  // Activity is observed as it happens, so the transition out of it stays detectable even if
  // the launch query is between polls.
  useEffect(() => {
    if (isActive && !observedActiveRef.current) {
      observedActiveRef.current = true;
      // If retry-evaluation already opened a discovery window, keep its submission-time
      // baseline. A newer snapshot found by that window is sufficient; completion of the
      // activity must not start a redundant second window against the new latest revision.
      const explicit = explicitDiscoveryBaselineRef.current;
      const explicitIsCurrent = Boolean(
        explicit &&
          explicit.launchId === launchId &&
          Date.now() - explicit.startedAt < SNAPSHOT_DISCOVERY_WINDOW_MS,
      );
      observedActiveBaselineRef.current = explicitIsCurrent
        ? explicit!.baselineLatest
        : activeWindow
        ? activeWindow.baselineLatest
        : latestSnapshotId;
    }
  }, [activeWindow, isActive, launchId, latestSnapshotId]);

  // A revision became visible: the empty-directory cycle for this launch is satisfied.
  useEffect(() => {
    if (latestSnapshotId != null) initialCycleRef.current = null;
  }, [latestSnapshotId]);

  // Launch identity change: forget everything that belonged to the previous launch.
  useEffect(() => {
    observedActiveRef.current = false;
    observedActiveBaselineRef.current = null;
    explicitDiscoveryBaselineRef.current = null;
    initialCycleRef.current = null;
    setActiveWindow(null);
  }, [launchId]);

  const openWindow = useCallback((baselineLatest: string | null) => {
    const next: DiscoveryWindow = {
      launchId: launchId ?? "",
      baselineLatest,
      startedAt: Date.now(),
    };
    setActiveWindow(next);
    // The directory may already hold the frozen revision while the launch query still echoes
    // the previous status, so read it immediately instead of waiting a full interval.
    refresh();
  }, [launchId, refresh]);

  const startDiscovery = useCallback((baselineLatest: string | null) => {
    if (!launchId) return;
    // A result-changing action is an explicit signal: capture the known revision before the
    // mutation and start bounded discovery even if /items never exposes an intermediate active state.
    observedActiveRef.current = false;
    observedActiveBaselineRef.current = null;
    explicitDiscoveryBaselineRef.current = {
      launchId,
      baselineLatest,
      startedAt: Date.now(),
    };
    initialCycleRef.current = launchId;
    openWindow(baselineLatest);
  }, [launchId, openWindow]);

  const settled = Boolean(
    activeWindow &&
      (activeWindow.baselineLatest == null
        ? latestSnapshotId != null
        : latestSnapshotId != null && latestSnapshotId !== activeWindow.baselineLatest),
  );
  const expired = Boolean(
    activeWindow &&
      Date.now() - activeWindow.startedAt >= SNAPSHOT_DISCOVERY_WINDOW_MS,
  );

  // Retry only while the window is open.
  useEffect(() => {
    if (!activeWindow || settled || expired) return;
    const interval = setInterval(refresh, SNAPSHOT_DISCOVERY_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [activeWindow, settled, expired, refresh]);

  useEffect(() => {
    if (!activeWindow) return;
    if (settled || expired) {
      setActiveWindow(null);
      return;
    }
  }, [activeWindow, settled, expired]);

  useEffect(() => {
    if (!launchId || !historySettled || isActive) return;

    const observedActive = observedActiveRef.current;
    const activityBaseline = observedActiveBaselineRef.current;
    const explicit = explicitDiscoveryBaselineRef.current;
    const explicitIsCurrent = Boolean(
      explicit &&
        explicit.launchId === launchId &&
        Date.now() - explicit.startedAt < SNAPSHOT_DISCOVERY_WINDOW_MS,
    );
    const discoveryBaseline = explicitIsCurrent
      ? explicit!.baselineLatest
      : activityBaseline;
    observedActiveRef.current = false;
    observedActiveBaselineRef.current = null;

    if (observedActive) {
      explicitDiscoveryBaselineRef.current = null;
      const revisionAlreadyFound = discoveryBaseline == null
        ? latestSnapshotId != null
        : latestSnapshotId != null && latestSnapshotId !== discoveryBaseline;
      if (revisionAlreadyFound || activeWindow) return;

      // Activity ended before a newer revision appeared and no discovery window remains.
      openWindow(discoveryBaseline);
      initialCycleRef.current = launchId;
      return;
    }

    if (activeWindow) return;

    const nothingFrozenYet = latestSnapshotId == null;
    if (nothingFrozenYet && initialCycleRef.current !== launchId) {
      // Terminal launch with no revision yet: try once, in case freezing lagged behind.
      initialCycleRef.current = launchId;
      openWindow(null);
    }
  }, [activeWindow, historySettled, isActive, launchId, latestSnapshotId, openWindow]);

  // Report the initial discovery synchronously on the first settled empty-directory render.
  // Otherwise the parent could briefly render mutable /items before this effect opens its
  // bounded window on the next commit.
  const initialDiscoveryPending = Boolean(
    launchId &&
      historySettled &&
      !isActive &&
      latestSnapshotId == null &&
      initialCycleRef.current !== launchId &&
      !activeWindow,
  );

  return {
    isDiscovering: Boolean((activeWindow && !settled && !expired) || initialDiscoveryPending),
    startDiscovery,
  };
}

/**
 * Stable refetch callback for the snapshot directory query.
 *
 * React Query hands back a new `refetch` identity per observer update; without this wrapper
 * the discovery effect would tear down and recreate its interval on unrelated renders.
 */
export function useSnapshotDirectoryRefresh(query: {
  refetch: () => unknown;
}): () => void {
  const refetchRef = useRef(query.refetch);
  refetchRef.current = query.refetch;
  return useCallback(() => {
    void refetchRef.current();
  }, []);
}
