import React from "react";
import { useQuery } from "@tanstack/react-query";
import { formatApiError } from "../../api/errors";
import { caseOutputQueryOptions } from "./launchComparisonQueries";
import { Button } from "../../components/ui/Primitives";
import { SideDrawer } from "../../components/ui/Overlay";
import { JsonViewer } from "../../components/JsonViewer";

type ComparisonCaseOutput = import("../../api/schema").components["schemas"]["ComparisonCaseOutputResponse"];
type OutputSide = ComparisonCaseOutput["baseline"];

const reasonTextMap: Record<string, string> = {
  UPSTREAM_ERROR: "远程追踪记录加载超时，请点击重试",
  OBSERVATION_NOT_FOUND: "远程 Observation 记录不存在或已清理",
  LANGFUSE_UNAVAILABLE: "Langfuse 观测服务暂不可用",
  NO_OUTPUT_REFERENCE: "未记录输出引用",
};

const OutputPanel: React.FC<{ title: string; side: OutputSide }> = ({ title, side }) => (
  <section aria-label={title} className="min-w-0 space-y-2 rounded-lg border border-border p-3">
    <div className="flex items-center justify-between gap-2">
      <h3 className="font-semibold">{title}</h3>
      <span className="rounded bg-canvas px-2 py-1 text-micro">{side.output_status}</span>
    </div>
    {side.reason && (
      <div className="space-y-0.5">
        <p className="text-xs text-muted-foreground">{side.reason}</p>
        {reasonTextMap[side.reason] && (
          <p className="text-micro text-fail/80">{reasonTextMap[side.reason]}</p>
        )}
      </div>
    )}
    {side.truncated && <p className="text-xs text-timeout">输出过大，已截断显示。</p>}
    {side.output_status === "AVAILABLE" && <JsonViewer data={side.output} title={`${title} Agent Output`} />}
    <JsonViewer data={side.scores ?? {}} title={`${title} Scores`} />
    {side.trace_url && <a className="inline-block text-xs text-primary hover:underline" href={side.trace_url} target="_blank" rel="noreferrer">打开 Trace</a>}
  </section>
);

export const ComparisonCaseDrawer: React.FC<{
  launchId: string;
  snapshotId: string;
  datasetItemId: string;
  onClose: () => void;
}> = ({ launchId, snapshotId, datasetItemId, onClose }) => {
  // Shared validated entry with the inline accordion: same key, same fetch, same
  // identity check, so a bypassing consumer cannot place a foreign payload in the
  // common success cache (issue 119 gap U1).
  const query = useQuery(caseOutputQueryOptions({ launchId, snapshotId, datasetItemId }));

  return (
    <SideDrawer
      open={Boolean(datasetItemId)}
      onClose={onClose}
      title="Case 双侧结果"
      subtitle={`${datasetItemId} · Snapshot ${snapshotId}`}
      className="max-w-table-xl"
    >
      <div className="space-y-4 p-5">
        {query.isPending && <p role="status" className="text-sm text-muted-foreground">正在读取冻结 Observation…</p>}
        {query.error && <div role="alert" className="space-y-2 text-sm text-fail">
          <p>读取 Case 详情失败：{formatApiError(query.error)}</p>
          <Button variant="secondary" className="text-xs" onClick={() => query.refetch()} disabled={query.isFetching}>重试</Button>
        </div>}
        {query.data && <>
          <p className="text-sm">{query.data.classification} · {query.data.reason ?? "—"}</p>
          <div className="grid gap-3 md:grid-cols-2">
            <OutputPanel title="Baseline" side={query.data.baseline} />
            <OutputPanel title="Candidate" side={query.data.candidate} />
          </div>
          {(query.data.baseline.retryable || query.data.candidate.retryable) && (
            <Button variant="secondary" className="text-xs" onClick={() => query.refetch()} disabled={query.isFetching}>重试读取不可用输出</Button>
          )}
        </>}
      </div>
    </SideDrawer>
  );
};
