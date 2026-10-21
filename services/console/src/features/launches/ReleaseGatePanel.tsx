import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "../../api/schema";
import { api } from "../../api/client";
import { formatApiError } from "../../api/errors";
import { Badge } from "../../components/Badge";
import { Button, Field, TextInput } from "../../components/ui/Primitives";

type Gate = components["schemas"]["ReleaseGateResponse"];
function verifyGate(value: Gate | undefined, launchId: string, snapshotId: string | null): Gate {
  if (!value || value.candidate_launch_id !== launchId || value.candidate_snapshot_id !== snapshotId) {
    throw new Error("Gate 与当前 Launch / Snapshot 不一致");
  }
  if (!value.id || typeof value.releasable !== "boolean" || !value.policy || typeof value.policy.name !== "string"
    || typeof value.policy.version !== "string" || !["PASS", "FAIL", "UNKNOWN"].includes(value.decision) || !Array.isArray(value.rules)
    || !Array.isArray(value.reason_codes)) throw new Error("Gate 响应格式无效");
  return value;
}

/** 复用既有 Gate API；只计算/保存门禁证据，不执行部署，也不编辑 ReleasePolicy。 */
export function ReleaseGatePanel({ launchId, snapshotId }: { launchId: string; snapshotId: string | null }) {
  const [params, setParams] = useSearchParams();
  const [name, setName] = useState("");
  const [version, setVersion] = useState("");
  const client = useQueryClient();
  const gateId = params.get("gate_id");
  const identity = `${launchId}::${snapshotId}`;
  const current = useRef(identity);
  current.current = identity;
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const key = (id: string) => ["release-gate", launchId, snapshotId, id];
  const saved = useQuery({
    queryKey: key(gateId ?? ""), enabled: Boolean(snapshotId && gateId), retry: false,
    queryFn: async () => {
      const res = await api.GET("/api/v1/release-gates/{gate_id}", { params: { path: { gate_id: gateId! } } });
      if (res.error) throw res.error;
      const result = verifyGate(res.data, launchId, snapshotId);
      if (result.id !== gateId) throw new Error("Gate ID 与请求不一致");
      return result;
    },
  });
  const evaluate = useMutation({
    mutationFn: async () => {
      if (!snapshotId) throw new Error("等待 Frozen Snapshot");
      const res = await api.POST("/api/v1/release-gates/evaluate", { body: {
        policy_name: name.trim(), policy_version: version.trim(), candidate_launch_id: launchId, candidate_snapshot_id: snapshotId,
      } });
      if (res.error) throw res.error;
      return { gate: verifyGate(res.data, launchId, snapshotId), identity };
    },
    onSuccess: ({ gate, identity: submitted }) => {
      if (!mounted.current || current.current !== submitted) return;
      client.setQueryData(key(gate.id), gate);
      setParams((previous) => { const next = new URLSearchParams(previous); next.set("gate_id", gate.id); return next; });
    },
  });
  const gate = saved.data;
  const error = saved.error ?? evaluate.error;
  const href = gate ? `/launches/${encodeURIComponent(launchId)}?${new URLSearchParams({ snapshot_id: snapshotId!, tab: "compare", gate_id: gate.id })}` : "";
  return <section aria-label="Release Gate" className="rounded-xl border border-border bg-surface p-4 space-y-3">
    <h3 className="text-sm font-semibold">Release Gate（固定证据）</h3>
    <p className="text-xs text-muted-foreground">选择已有 ReleasePolicy 的名称与版本，基于当前 Frozen Snapshot 计算并保存发布结论。不会重新调用 Agent、读取 Langfuse latest 或执行部署；策略由既有发布流程管理。</p>
    {!snapshotId && <p className="text-xs text-timeout">等待 Frozen Snapshot 后才能计算 Gate。诊断快照或缺失证据不能发布 PASS。</p>}
    <form className="flex flex-wrap items-end gap-3" onSubmit={(event) => { event.preventDefault(); if (!evaluate.isPending && snapshotId && name.trim() && version.trim()) evaluate.mutate(); }}>
      <Field label="ReleasePolicy 名称">{(field) => <TextInput id={field.id} value={name} maxLength={128} onChange={(event) => setName(event.target.value)} />}</Field>
      <Field label="ReleasePolicy 版本">{(field) => <TextInput id={field.id} value={version} maxLength={64} onChange={(event) => setVersion(event.target.value)} />}</Field>
      <Button type="submit" disabled={!snapshotId || !name.trim() || !version.trim() || evaluate.isPending}>{evaluate.isPending ? "正在计算 Gate…" : "计算并保存 Gate"}</Button>
    </form>
    {saved.isFetching && <p role="status" className="text-xs">正在读取固定 Gate…</p>}
    {error && <div role="alert" className="text-xs text-fail">{formatApiError(error)}{saved.isError && <Button variant="secondary" onClick={() => void saved.refetch()}>重试读取 Gate</Button>}</div>}
    {gate && <div className="space-y-2 text-xs break-all" data-testid="release-gate-result">
      <Badge tone={gate.decision === "PASS" && gate.releasable ? "pass" : gate.decision === "FAIL" ? "fail" : "neutral"}>{gate.decision} · {gate.decision === "PASS" && gate.releasable ? "可发布" : "不可发布"}</Badge>
      <p>Gate ID：{gate.id}；Snapshot：{gate.candidate_snapshot_id} · revision {gate.candidate_revision}</p>
      <p>ReleasePolicy：{gate.policy.name}@{gate.policy.version}；digest：{gate.policy_digest}</p>
      <p>结果 digest：{gate.candidate_source_result_digest}</p>
      <p>原因：{gate.reason_codes.join("；") || "无阻断原因"}</p>
      <ul className="space-y-1">{gate.rules.map((rule) => <li key={rule.id}>{rule.id}：{rule.conclusion}；实际 {rule.actual ?? "未记录"}；{rule.reason}</li>)}</ul>
      <Link to={href} className="inline-block text-primary underline">固定 Gate 报告</Link>
    </div>}
  </section>;
}
