import React, { useReducer, useEffect, useRef, useCallback } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, RefreshCw } from "lucide-react";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";
import { Button } from "../../components/ui/Primitives";
import { Modal } from "../../components/ui/Overlay";
import {
  baselineDialogReducer,
  initialBaselineDialogState,
  isConfirmDisabled,
} from "./baselineDialogState";

type SnapshotRevision = import("../../api/schema").components["schemas"]["ResultSnapshotRevisionResponse"];
type Baseline = import("../../api/schema").components["schemas"]["BaselineResponse"];
type BaselineResponseData = import("../../api/schema").components["schemas"]["BaselineResponse"];

interface SetBaselineModalProps {
  open: boolean;
  onClose: () => void;
  agentId: string;
  environment: string;
  activeSnapshot: SnapshotRevision | null;
  activeBaseline: Baseline | null;
  onSuccess?: () => void;
}

interface BaselineSession {
  epoch: number;
  agentId: string;
  environment: string;
  snapshotId: string;
}

const CONFLICT_MESSAGE =
  "Baseline 绑定版本发生并发冲突 (HTTP 409)，请重新确认最新状态后再试。";

/**
 * One dialog session = one (open, agent, environment, target snapshot) combination.
 *
 * The epoch is bumped during render whenever that combination changes, so every request,
 * callback and cache write started for a previous session becomes stale immediately —
 * including the moment the dialog closes, before it is ever reopened.
 */
export const SetBaselineModal: React.FC<SetBaselineModalProps> = ({
  open,
  onClose,
  agentId,
  environment,
  activeSnapshot,
  activeBaseline,
  onSuccess,
}) => {
  const queryClient = useQueryClient();
  const [dialogState, dispatch] = useReducer(baselineDialogReducer, initialBaselineDialogState);

  const targetSnapshotId = activeSnapshot?.snapshot_id ?? "";

  // Every fresh GET carries a sequence; a late response for an older request is dropped
  // even inside the same session.
  const seqRef = useRef(0);
  const refreshInFlightRef = useRef(false);
  const submitInFlightRef = useRef(false);

  const sessionRef = useRef<BaselineSession>({
    epoch: 0,
    agentId,
    environment,
    snapshotId: targetSnapshotId,
  });
  const sessionKey = `${open}|${agentId}|${environment}|${targetSnapshotId}`;
  const lastSessionKeyRef = useRef(sessionKey);
  if (lastSessionKeyRef.current !== sessionKey) {
    lastSessionKeyRef.current = sessionKey;
    sessionRef.current = {
      epoch: sessionRef.current.epoch + 1,
      agentId,
      environment,
      snapshotId: targetSnapshotId,
    };
    // The new session owns refreshing from here on: the old in-flight request is stale
    // bookkeeping, not a lock, and must not block this session's single fresh GET.
    refreshInFlightRef.current = false;
  }

  useEffect(() => {
    if (!open) {
      dispatch({ type: "RESET" });
    }
  }, [open]);

  const staleSession = useCallback(
    (session: BaselineSession, seq: number) =>
      session.epoch !== sessionRef.current.epoch || seq !== seqRef.current,
    [],
  );

  const invalidateBaselineKey = useCallback(
    (session: BaselineSession) => {
      queryClient.invalidateQueries({
        queryKey: queryKeys.baselines.detail(session.agentId, session.environment),
      });
    },
    [queryClient],
  );

  /**
   * The single recovery path after a 409 (and the retry button): one fresh GET for the
   * session's own agent/environment, verified before anything is cached or dispatched.
   */
  const handleRefreshBaseline = useCallback(async (): Promise<void> => {
    refreshInFlightRef.current = true;
    const session = sessionRef.current;
    const seq = ++seqRef.current;
    try {
      const res = await api.GET("/api/v1/agents/{agent_id}/baselines", {
        params: {
          path: { agent_id: session.agentId },
          query: { environment: session.environment },
        },
      });
      if (staleSession(session, seq)) return;

      if (res.error) {
        const status = res.response?.status;
        if (status === 404) {
          // The endpoint answers 404 only for a valid agent/environment without a
          // binding, so this is "no binding" — never a refresh failure and never a 403.
          queryClient.setQueryData(
            queryKeys.baselines.detail(session.agentId, session.environment),
            null,
          );
          dispatch({ type: "REFRESH_SUCCESS", newRevision: 0, snapshotId: null });
          return;
        }
        dispatch({
          type: "REFRESH_FAILURE",
          error:
            status === 403
              ? "无权访问该 Agent 的 Baseline（HTTP 403）"
              : formatApiError(res.error),
        });
        invalidateBaselineKey(session);
        return;
      }

      const data = res.data as BaselineResponseData | undefined;
      const identityMatches =
        !!data &&
        typeof data === "object" &&
        typeof data.environment === "string" &&
        data.environment === session.environment &&
        (data.agent_id == null || data.agent_id === session.agentId) &&
        Number.isInteger(data.revision) &&
        data.revision >= 0 &&
        (data.revision === 0 ||
          (typeof data.result_snapshot_id === "string" && data.result_snapshot_id.length > 0));
      if (!identityMatches) {
        // A response for another agent/environment must never reach the shared cache.
        dispatch({
          type: "REFRESH_FAILURE",
          error: "返回的 Baseline 与当前会话（Agent / 环境）不一致",
        });
        invalidateBaselineKey(session);
        return;
      }

      queryClient.setQueryData(
        queryKeys.baselines.detail(session.agentId, session.environment),
        data,
      );
      dispatch({
        type: "REFRESH_SUCCESS",
        newRevision: data.revision,
        snapshotId: data.result_snapshot_id ?? null,
      });
    } catch (err) {
      if (staleSession(session, seq)) return;
      dispatch({ type: "REFRESH_FAILURE", error: formatApiError(err) });
      invalidateBaselineKey(session);
    } finally {
      if (!staleSession(session, seq)) {
        refreshInFlightRef.current = false;
      }
    }
  }, [queryClient, staleSession]);

  const setBaselineMutation = useMutation({
    mutationFn: async (variables: { expectedRevision: number; epoch: number }) => {
      const session = sessionRef.current;
      if (!activeSnapshot || !session.agentId) {
        throw new Error("快照或 Agent ID 不存在");
      }
      // POST the binding the dialog currently displays, from the session that owns it.
      const res = await api.POST("/api/v1/agents/{agent_id}/baselines", {
        params: { path: { agent_id: session.agentId } },
        body: {
          environment: session.environment,
          result_snapshot_id: session.snapshotId,
          expected_revision: variables.expectedRevision,
        },
      });

      if (res.error) {
        if (res.response?.status === 409) {
          dispatch({ type: "CONFLICT_409", currentRevision: variables.expectedRevision });
          void handleRefreshBaseline();
          const cErr: any = new Error(CONFLICT_MESSAGE);
          cErr.isConflict = true;
          throw cErr;
        }
        throw res.error;
      }
      return res.data;
    },
    onMutate: () => {
      dispatch({ type: "SUBMIT_START" });
    },
    onSuccess: (_data, variables) => {
      const session = sessionRef.current;
      if (variables.epoch !== session.epoch) return; // superseded session
      queryClient.invalidateQueries({
        queryKey: queryKeys.baselines.detail(session.agentId, session.environment),
      });
      queryClient.invalidateQueries({ queryKey: queryKeys.launches.all });
      onSuccess?.();
      onClose();
    },
    onError: (err: any, variables) => {
      if (variables?.epoch !== sessionRef.current.epoch) return; // superseded session
      if (err?.isConflict) {
        // Already handled with CONFLICT_409 inside mutationFn.
        return;
      }
      dispatch({ type: "SUBMIT_FAILURE", error: formatApiError(err) });
    },
  });

  const handleConfirm = () => {
    // Synchronous re-entry guard: two clicks inside one React batch must not POST twice.
    if (submitInFlightRef.current || setBaselineMutation.isPending) return;
    submitInFlightRef.current = true;
    const expectedRevision = dialogState.displayedRevision ?? activeBaseline?.revision ?? 0;
    setBaselineMutation.mutate(
      { expectedRevision, epoch: sessionRef.current.epoch },
      { onSettled: () => { submitInFlightRef.current = false; } },
    );
  };

  const handleRetryRefresh = () => {
    if (refreshInFlightRef.current) return;
    dispatch({ type: "REFRESH_RETRY" });
    void handleRefreshBaseline();
  };

  if (!open) return null;

  const displayedRevision =
    dialogState.displayedRevision ?? activeBaseline?.revision ?? null;
  const displayedSnapshotId =
    dialogState.displayedRevision != null
      ? dialogState.displayedSnapshotId
      : (activeBaseline?.result_snapshot_id ?? null);
  const hasDisplayedBinding = displayedSnapshotId != null;

  const confirmDisabled = isConfirmDisabled(
    dialogState.stage,
    Boolean(activeSnapshot),
    setBaselineMutation.isPending,
  );

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="固化设为新 Baseline (Set as Baseline)"
      tone="neutral"
      dismissable={dialogState.stage !== "submitting" && !setBaselineMutation.isPending}
      footer={
        <>
          <Button
            type="button"
            variant="secondary"
            className="text-xs"
            onClick={onClose}
            disabled={dialogState.stage === "submitting" || setBaselineMutation.isPending}
          >
            取消
          </Button>
          <Button
            type="button"
            variant="primary"
            className="text-xs font-semibold"
            disabled={confirmDisabled}
            onClick={handleConfirm}
          >
            {dialogState.stage === "submitting" || setBaselineMutation.isPending
              ? "正在固化绑定..."
              : "确认设为 Baseline"}
          </Button>
        </>
      }
    >
      <div className="p-6 space-y-4 text-xs">
        <p className="text-muted-foreground leading-relaxed">
          将当前 Candidate 结果快照设为 <strong className="text-foreground">{environment}</strong> 环境的正式基准 Baseline。
          后续所有 Candidate 评测均将以该固定版本为依据进行回归与能力跃升对比。
        </p>

        <div className="p-3 bg-surface-subtle border border-border rounded-xl space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-muted-foreground">目标环境 (Environment):</span>
            <span className="font-semibold text-foreground">{environment}</span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-muted-foreground">目标结果版本:</span>
            <span className="font-semibold text-foreground">
              Revision {activeSnapshot?.revision} ({activeSnapshot?.snapshot_id?.slice(0, 12)}…)
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-muted-foreground">当前绑定的 Baseline:</span>
            <span className="font-mono text-muted-foreground">
              {hasDisplayedBinding && displayedRevision != null
                ? `Revision ${displayedRevision} (Snapshot: ${displayedSnapshotId.slice(0, 8)}…)`
                : "尚未绑定任何 Baseline"}
            </span>
          </div>
        </div>

        {activeSnapshot?.evidence_state !== "COMPLETE" && (
          <div className="p-3 bg-timeout-subtle border border-timeout-border rounded-xl text-timeout flex items-start gap-2">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <div>
              <strong className="block">警告：当前快照证据不完整</strong>
              <span>该版本的证据状态为 DIAGNOSTIC，设为正式 Baseline 可能会影响后续发布的对比严肃性。</span>
            </div>
          </div>
        )}

        {/* 409 冲突提示 */}
        {dialogState.conflictNotice && (
          <div className="p-3 bg-fail-subtle border border-fail-border rounded-xl text-fail flex items-start gap-2">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <div>
              <p>{dialogState.conflictNotice}</p>
              {dialogState.stage === "refreshing" && (
                <p className="text-2xs text-muted-foreground flex items-center gap-1 mt-1">
                  <RefreshCw className="w-3 h-3 animate-spin" />
                  正在刷新最新状态...
                </p>
              )}
              {dialogState.stage === "ready_for_reconfirmation" && displayedRevision != null && (
                <p className="text-2xs text-muted-foreground mt-1">
                  已刷新到 Revision {displayedRevision}，请二次确认后重新提交。
                </p>
              )}
            </div>
          </div>
        )}

        {/* 普通提交失败信息 */}
        {dialogState.stage === "submit_failed" && dialogState.errorMessage && (
          <div className="p-3 bg-fail-subtle border border-fail-border rounded-xl text-fail flex items-start gap-2">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <p>{dialogState.errorMessage}</p>
          </div>
        )}

        {/* 刷新失败信息 */}
        {dialogState.stage === "refresh_failed" && (
          <div className="p-3 bg-fail-subtle border border-fail-border rounded-xl text-fail flex flex-col gap-2" role="alert">
            <div className="flex items-start gap-2">
              <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
              <p>获取最新 Baseline 绑定版本失败{dialogState.errorMessage ? `: ${dialogState.errorMessage}` : ""}，请重试或取消退出。</p>
            </div>
            <Button
              type="button"
              variant="secondary"
              className="h-6 text-2xs px-2 self-start"
              onClick={handleRetryRefresh}
            >
              重试获取最新状态
            </Button>
          </div>
        )}
      </div>
    </Modal>
  );
};
