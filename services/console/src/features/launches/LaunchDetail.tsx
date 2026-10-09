import React, { useEffect, useState } from "react";
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

type LaunchResponse = import("../../api/schema").components["schemas"]["ExperimentLaunchResponse"];
type ItemExecution = import("../../api/schema").components["schemas"]["ExperimentItemExecutionResponse"];
type SnapshotList = import("../../api/schema").components["schemas"]["ResultSnapshotListResponse"];
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

  // 2. Fetch Result Snapshots List (Issue #85)
  const snapshotsQuery = useQuery({
    queryKey: [...queryKeys.launches.all, "result-snapshots", launchId],
    enabled: Boolean(launchId),
    queryFn: async () => {
      if (!launchId) return null;
      const res = await api.GET("/api/v1/experiment-launches/{launch_id}/result-snapshots", {
        params: { path: { launch_id: launchId } },
      });
      if (res.error) throw res.error;
      return (res.data ?? null) as SnapshotList | null;
    },
  });

  const revisions: SnapshotRevision[] = snapshotsQuery.data?.revisions ?? [];
  const selectedSnapshot = revisions.find((row) => row.snapshot_id === selectedSnapshotId) ?? null;
  // If a snapshot_id is explicitly specified, never silently fall back to latest revisions[0]
  const activeSnapshot = selectedSnapshotId
    ? selectedSnapshot
    : (revisions[0] ?? null);
  const effectiveSnapshotId = selectedSnapshotId || activeSnapshot?.snapshot_id || null;

  useEffect(() => {
    setActionError(null);
    setShowRetryModal(false);
    setForceRetry(false);
    setShowBaselineModal(false);
    setCasesFilter("ALL");
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
    isLoading: isItemsLoading,
    error: itemsError,
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

  const items: ItemExecution[] = Array.isArray(rawItems) ? rawItems : [];

  // Invalidate all queries
  const invalidateAll = () => {
    if (launchId) {
      queryClient.invalidateQueries({ queryKey: queryKeys.launches.detail(launchId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.launches.items(launchId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.launches.list() });
      queryClient.invalidateQueries({ queryKey: [...queryKeys.launches.all, "result-snapshots", launchId] });
    }
  };

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
    (frozenSummary?.quality_pass_count != null || frozenSummary?.quality_fail_count != null) ||
    (activeSnap?.quality_pass_count != null || activeSnap?.quality_fail_count != null);

  const frozenPassCount = Number(frozenSummary?.quality_pass_count ?? activeSnap?.quality_pass_count ?? 0);
  const frozenFailCount = Number(frozenSummary?.quality_fail_count ?? activeSnap?.quality_fail_count ?? 0);
  const frozenUnknownCount = Number(frozenSummary?.quality_unknown_count ?? activeSnap?.quality_unknown_count ?? 0);
  const frozenTotalCount = frozenPassCount + frozenFailCount + frozenUnknownCount;

  // Quality metrics calculations
  const totalItems: number | null = hasFrozenQualityCounts
    ? (frozenTotalCount > 0 ? frozenTotalCount : (itemsError ? null : items.length))
    : (itemsError ? null : items.length);

  const passedItems: number | null = hasFrozenQualityCounts
    ? frozenPassCount
    : (itemsError
        ? null
        : items.filter((i) => i.quality_conclusion?.toLowerCase() === "pass").length);

  const decisionCounts: { pass: number; fail: number; unknown: number } | null = hasFrozenQualityCounts
    ? { pass: frozenPassCount, fail: frozenFailCount, unknown: frozenUnknownCount }
    : (itemsError
        ? null
        : items.reduce(
            (acc, item) => {
              const conclusion = (item.quality_conclusion || "unknown").toLowerCase();
              if (conclusion === "pass") acc.pass += 1;
              else if (conclusion === "fail") acc.fail += 1;
              else acc.unknown += 1;
              return acc;
            },
            { pass: 0, fail: 0, unknown: 0 },
          ));

  const decidedCount = decisionCounts ? decisionCounts.pass + decisionCounts.fail : 0;
  const passRateNum = frozenSummary?.pass_rate != null ? Number(frozenSummary.pass_rate) : null;
  const decidedPassRate = passRateNum != null
    ? (passRateNum * 100).toFixed(1)
    : decidedCount > 0 && decisionCounts
    ? ((decisionCounts.pass / decidedCount) * 100).toFixed(1)
    : null;
  const decisionCoverage = decisionCounts && totalItems ? (decidedCount / totalItems) * 100 : null;

  // Derive frozen quality conclusion for LaunchHeader
  let frozenQualityConclusion = launch.quality_conclusion;
  if (hasFrozenQualityCounts) {
    if (frozenFailCount > 0) {
      frozenQualityConclusion = "fail";
    } else if (frozenPassCount > 0) {
      frozenQualityConclusion = "pass";
    } else {
      frozenQualityConclusion = "unknown";
    }
  }

  const isSnapshotNotFound = Boolean(
    selectedSnapshotId && snapshotsQuery.isSuccess && !selectedSnapshot
  );

  return (
    <div className="space-y-6">
      {/* 1. 业务主体 Header */}
      <LaunchHeader
        launch={launch}
        activeSnapshot={activeSnapshot}
        allowedActions={allowedActions}
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
        onCancel={() => cancelMutation.mutate()}
        onResume={() => resumeMutation.mutate()}
        onRetryFailed={() => {
          setForceRetry(false);
          setShowRetryModal(true);
        }}
        onRetryEvaluation={() => retryEvaluationMutation.mutate()}
        onOpenBaselineModal={() => setShowBaselineModal(true)}
        isRunPending={runMutation.isPending}
        isCancelPending={cancelMutation.isPending}
        isResumePending={resumeMutation.isPending}
        isRetryFailedPending={retryFailedMutation.isPending}
        isRetryEvaluationPending={retryEvaluationMutation.isPending}
      />

      {/* 结果快照版本控制面板 (Result Snapshot Revision) */}
      {launchId && (
        <ResultSnapshotPanel
          launchId={launchId}
          selectedSnapshotId={selectedSnapshotId}
          onSelect={selectSnapshot}
        />
      )}

      {/* 快照请求异常与未找到警示 (Alert) */}
      {summaryQuery.error && currentTab !== "compare" && (
        <div
          role="alert"
          className="p-3 text-xs bg-fail-subtle border border-fail-border rounded-lg text-fail font-medium"
        >
          {formatApiError(summaryQuery.error)}
        </div>
      )}

      {!summaryQuery.error && isSnapshotNotFound && (
        <div
          role="alert"
          className="p-3 text-xs bg-fail-subtle border border-fail-border rounded-lg text-fail font-medium"
        >
          Snapshot {selectedSnapshotId} not found
        </div>
      )}

      {/* 用例明细数据加载异常或归属不一致警示 */}
      {itemsError && (
        <ErrorState
          message={formatApiError(itemsError)}
          onRetry={() => refetchItems()}
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
          else if (tabId === "compare") setTab("compare");
        }}
      />

      {/* 3. 运行态与诊断进度面板 (Progress Board) */}
      <ExecutionProgressPanel
        status={launch.status}
        progress={launch.progress}
        cancelRequestedAt={launch.cancel_requested_at}
        statusReason={launch.status_reason}
      />

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
          {itemsError && <ErrorState message={formatApiError(itemsError)} onRetry={() => refetchItems()} />}
          {!isItemsLoading && !itemsError && (
            <CasesTraceTab
              key={`${launchId}-${effectiveSnapshotId}`}
              launchId={launch.id}
              snapshotId={effectiveSnapshotId}
              items={items}
              manifestDataset={manifest.dataset}
              currentFilter={casesFilter}
              onFilterChange={setCasesFilter}
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
