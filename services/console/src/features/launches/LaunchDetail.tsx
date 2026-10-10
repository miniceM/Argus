import React, { useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import {
  ArrowLeft,
  FileText,
  Layers,
  Scale,
} from "lucide-react";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";
import { isLaunchExecutionActive } from "./launchState";
import { useLaunchPolling } from "./useLaunchPolling";
import { getLangfuseLinkView } from "./langfuseLink";
import { ErrorState, LoadingState } from "../../components/StateViews";
import { Button } from "../../components/ui/Primitives";
import { Modal } from "../../components/ui/Overlay";

import { LaunchHeader } from "./LaunchHeader";
import { LaunchKpiOverview } from "./LaunchKpiOverview";
import { ExecutionProgressPanel } from "./ExecutionProgressPanel";
import { SetBaselineModal } from "./SetBaselineModal";
import { ResultSnapshotPanel } from "./resultSnapshot";
import { GateComparisonTab } from "./tabs/GateComparisonTab";
import { CasesTraceTab } from "./tabs/CasesTraceTab";
import { ManifestAuditTab } from "./tabs/ManifestAuditTab";
import { projectFrozenCase } from "./launchReportView";
import {
  snapshotDetailKey,
  snapshotDetailQueryOptions,
  snapshotListKey,
  snapshotListQueryOptions,
} from "./launchSnapshotQueries";
import {
  useSnapshotDirectoryRefresh,
  useSnapshotRevisionDiscovery,
} from "./useSnapshotRevisionDiscovery";

type LaunchResponse = import("../../api/schema").components["schemas"]["ExperimentLaunchResponse"];
type ItemExecution = import("../../api/schema").components["schemas"]["ExperimentItemExecutionResponse"];
type SnapshotRevision = import("../../api/schema").components["schemas"]["ResultSnapshotRevisionResponse"];
type Baseline = import("../../api/schema").components["schemas"]["BaselineResponse"];
type RunSummary = import("../../api/schema").components["schemas"]["RunSummaryResponse"];

const TERMINAL = new Set(["COMPLETED", "PARTIAL_FAILED", "FAILED", "CANCELLED"]);

export const LaunchDetail: React.FC = () => {
  const { launchId } = useParams<{ launchId: string }>();
  const queryClient = useQueryClient();

  const VALID_TABS = ["compare", "cases", "audit"] as const;
  type TabKey = (typeof VALID_TABS)[number];

  const [searchParams, setSearchParams] = useSearchParams();
  const selectedSnapshotId = searchParams.get("snapshot_id");
  const rawTab = searchParams.get("tab");
  const currentTab: TabKey = rawTab && VALID_TABS.includes(rawTab as TabKey)
    ? (rawTab as TabKey)
    : "compare";

  const [actionError, setActionError] = useState<string | null>(null);
  const [showRetryModal, setShowRetryModal] = useState(false);
  const [forceRetry, setForceRetry] = useState(false);
  const [evalRetryNotice, setEvalRetryNotice] = useState<string | null>(null);
  const [showBaselineModal, setShowBaselineModal] = useState(false);
  const [casesFilter, setCasesFilter] = useState<string>("ALL");

  // Switch tab without losing other params
  const setTab = (tab: TabKey) => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set("tab", tab);
        return next;
      },
      { replace: false },
    );
  };

  // Keyboard navigation for WAI-ARIA tabs
  const handleTabKeyDown = (e: React.KeyboardEvent, index: number) => {
    let targetIndex = -1;
    if (e.key === "ArrowRight") {
      targetIndex = (index + 1) % VALID_TABS.length;
    } else if (e.key === "ArrowLeft") {
      targetIndex = (index - 1 + VALID_TABS.length) % VALID_TABS.length;
    } else if (e.key === "Home") {
      targetIndex = 0;
    } else if (e.key === "End") {
      targetIndex = VALID_TABS.length - 1;
    }

    if (targetIndex !== -1) {
      e.preventDefault();
      const nextTab = VALID_TABS[targetIndex];
      setTab(nextTab);
      const nextBtn = document.getElementById(`btn-tab-${nextTab}`);
      nextBtn?.focus();
    }
  };

  // Select snapshot and sync URL
  const selectSnapshot = (snapshotId: string) => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set("snapshot_id", snapshotId);
        return next;
      },
      { replace: false },
    );
  };

  // 1. Fetch Launch Details with S2 Polling
  const launchPollingInterval = useLaunchPolling<LaunchResponse>(1500);

  const {
    data: launch,
    isLoading: isLaunchLoading,
    error: launchError,
    refetch: refetchLaunch,
    isFetching: isLaunchFetching,
  } = useQuery<LaunchResponse>({
    queryKey: queryKeys.launches.detail(launchId || ""),
    queryFn: async () => {
      if (!launchId) throw new Error("缺少 Launch ID");
      const res = await api.GET("/api/v1/experiment-launches/{launch_id}", {
        params: { path: { launch_id: launchId } },
      });

      if (res.error) {
        if (res.response.status === 404) {
          throw new Error(`Launch ${launchId} 不存在或已删除`);
        }
        throw res.error;
      }

      if (
        !res.data ||
        Array.isArray(res.data) ||
        typeof res.data !== "object" ||
        res.data.id !== launchId
      ) {
        throw new Error("Launch 详情响应与请求 ID 不一致，请重新加载");
      }

      return res.data as LaunchResponse;
    },
    enabled: Boolean(launchId),
    refetchInterval: (query) => launchPollingInterval(query.state.data as LaunchResponse | undefined),
  });

  // 2. Fetch Result Snapshots List (Issue #85) — shared entry, see launchSnapshotQueries
  const snapshotsQuery = useQuery(snapshotListQueryOptions(launchId));

  const revisions: SnapshotRevision[] = snapshotsQuery.data?.revisions ?? [];
  const selectedSnapshot = revisions.find((row) => row.snapshot_id === selectedSnapshotId) ?? null;
  const isSnapshotNotFound = Boolean(
    selectedSnapshotId && snapshotsQuery.isSuccess && !selectedSnapshot
  );
  // If a snapshot_id is explicitly specified, never silently fall back to latest revisions[0]
  const activeSnapshot = selectedSnapshotId
    ? selectedSnapshot
    : (revisions[0] ?? null);
  const effectiveSnapshotId = selectedSnapshotId || activeSnapshot?.snapshot_id || null;

  const [expandedCaseId, setExpandedCaseId] = useState<string | null>(null);
  const [showCancelModal, setShowCancelModal] = useState(false);
  const [comparisonFilter, setComparisonFilter] = useState("ALL");
  const [comparisonSelectedCaseId, setComparisonSelectedCaseId] = useState<string | null>(null);

  useEffect(() => {
    setActionError(null);
    setShowRetryModal(false);
    setForceRetry(false);
    setShowBaselineModal(false);
    setShowCancelModal(false);
    setCasesFilter("ALL");
    setExpandedCaseId(null);
    setComparisonFilter("ALL");
    setComparisonSelectedCaseId(null);
  }, [launchId, effectiveSnapshotId]);

  // Sync snapshot_id into URL if none was specified and one exists
  useEffect(() => {
    if (
      !selectedSnapshotId &&
      activeSnapshot?.snapshot_id &&
      snapshotsQuery.data?.latest_snapshot_id
    ) {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (!next.has("snapshot_id")) {
            next.set("snapshot_id", activeSnapshot.snapshot_id);
          }
          return next;
        },
        { replace: true },
      );
    }
  }, [activeSnapshot?.snapshot_id, selectedSnapshotId, setSearchParams, snapshotsQuery.data?.latest_snapshot_id]);

  // 3. Fetch Items with S2 Polling
  const {
    data: rawItems,
    isLoading: isRawItemsLoading,
    error: rawItemsError,
    refetch: refetchItems,
  } = useQuery<ItemExecution[]>({
    queryKey: queryKeys.launches.items(launchId || ""),
    queryFn: async () => {
      if (!launchId) return [];
      const res = await api.GET("/api/v1/experiment-launches/{launch_id}/items", {
        params: { path: { launch_id: launchId } },
      });

      if (res.error) throw res.error;
      if (!Array.isArray(res.data)) {
        throw new Error("用例明细响应格式无效，请重新加载");
      }

      if (res.data.some((item) => item.launch_id !== launchId)) {
        throw new Error("用例明细归属的 Launch 与当前页面不一致，请重新加载");
      }

      return res.data as ItemExecution[];
    },
    enabled: Boolean(launchId && launch?.id === launchId),
    refetchInterval: (query) => {
      const current = query.state.data as ItemExecution[] | undefined;
      if (
        Array.isArray(current) &&
        current.some((i) => (i.evaluation_status || "").toLowerCase() === "evaluating")
      ) {
        return 1500;
      }
      if (launch && isLaunchExecutionActive(launch.status)) {
        return 1500;
      }
      return false;
    },
  });

  // 3.1 Fetch Snapshot Detail for frozen items when viewing an immutable snapshot.
  //
  // An explicitly requested revision keeps its own identity even when the revision directory
  // cannot answer: an empty or failed directory must never be mistaken for "this revision
  // does not exist", so a failed directory falls back to reading that revision directly.
  // A revision the directory answered *without* listing is definitively missing, and no
  // detail request is issued for it (and never falls back to the latest revision).
  const historyResolved = snapshotsQuery.isSuccess || snapshotsQuery.isError;
  const targetSnapshotId = selectedSnapshotId
    ? (selectedSnapshot || snapshotsQuery.isError ? selectedSnapshotId : null)
    : (activeSnapshot?.snapshot_id ?? null);
  const snapshotDetailQuery = useQuery(
    snapshotDetailQueryOptions(
      launchId && targetSnapshotId ? { launchId, snapshotId: targetSnapshotId } : null,
    ),
  );

  const rawItemsList: ItemExecution[] = Array.isArray(rawItems) ? rawItems : [];
  const liveItemsMap = useMemo(() => {
    const map = new Map<string, ItemExecution>();
    for (const item of rawItemsList) {
      if (item.dataset_item_id) {
        map.set(item.dataset_item_id, item);
      }
    }
    return map;
  }, [rawItemsList]);

  // Data isolation (M01 & M04):
  // 1. Explicit invalid snapshot: empty items, never bleed live items
  // 2. Frozen snapshot: strictly consume snapshot items with pure projector, never bleed live items or fake frozen
  // 3. Pure live run (no snapshot specified and no revisions exist): consume rawItemsList
  const items = useMemo(() => {
    if (isSnapshotNotFound) {
      return [];
    }
    if (targetSnapshotId) {
      // The shared query throws on any invalid payload, so data here is already verified.
      const frozenList = snapshotDetailQuery.data?.items;
      if (Array.isArray(frozenList)) {
        return frozenList.map((row: any) => {
          const datasetItemId = String(row.dataset_item_id || "");
          const stableExecId = liveItemsMap.get(datasetItemId)?.id ?? null;
          return projectFrozenCase(row, stableExecId);
        });
      }
      return [];
    }
    if (!selectedSnapshotId && revisions.length === 0) {
      return rawItemsList.map((row) => ({
        ...row,
        trace_url: row.langfuse_trace_url ?? null,
        latency_ms: row.final_attempt_latency_ms ?? null,
        is_frozen: false,
      })) as any[];
    }
    return [];
  }, [isSnapshotNotFound, targetSnapshotId, snapshotDetailQuery.data?.items, selectedSnapshotId, revisions.length, rawItemsList, liveItemsMap]);

  const isItemsLoading = isSnapshotNotFound
    ? false
    : targetSnapshotId
    ? snapshotDetailQuery.isLoading
    : (!selectedSnapshotId && revisions.length === 0)
    ? isRawItemsLoading
    : snapshotsQuery.isLoading;

  // A rejected detail payload surfaces as a query error, so the error branch is the single
  // place where "silent empty report" is impossible. An HTTP 404 remains the only credible
  // proof that a requested revision does not exist.
  const itemsError = isSnapshotNotFound
    ? new Error(`评测快照 ${selectedSnapshotId} 不存在或无权访问`)
    : targetSnapshotId
    ? snapshotDetailQuery.error
    : (!selectedSnapshotId && revisions.length === 0)
    ? (snapshotsQuery.isError ? new Error(`快照服务异常: ${formatApiError(snapshotsQuery.error)}`) : rawItemsError)
    : snapshotsQuery.error
    ? new Error(`快照服务异常: ${formatApiError(snapshotsQuery.error)}`)
    : null;

  // Retry routing (M01 & M04): retry frozen snapshot detail query on snapshot error, not live /items
  const handleRetryItems = () => {
    if (targetSnapshotId) {
      snapshotDetailQuery.refetch();
      summaryQuery.refetch();
      if (snapshotsQuery.isError) {
        snapshotsQuery.refetch();
      }
    } else if (!selectedSnapshotId && revisions.length === 0) {
      refetchItems();
    } else {
      snapshotsQuery.refetch();
      refetchItems();
    }
  };

  // Invalidate all queries
  const invalidateAll = () => {
    if (launchId) {
      queryClient.invalidateQueries({ queryKey: queryKeys.launches.detail(launchId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.launches.items(launchId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.launches.list() });
      queryClient.invalidateQueries({ queryKey: [...snapshotListKey(launchId)] });
      queryClient.invalidateQueries({ queryKey: [...snapshotDetailKey(launchId, null)] });
    }
  };

  // 3.2 Bounded discovery of a just-frozen revision.
  //
  // The launch query stops polling as soon as the run reaches a terminal state, but the
  // frozen revision is written around that same moment. Without one bounded re-read the
  // report would stay "not frozen yet" until the user manually refreshes or refocuses the
  // window, even though the snapshot exists server-side.
  const refreshSnapshots = useSnapshotDirectoryRefresh(snapshotsQuery);
  const evaluationActive = Array.isArray(rawItems) && rawItems.some(
    (item) => (item.evaluation_status || "").toLowerCase() === "evaluating",
  );
  const { isDiscovering } = useSnapshotRevisionDiscovery({
    launchId,
    executionActive: Boolean(launch && isLaunchExecutionActive(launch.status)),
    evaluationActive,
    historySettled: historyResolved,
    latestSnapshotId: snapshotsQuery.data?.latest_snapshot_id ?? null,
    refresh: refreshSnapshots,
  });

  // 4. Mutations
  const runMutation = useMutation({
    mutationFn: async () => {
      if (!launchId) return;
      setActionError(null);
      const res = await api.POST("/api/v1/experiment-launches/{launch_id}/run", {
        params: { path: { launch_id: launchId } },
      });
      if (res.error) throw res.error;
      return res.data;
    },
    onSuccess: invalidateAll,
    onError: (err) => setActionError(formatApiError(err)),
  });

  const cancelMutation = useMutation({
    mutationFn: async () => {
      if (!launchId) return;
      setActionError(null);
      const res = await api.POST("/api/v1/experiment-launches/{launch_id}/cancel", {
        params: { path: { launch_id: launchId } },
      });
      if (res.error) throw res.error;
      return res.data;
    },
    onSuccess: invalidateAll,
    onError: (err) => setActionError(formatApiError(err)),
  });

  const resumeMutation = useMutation({
    mutationFn: async () => {
      if (!launchId) return;
      setActionError(null);
      const res = await api.POST("/api/v1/experiment-launches/{launch_id}/resume", {
        params: { path: { launch_id: launchId } },
      });
      if (res.error) throw res.error;
      return res.data;
    },
    onSuccess: invalidateAll,
    onError: (err) => setActionError(formatApiError(err)),
  });

  const retryFailedMutation = useMutation({
    mutationFn: async (force: boolean) => {
      if (!launchId) return;
      setActionError(null);
      const res = await api.POST("/api/v1/experiment-launches/{launch_id}/retry-failed", {
        params: { path: { launch_id: launchId } },
        body: { force },
      });
      if (res.error) throw res.error;
      return res.data;
    },
    onSuccess: () => {
      setShowRetryModal(false);
      invalidateAll();
    },
    onError: (err) => {
      const errMsg = formatApiError(err);
      setActionError(errMsg);
      if (errMsg.toLowerCase().includes("force") || errMsg.toLowerCase().includes("ambiguous")) {
        setShowRetryModal(true);
      }
    },
  });

  const retryEvaluationMutation = useMutation({
    mutationFn: async () => {
      if (!launchId) return;
      setActionError(null);
      setEvalRetryNotice(null);
      const res = await api.POST("/api/v1/experiment-launches/{launch_id}/retry-evaluation", {
        params: { path: { launch_id: launchId } },
      });
      if (res.error) throw res.error;
      return res.data;
    },
    onSuccess: (data) => {
      const submitted = data?.submitted?.length ?? 0;
      const blocked = data?.blocked ?? [];
      let notice = `已提交 ${submitted} 个用例仅重试评测（复用原 Agent 输出，不会再次调用 Agent）。`;
      if (blocked.length > 0) {
        notice += ` ${blocked.length} 个用例因检查点不可用被阻止：${blocked[0].message}`;
      }
      setEvalRetryNotice(notice);
      invalidateAll();
    },
    onError: (err) => {
      setActionError(formatApiError(err));
    },
  });

  // 5. Comparison & Summary data for KPI & Tab 1
  const manifest = (launch?.manifest || {}) as any;
  const manifestAgent = manifest.agent || {};
  const agentId = manifestAgent.id || launch?.agent_id || "";
  const environment = manifest.comparison?.environment || "production";
  const canReadResults = TERMINAL.has(launch?.status || "") || Boolean(effectiveSnapshotId);

  const summaryQuery = useQuery({
    queryKey: queryKeys.launches.summary(launchId || "", effectiveSnapshotId, 0),
    queryFn: async () => {
      if (!launchId) return null;
      const response = await api.GET("/api/v1/experiment-launches/{launch_id}/summary", {
        params: { path: { launch_id: launchId }, query: { snapshot_id: effectiveSnapshotId ?? undefined } },
      });
      if (response.error) throw response.error;
      return response.data as unknown as RunSummary;
    },
    enabled: Boolean(canReadResults && launchId),
  });

  const activeBaselineQuery = useQuery({
    queryKey: queryKeys.baselines.detail(agentId, environment),
    queryFn: async (): Promise<Baseline | null> => {
      if (!agentId) return null;
      const res = await api.GET("/api/v1/agents/{agent_id}/baselines", {
        params: { path: { agent_id: agentId }, query: { environment } },
      });
      if (res.error) {
        if (res.response?.status === 404) return null;
        throw res.error;
      }
      return res.data as unknown as Baseline;
    },
    enabled: Boolean(agentId),
  });

  const comparisonInitialQuery = useQuery({
    queryKey: [...queryKeys.launches.comparison(launchId || "", effectiveSnapshotId ?? "unresolved", "ALL"), "kpi-initial"],
    queryFn: async () => {
      if (!launchId || !effectiveSnapshotId) return null;
      const res = await api.GET("/api/v1/experiment-launches/{launch_id}/comparison", {
        params: {
          path: { launch_id: launchId },
          query: { snapshot_id: effectiveSnapshotId, limit: 50 },
        },
      });
      if (res.error) throw res.error;
      return res.data as any;
    },
    enabled: Boolean(canReadResults && launchId && effectiveSnapshotId),
  });

  if (isLaunchLoading) return <LoadingState message="正在加载评测任务与不可变快照..." />;
  if (launchError) {
    return (
      <div className="space-y-3">
        <Link
          to="/launches"
          className="inline-flex items-center gap-1.5 text-xs font-semibold text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="w-3.5 h-3.5" />
          <span>返回评测列表</span>
        </Link>
        <ErrorState message={formatApiError(launchError)} onRetry={() => refetchLaunch()} />
      </div>
    );
  }
  if (!launch) return <ErrorState message="未找到对应的评测任务" />;

  const allowedActions = launch.allowed_actions || (launch.status === "PENDING" ? ["run"] : []);
  const langfuseLink = getLangfuseLinkView(launch);

  // Frozen summary and snapshot metrics calculations
  const frozenSummary = summaryQuery.data?.summary as Record<string, any> | undefined;
  const activeSnap = activeSnapshot as Record<string, any> | undefined;
  const hasFrozenQualityCounts =
    !isSnapshotNotFound && (
      (frozenSummary?.quality_pass_count != null || frozenSummary?.quality_fail_count != null) ||
      (activeSnap?.quality_pass_count != null || activeSnap?.quality_fail_count != null)
    );

  const frozenPassCount = Number(frozenSummary?.quality_pass_count ?? activeSnap?.quality_pass_count ?? 0);
  const frozenFailCount = Number(frozenSummary?.quality_fail_count ?? activeSnap?.quality_fail_count ?? 0);
  const frozenUnknownCount = Number(frozenSummary?.quality_unknown_count ?? activeSnap?.quality_unknown_count ?? 0);
  const frozenTotalCount = frozenPassCount + frozenFailCount + frozenUnknownCount;

  // Quality metrics calculations
  const totalItems: number | null = isSnapshotNotFound
    ? null
    : hasFrozenQualityCounts
    ? (frozenTotalCount > 0 ? frozenTotalCount : (itemsError ? null : items.length))
    : activeSnapshot
    ? (itemsError ? null : items.length)
    : (!selectedSnapshotId && revisions.length === 0)
    ? (itemsError ? null : items.length)
    : null;

  const passedItems: number | null = isSnapshotNotFound
    ? null
    : hasFrozenQualityCounts
    ? frozenPassCount
    : activeSnapshot
    ? (itemsError ? null : items.filter((i: any) => i.quality_conclusion?.toLowerCase() === "pass").length)
    : (!selectedSnapshotId && revisions.length === 0)
    ? (itemsError ? null : items.filter((i: any) => i.quality_conclusion?.toLowerCase() === "pass").length)
    : null;

  const decisionCounts: { pass: number; fail: number; unknown: number } | null = isSnapshotNotFound
    ? null
    : hasFrozenQualityCounts
    ? { pass: frozenPassCount, fail: frozenFailCount, unknown: frozenUnknownCount }
    : activeSnapshot
    ? (itemsError
        ? null
        : items.reduce(
            (acc: any, item: any) => {
              const conclusion = (item.quality_conclusion || "unknown").toLowerCase();
              if (conclusion === "pass") acc.pass += 1;
              else if (conclusion === "fail") acc.fail += 1;
              else acc.unknown += 1;
              return acc;
            },
            { pass: 0, fail: 0, unknown: 0 },
          ))
    : (!selectedSnapshotId && revisions.length === 0)
    ? (itemsError
        ? null
        : items.reduce(
            (acc: any, item: any) => {
              const conclusion = (item.quality_conclusion || "unknown").toLowerCase();
              if (conclusion === "pass") acc.pass += 1;
              else if (conclusion === "fail") acc.fail += 1;
              else acc.unknown += 1;
              return acc;
            },
            { pass: 0, fail: 0, unknown: 0 },
          ))
    : null;

  const decidedCount = decisionCounts ? decisionCounts.pass + decisionCounts.fail : 0;
  const passRateNum = frozenSummary?.pass_rate != null ? Number(frozenSummary.pass_rate) : null;
  const decidedPassRate = passRateNum != null
    ? (passRateNum * 100).toFixed(1)
    : decidedCount > 0 && decisionCounts
    ? ((decisionCounts.pass / decidedCount) * 100).toFixed(1)
    : null;
  const decisionCoverage = decisionCounts && totalItems ? (decidedCount / totalItems) * 100 : null;

  // Derive frozen quality conclusion for LaunchHeader (fail-closed, UNKNOWN-safe)
  let frozenQualityConclusion: string | null = null;
  if (isSnapshotNotFound) {
    frozenQualityConclusion = "unknown";
  } else if (hasFrozenQualityCounts) {
    if (frozenFailCount > 0) {
      frozenQualityConclusion = "fail";
    } else if (frozenUnknownCount > 0) {
      frozenQualityConclusion = "unknown";
    } else if (
      frozenPassCount > 0 &&
      (activeSnap?.evidence_state === "COMPLETE" || summaryQuery.data?.evidence_state === "COMPLETE") &&
      activeSnap?.releasable !== false
    ) {
      frozenQualityConclusion = "pass";
    } else {
      frozenQualityConclusion = "unknown";
    }
  } else if (!effectiveSnapshotId && revisions.length === 0) {
    if (decisionCounts) {
      if (decisionCounts.fail > 0) {
        frozenQualityConclusion = "fail";
      } else if (decisionCounts.unknown > 0) {
        frozenQualityConclusion = "unknown";
      } else if (decisionCounts.pass > 0) {
        frozenQualityConclusion = launch.quality_conclusion?.toLowerCase() === "fail" ? "fail" : "pass";
      } else {
        frozenQualityConclusion = launch.quality_conclusion ?? "unknown";
      }
    } else {
      frozenQualityConclusion = launch.quality_conclusion ?? null;
    }
  } else {
    frozenQualityConclusion = "unknown";
  }

  return (
    <div className="space-y-4">
      {/* 1. 业务主体 Header */}
      <LaunchHeader
        launch={launch}
        activeSnapshot={activeSnapshot}
        allowedActions={isSnapshotNotFound ? [] : allowedActions}
        langfuseLink={langfuseLink}
        isFetching={isLaunchFetching}
        qualityConclusion={frozenQualityConclusion}
        onRefresh={() => {
          refetchLaunch();
          refetchItems();
          snapshotsQuery.refetch();
          summaryQuery.refetch();
        }}
        onRun={() => runMutation.mutate()}
        onCancel={() => setShowCancelModal(true)}
        onResume={() => resumeMutation.mutate()}
        onRetryFailed={() => {
          setForceRetry(false);
          setShowRetryModal(true);
        }}
        onRetryEvaluation={() => retryEvaluationMutation.mutate()}
        onOpenBaselineModal={isSnapshotNotFound ? undefined : () => setShowBaselineModal(true)}
        isRunPending={runMutation.isPending}
        isCancelPending={cancelMutation.isPending}
        isResumePending={resumeMutation.isPending}
        isRetryFailedPending={retryFailedMutation.isPending}
        isRetryEvaluationPending={retryEvaluationMutation.isPending}
      />

      {/* 快照请求异常与未找到警示 (Alert) - 仅在非 compare Tab 渲染，避免与 GateComparisonTab 内部 Alert 重复 */}
      {currentTab !== "compare" && (summaryQuery.error || isSnapshotNotFound) && (
        <div
          role="alert"
          className="p-3 text-xs bg-fail-subtle border border-fail-border rounded-lg text-fail font-medium"
        >
          {summaryQuery.error ? formatApiError(summaryQuery.error) : `Snapshot ${selectedSnapshotId} not found`}
        </div>
      )}

      {/* 用例明细数据加载异常或归属不一致警示 */}
      {itemsError && !isSnapshotNotFound && currentTab !== "cases" && (
        <ErrorState
          message={formatApiError(itemsError)}
          onRetry={handleRetryItems}
        />
      )}

      {/* 提示与反馈横幅 */}
      {evalRetryNotice && (
        <div
          className="p-3 text-xs bg-pass-subtle border border-pass-border rounded-lg text-pass font-medium"
          data-testid="retry-evaluation-notice"
          role="status"
        >
          {evalRetryNotice}
        </div>
      )}

      {actionError && (
        <div className="p-3 text-xs bg-fail-subtle border border-fail-border rounded-lg text-fail font-medium">
          {actionError}
        </div>
      )}

      {/* 2. 四项核心决策 KPI (Overview) */}
      <LaunchKpiOverview
        totalItems={totalItems}
        passedItems={passedItems}
        decisionCounts={decisionCounts}
        decidedPassRate={decidedPassRate}
        decisionCoverage={decisionCoverage}
        itemsError={Boolean(itemsError)}
        comparisonSummary={comparisonInitialQuery.data?.summary}
        classificationCounts={comparisonInitialQuery.data?.classification_counts}
        comparability={comparisonInitialQuery.data?.comparability}
        formal={comparisonInitialQuery.data?.formal}
        onNavigateTab={(tabId) => {
          if (tabId === "cases") setTab("cases");
          else if (tabId === "compare") {
            setTab("compare");
            setComparisonFilter("REGRESSION");
          }
        }}
      />

      {/* 3. 运行态与诊断进度面板 (Progress Board) */}
      <ExecutionProgressPanel
        status={launch.status}
        progress={launch.progress}
        cancelRequestedAt={launch.cancel_requested_at}
        statusReason={launch.status_reason}
        isEvaluating={Array.isArray(rawItems) && rawItems.some((i) => (i.evaluation_status || "").toLowerCase() === "evaluating")}
      />

      {/* 3.5 结果快照版本控制面板 (Issue #85) */}
      {launchId && (
        <ResultSnapshotPanel
          launchId={launchId}
          selectedSnapshotId={selectedSnapshotId}
          onSelect={selectSnapshot}
          discovering={isDiscovering}
        />
      )}

      {/* 4. Tab 导航条 (WAI-ARIA Compliant) */}
      <div
        role="tablist"
        aria-label="评测详情功能分区"
        className="border-b border-border flex items-center gap-6 sm:gap-8 text-sm font-medium pt-1"
      >
        <button
          type="button"
          role="tab"
          id="btn-tab-compare"
          aria-controls="tab-compare"
          aria-selected={currentTab === "compare"}
          tabIndex={currentTab === "compare" ? 0 : -1}
          onKeyDown={(e) => handleTabKeyDown(e, 0)}
          onClick={() => setTab("compare")}
          className={clsx(
            "pb-3 flex items-center gap-1.5 transition cursor-pointer font-semibold",
            currentTab === "compare"
              ? "text-primary border-b-2 border-primary"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          <Scale className="w-4 h-4" />
          <span>门禁与版本对比 (Gate & Comparison)</span>
        </button>

        <button
          type="button"
          role="tab"
          id="btn-tab-cases"
          aria-controls="tab-cases"
          aria-selected={currentTab === "cases"}
          tabIndex={currentTab === "cases" ? 0 : -1}
          onKeyDown={(e) => handleTabKeyDown(e, 1)}
          onClick={() => setTab("cases")}
          className={clsx(
            "pb-3 flex items-center gap-1.5 transition cursor-pointer font-semibold",
            currentTab === "cases"
              ? "text-primary border-b-2 border-primary"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          <Layers className="w-4 h-4" />
          <span>用例排查与 Trace ({totalItems ?? items.length})</span>
        </button>

        <button
          type="button"
          role="tab"
          id="btn-tab-audit"
          aria-controls="tab-audit"
          aria-selected={currentTab === "audit"}
          tabIndex={currentTab === "audit" ? 0 : -1}
          onKeyDown={(e) => handleTabKeyDown(e, 2)}
          onClick={() => setTab("audit")}
          className={clsx(
            "pb-3 flex items-center gap-1.5 transition cursor-pointer font-semibold",
            currentTab === "audit"
              ? "text-primary border-b-2 border-primary"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          <FileText className="w-4 h-4" />
          <span>不可变快照与审计 (Manifest & Policy)</span>
        </button>
      </div>

      {/* 5. 三大核心 Tab 面板 */}
      {currentTab === "compare" && (
        <div
          id="tab-compare"
          role="tabpanel"
          aria-labelledby="btn-tab-compare"
        >
          <GateComparisonTab
            launchId={launch.id}
            snapshotId={effectiveSnapshotId}
            environment={environment}
            summary={summaryQuery.data}
            activeBaseline={activeBaselineQuery.data ?? null}
            onSetBaselineModal={() => setShowBaselineModal(true)}
            onShowLatestSnapshot={() => {
              if (snapshotsQuery.data?.latest_snapshot_id) {
                selectSnapshot(snapshotsQuery.data.latest_snapshot_id);
              }
            }}
            qualityPolicyRules={manifest.quality_policy?.rules ?? []}
            manifestEvaluators={manifest.evaluators || []}
            currentFilter={comparisonFilter}
            onFilterChange={setComparisonFilter}
            selectedCaseId={comparisonSelectedCaseId}
            onSelectedCaseChange={setComparisonSelectedCaseId}
          />
        </div>
      )}

      {currentTab === "cases" && (
        <div
          id="tab-cases"
          role="tabpanel"
          aria-labelledby="btn-tab-cases"
        >
          {isItemsLoading && <LoadingState message="正在加载用例明细与得分..." />}
          {itemsError && !isSnapshotNotFound && <ErrorState message={formatApiError(itemsError)} onRetry={handleRetryItems} />}
          {!isItemsLoading && (!itemsError || isSnapshotNotFound) && (
            <CasesTraceTab
              key={`${launchId}-${effectiveSnapshotId}`}
              launchId={launch.id}
              snapshotId={effectiveSnapshotId}
              items={items}
              manifestDataset={manifest.dataset}
              currentFilter={casesFilter}
              onFilterChange={setCasesFilter}
              expandedCaseId={expandedCaseId}
              onExpandedCaseChange={setExpandedCaseId}
              snapshotCounts={
                hasFrozenQualityCounts
                  ? {
                      pass: frozenPassCount,
                      fail: frozenFailCount,
                      unknown: frozenUnknownCount,
                      total: totalItems,
                    }
                  : undefined
              }
            />
          )}
        </div>
      )}

      {currentTab === "audit" && (
        <div
          id="tab-audit"
          role="tabpanel"
          aria-labelledby="btn-tab-audit"
        >
          <ManifestAuditTab
            launch={launch}
            activeSnapshot={activeSnapshot}
            onSelectSnapshot={selectSnapshot}
          />
        </div>
      )}

      {/* 6. 模态框：设为 Baseline 二次确认弹窗 */}
      <SetBaselineModal
        open={showBaselineModal}
        onClose={() => setShowBaselineModal(false)}
        agentId={agentId}
        environment={environment}
        activeSnapshot={activeSnapshot}
        activeBaseline={activeBaselineQuery.data ?? null}
        onSuccess={() => {
          activeBaselineQuery.refetch();
          invalidateAll();
        }}
      />

      {/* 7. 模态框：取消评测二次确认弹窗 */}
      <Modal
        open={showCancelModal}
        onClose={() => setShowCancelModal(false)}
        title="确认取消评测任务"
        tone="danger"
        dismissable={!cancelMutation.isPending}
        footer={
          <>
            <Button
              type="button"
              variant="secondary"
              onClick={() => setShowCancelModal(false)}
              disabled={cancelMutation.isPending}
            >
              放弃
            </Button>
            <Button
              type="button"
              variant="danger"
              disabled={cancelMutation.isPending}
              onClick={() => {
                cancelMutation.mutate(undefined, {
                  onSettled: () => setShowCancelModal(false),
                });
              }}
            >
              {cancelMutation.isPending ? "正在取消..." : "确认取消"}
            </Button>
          </>
        }
      >
        <p className="text-xs text-muted-foreground">
          确定要取消当前评测任务吗？取消后，正在执行或排队中的用例将安全终止。
        </p>
      </Modal>

      {/* 7. 模态框：重试失败用例二次确认弹窗 */}
      <Modal
        open={showRetryModal}
        onClose={() => setShowRetryModal(false)}
        title="重试失败用例 (Retry Failed Items)"
        tone="danger"
        dismissable={!retryFailedMutation.isPending}
        footer={
          <>
            <Button
              type="button"
              variant="secondary"
              className="text-xs"
              onClick={() => setShowRetryModal(false)}
            >
              取消
            </Button>
            <Button
              type="button"
              variant="warning-solid"
              className="text-xs"
              disabled={retryFailedMutation.isPending}
              onClick={() => retryFailedMutation.mutate(forceRetry)}
            >
              {retryFailedMutation.isPending ? "正在提交重试..." : "确认重新调度"}
            </Button>
          </>
        }
      >
        <div className="p-6 space-y-4">
          <p className="text-xs text-foreground-secondary leading-relaxed">
            系统将仅针对执行失败 (<code className="text-fail font-mono font-semibold">FAILED</code>) 或超时 (<code className="text-timeout font-mono font-semibold">TIMED_OUT</code>) 的用例发起全新调度代次 (generation + 1)，已成功的用例将被严格保护并跳过。
          </p>

          <div className="p-3 bg-timeout-subtle border border-timeout-border rounded-xl">
            <label className="flex items-start gap-2.5 cursor-pointer">
              <input
                type="checkbox"
                checked={forceRetry}
                onChange={(e) => setForceRetry(e.target.checked)}
                className="mt-0.5 rounded text-primary focus:ring-focus"
              />
              <span className="text-xs text-timeout-strong">
                <strong className="block font-semibold">强制重试非幂等可能已发送用例 (Force Replay)</strong>
                若用例在 Worker 崩溃前可能已将请求发出且接口非幂等，勾选此项以确认允许二次执行。
              </span>
            </label>
          </div>
        </div>
      </Modal>
    </div>
  );
};
