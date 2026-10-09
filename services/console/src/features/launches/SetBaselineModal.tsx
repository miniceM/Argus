import React, { useState } from "react";
import { AlertCircle } from "lucide-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";
import { Button } from "../../components/ui/Primitives";
import { Modal } from "../../components/ui/Overlay";

type Baseline = import("../../api/schema").components["schemas"]["BaselineResponse"];
type SnapshotRevision = import("../../api/schema").components["schemas"]["ResultSnapshotRevisionResponse"];

interface SetBaselineModalProps {
  open: boolean;
  onClose: () => void;
  agentId: string;
  environment: string;
  activeSnapshot: SnapshotRevision | null;
  activeBaseline: Baseline | null;
  onSuccess?: () => void;
}

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
  const [conflictError, setConflictError] = useState<string | null>(null);

  const setBaselineMutation = useMutation({
    mutationFn: async () => {
      if (!activeSnapshot || !agentId) {
        throw new Error("快照或 Agent ID 不存在");
      }
      setConflictError(null);
      const res = await api.POST("/api/v1/agents/{agent_id}/baselines", {
        params: { path: { agent_id: agentId } },
        body: {
          environment,
          result_snapshot_id: activeSnapshot.snapshot_id,
          expected_revision: activeBaseline?.revision ?? 0,
        },
      });

      if (res.error) {
        if (res.response?.status === 409) {
          queryClient.invalidateQueries({
            queryKey: queryKeys.baselines.detail(agentId, environment),
          });
          throw new Error("Baseline 绑定版本发生并发冲突 (HTTP 409)，请重新确认最新状态后再试。");
        }
        throw res.error;
      }
      return res.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: queryKeys.baselines.detail(agentId, environment),
      });
      queryClient.invalidateQueries({
        queryKey: queryKeys.launches.all,
      });
      onSuccess?.();
      onClose();
    },
    onError: (err) => {
      setConflictError(formatApiError(err));
    },
  });

  if (!open) return null;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="固化设为新 Baseline (Set as Baseline)"
      tone="neutral"
      dismissable={!setBaselineMutation.isPending}
      footer={
        <>
          <Button
            type="button"
            variant="secondary"
            className="text-xs"
            onClick={onClose}
            disabled={setBaselineMutation.isPending}
          >
            取消
          </Button>
          <Button
            type="button"
            variant="primary"
            className="text-xs font-semibold"
            disabled={setBaselineMutation.isPending || !activeSnapshot}
            onClick={() => setBaselineMutation.mutate()}
          >
            {setBaselineMutation.isPending ? "正在固化绑定..." : "确认设为 Baseline"}
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
              {activeBaseline
                ? `Revision ${activeBaseline.revision} (Snapshot: ${activeBaseline.result_snapshot_id?.slice(0, 8)}…)`
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

        {conflictError && (
          <div className="p-3 bg-fail-subtle border border-fail-border rounded-xl text-fail flex items-start gap-2">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            <p>{conflictError}</p>
          </div>
        )}
      </div>
    </Modal>
  );
};
