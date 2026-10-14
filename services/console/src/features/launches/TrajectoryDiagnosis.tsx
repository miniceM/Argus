import React, { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { formatApiError } from "../../api/errors";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/ui/Primitives";
import { trajectoryQueryOptions } from "./trajectoryQueries";
import { TrajectoryStepList } from "./TrajectoryStepList";

const labels = { pass: "通过 PASS", fail: "违规 FAIL", unknown: "证据不足 UNKNOWN" } as const;
const statuses = { succeeded: "测量成功", failed: "测量失败", skipped: "已跳过", no_result: "无测量结果" } as const;

export const TrajectoryDiagnosis: React.FC<{ launchId: string; snapshotId: string | null; datasetItemId: string }> = props => {
  const query = useQuery(trajectoryQueryOptions(props));
  const [selectedStep, setSelectedStep] = useState<{ id: string } | null>(null);
  const ruleRefs = useRef(new Map<string, HTMLElement>());
  const locateStep = (id: string) => setSelectedStep({ id });
  const locateRule = (id: string) => {
    const target = ruleRefs.current.get(id);
    target?.focus();
    target?.scrollIntoView?.({ block: "nearest" });
  };
  if (!props.snapshotId) return <p className="text-xs text-muted-foreground">尚无固定 Snapshot，轨迹诊断待冻结后可用。</p>;
  const data = query.data;
  return <section aria-label="固定轨迹诊断" className="space-y-3 rounded-lg border border-border bg-surface-subtle p-3">
    <h3 className="font-semibold text-foreground">固定轨迹诊断</h3>
    <p className="break-words font-mono text-micro text-muted-foreground">Snapshot {props.snapshotId} · Case {props.datasetItemId}</p>
    {query.isPending && <p role="status" className="text-xs text-muted-foreground">正在读取冻结轨迹证据...</p>}
    {query.error && <div role="alert" className="space-y-2">
      <p className="text-xs text-fail">轨迹诊断读取失败：{formatApiError(query.error)}</p>
      <Button variant="secondary" onClick={() => query.refetch()}>重试读取轨迹诊断</Button>
    </div>}
    {data && <>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span>执行状态：</span><Badge tone="neutral">{data.execution_status}</Badge>
        <span>整体质量（Snapshot）：</span><Badge tone="neutral">{data.quality_conclusion.toUpperCase()}</Badge>
        <span>采集：</span><Badge tone="neutral">{data.acquisition_state}</Badge>
        <span>证据完整度：</span><Badge tone="neutral">{data.completeness?.status ?? "不可用"}</Badge>
        {data.required_evidence_status && <span>冻结时 required：{data.required_evidence_status}</span>}
      </div>
      {(data.reason_code || data.completeness?.status !== "OBSERVED_COMPLETE") && <div role="status" className="space-y-1 rounded border border-timeout-border bg-timeout-subtle p-3 text-xs text-timeout">
        <p>轨迹证据不完整或不可用；答案规则仍按冻结策略评测，已证违规保留逐规则 FAIL。</p>
        <p className="break-words font-mono">{data.reason_code ?? data.completeness?.warnings.join(" · ") ?? "TRAJECTORY_UNAVAILABLE"}</p>
      </div>}
      <div className="space-y-2">
        <h4 className="font-semibold text-foreground">冻结规则与测量</h4>
        {data.rule_results.length === 0 && <p className="text-xs text-muted-foreground">此 Snapshot 没有可用的轨迹规则记录。</p>}
        {data.rule_results.map(rule => <section key={rule.binding_id ?? rule.evaluator_id} role="region" tabIndex={-1}
          aria-label={`轨迹规则 ${rule.evaluator_id}`} ref={element => { if (element) ruleRefs.current.set(rule.evaluator_id, element); else ruleRefs.current.delete(rule.evaluator_id); }}
          className="space-y-2 rounded border border-border bg-surface p-3 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus">
          <div className="flex flex-wrap items-center gap-2">
            <span className="break-words font-mono text-xs text-foreground">{rule.evaluator_id} @{rule.evaluator_version}</span>
            <Badge tone={rule.conclusion === "pass" ? "pass" : rule.conclusion === "fail" ? "fail" : "timeout"}>{labels[rule.conclusion]}</Badge>
            <Badge tone="neutral">{statuses[rule.typed_status]}</Badge>
            <span className="text-micro">{rule.required ? "必要" : "可选"}{rule.critical ? " · critical" : ""}</span>
          </div>
          <p className="text-xs">测量值：<code>{rule.value == null ? "—" : String(rule.value)}</code></p>
          {rule.reason_code && <p className="break-words font-mono text-micro text-muted-foreground">{rule.reason_code}</p>}
          {rule.frozen_conclusion !== rule.conclusion && <p className="text-micro text-muted-foreground">冻结时结论：{labels[rule.frozen_conclusion]}；当前证据无法复核。</p>}
          <div className="flex flex-wrap gap-2">
            {rule.evidence_step_ids.map(id => <Button key={id} variant="secondary" className="text-xs"
              aria-label={`定位 ${rule.evaluator_id} 的证据 Step ${id}`} onClick={() => locateStep(id)}>定位 Step {id}</Button>)}
          </div>
        </section>)}
      </div>
      <TrajectoryStepList diagnosis={data} selectedStep={selectedStep} onLocateStep={locateStep} onLocateRule={locateRule} />
      {data.langfuse_trace_url && <p className="text-xs text-muted-foreground">
        <a href={data.langfuse_trace_url} target="_blank" rel="noopener noreferrer" className="text-primary underline">查看 Langfuse 在线观测内容</a>
        <span> · 在线内容可能变化，不参与此 Snapshot 的冻结结论。</span>
      </p>}
    </>}
  </section>;
};
