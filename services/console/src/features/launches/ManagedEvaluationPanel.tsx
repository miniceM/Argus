import { useQuery } from "@tanstack/react-query";
import type { components } from "../../api/schema";
import { api } from "../../api/client";
import { Badge } from "../../components/Badge";
import { Button, Panel } from "../../components/ui/Primitives";
import { EvaluationResultList } from "./EvaluationResultList";
import { isSafeLangfuseUrl } from "./langfuseLink";

type Schemas = components["schemas"];
type Item = Partial<Schemas["ExperimentItemExecutionResponse"]>;
type Manifest = Record<string, unknown>;
export const MANAGED_LATEST_NOTICE = "Langfuse Evaluator / Rule 遵循当前 latest；远程执行实现并未冻结。Argus 仅冻结本次实际采纳的 Score、采纳条件与质量判断规则。";

export function managedContract(manifest: Manifest): Schemas["ManagedEvaluationContract"] | null {
  if (manifest.evaluation_source !== "langfuse_managed") return null;
  const value = manifest.managed_evaluation;
  if (!value || typeof value !== "object" || !("scores" in value) || !Array.isArray(value.scores)
    || value.scores.length === 0 || value.scores.some((score: unknown) => {
      if (!score || typeof score !== "object") return true;
      const row = score as Record<string, unknown>;
      return typeof row.name !== "string" || !row.name.trim() || typeof row.rule_id !== "string"
        || !row.rule_id.trim() || !["boolean", "numeric", "categorical", "text"].includes(String(row.data_type));
    })) return null;
  return value as Schemas["ManagedEvaluationContract"];
}

export function isItemEvaluationActive(item: Item): boolean {
  return (item.execution_status?.toLowerCase() === "succeeded" && item.eval_status?.toLowerCase() === "pending")
    || item.evaluation_status?.toLowerCase() === "evaluating";
}

/** 只展示服务端已核验的采纳结果，绝不依据 Score 名称推断来源。 */
export function isAdoptedManagedResult(result: Schemas["EvaluationResultResponse"]): boolean {
  return result.status === "succeeded" && result.provenance?.executor_type === "langfuse_managed"
    && result.provenance.contract_status === "MANAGED_SCORE_VERIFIED";
}

export function ManagedEvaluationNotice({ manifest }: { manifest: Manifest }) {
  return <section aria-label="评测来源" className="rounded-xl border border-border bg-surface p-4 space-y-2">
    <Badge tone="neutral">{manifest.evaluation_source === "langfuse_managed" ? "Langfuse Managed" : Object.keys(manifest).length ? "Builtin" : "评分来源未记录"}</Badge>
    <p className="text-xs text-muted-foreground">{manifest.evaluation_source === "langfuse_managed" ? MANAGED_LATEST_NOTICE : Object.keys(manifest).length ? "Builtin 在 Argus 内执行冻结的评测版本；无需 Langfuse 托管评分配置。" : "当前报告尚未提供可信的 Manifest；请等待加载或重试，不能推断评分来源。"}</p>
    {manifest.evaluation_source === "langfuse_managed" && <p className="text-xs text-muted-foreground">Agent 执行、Evaluation、质量结论和 OTLP 同步互相独立。等待中会自动收集评分；异常修复后使用“仅重试评测”，不会再次调用 Agent。展开用例查看必选 Score 和采纳证据。</p>}
  </section>;
}

export function ManagedEvaluationPanel({ launchId, snapshotId, manifest, item }: {
  launchId: string; snapshotId: string | null; manifest: Manifest; item: Item;
}) {
  const contract = managedContract(manifest);
  const live = Boolean(contract && !snapshotId && item.id);
  const diagnostics = useQuery({
    queryKey: ["managed-score-diagnostics", launchId, item.id],
    enabled: live,
    queryFn: async () => {
      const res = await api.GET("/api/v1/experiment-launches/{launch_id}/items/{item_id}/managed-scores", {
        params: { path: { launch_id: launchId, item_id: item.id! } },
      });
      if (res.error) throw new Error("托管评分诊断读取失败");
      if (!res.data || res.data.launch_id !== launchId || res.data.item_execution_id !== item.id
        || !Array.isArray(res.data.evidence)) throw new Error("托管评分诊断归属或格式无效");
      return res.data;
    },
    refetchInterval: live && isItemEvaluationActive(item) ? 1500 : false,
    retry: false,
  });
  if (!contract) return manifest.evaluation_source === "langfuse_managed"
    ? <p role="alert" className="text-xs text-timeout">Managed 采纳契约缺失或格式无效，不能确认必要评分覆盖率；请检查当前冻结报告。</p> : null;
  // frozen 视图只读所选 Snapshot 的 Typed Results，不读取可变诊断或远端 latest。
  const results = item.evaluation_results ?? [];
  const required = contract.scores.filter((s) => s.required !== false);
  const adopted = (name: string) => results.find((r) => r.evaluator_id === name && isAdoptedManagedResult(r));
  const received = required.filter((s) => adopted(s.name)).length;
  const pending = isItemEvaluationActive(item);
  const failed = item.eval_status?.toLowerCase() === "failed";
  return <Panel title="Langfuse Managed Score" className="p-4 min-w-0" >
    <div className="space-y-3 break-words" role="region" aria-label="托管评分证据">
      <h3 className="text-sm font-semibold">Langfuse Managed Score</h3>
      <div className="flex flex-wrap gap-2" aria-live="polite">
        <Badge tone="neutral">Agent 执行：{item.execution_status ?? "未知"}</Badge>
        <Badge tone={pending ? "queued" : failed ? "fail" : item.eval_status === "succeeded" ? "pass" : "neutral"}>
          {pending ? "等待托管评分" : failed ? "评分失败" : item.eval_status === "succeeded" ? "评分完成" : "评分状态未知"}
        </Badge>
        <Badge tone={item.quality_conclusion === "pass" ? "pass" : item.quality_conclusion === "fail" ? "fail" : "neutral"}>质量结论：{(item.quality_conclusion ?? "unknown").toUpperCase()}</Badge>
      </div>
      <p className="text-sm font-semibold">必要评分 {received} / {required.length} 已采纳</p>
      <p className="text-xs text-muted-foreground">{snapshotId ? "历史冻结证据（只读）；远程后续重评不会改变本报告。" : "实时采纳进度；等待时自动刷新。"}缺少必要评分保持 UNKNOWN，不作为零分或质量 FAIL。</p>
      {(item.eval_error || item.evaluation_error) && <p className="text-xs text-fail">{item.eval_error || item.evaluation_error}</p>}
      {diagnostics.isError && live && <div role="alert" className="text-xs text-fail">诊断暂不可用，未将读取失败当作零分。<Button variant="secondary" onClick={() => void diagnostics.refetch()}>重试诊断</Button></div>}
      {contract.scores.map((score) => {
        const result = adopted(score.name);
        const audit = live ? diagnostics.data?.evidence.filter((e) => e.score_name === score.name && e.decision !== "ADOPTED") : [];
        const href = result?.evidence?.score_url;
        const raw = result?.evidence?.score as Record<string, unknown> | undefined;
        const displayResult = result && !isSafeLangfuseUrl(href) && href
          ? { ...result, evidence: { ...result.evidence, score_url: "[链接无效，未展示]" } } : result;
        return <section key={score.name} aria-label={`Score ${score.name}`} className="rounded-lg border border-border p-3 space-y-2 min-w-0">
          <div className="flex flex-wrap gap-2"><strong className="text-sm break-all">{score.name}</strong><Badge tone="neutral">{score.data_type}</Badge><Badge tone="neutral">{score.required === false ? "可选证据" : "必要评分"}</Badge></div>
          <p className="text-xs text-muted-foreground break-all">Rule：{score.rule_id}；Evaluator：{score.evaluator_id || "未指定外部身份"}</p>
          {result ? <>
            <EvaluationResultList results={[displayResult!]} variant="detail" />
            <p className="text-xs font-mono break-all">source={String(raw?.source ?? "未记录")}；Score ID：{String(raw?.id ?? "未记录")}；dataType={String(raw?.dataType ?? "未记录")}</p>
            {isSafeLangfuseUrl(href) ? <a href={href} target="_blank" rel="noopener noreferrer" className="inline-block text-xs text-primary underline">查看 Score 所在 Observation（Langfuse）</a> : <p className="text-xs text-muted-foreground">未记录安全的外部 Observation 链接；不构造独立 Score URL。</p>}
          </> : <p className="text-xs text-timeout">SCORE_MISSING：尚无已核验的采纳结果。{pending ? "正在等待 Langfuse Rule 产生评分。" : "请检查 Rule 范围、source=EVAL、类型和身份归属；修复后仅重试评测。"}</p>}
          {audit && audit.length > 0 && <details className="text-xs"><summary className="cursor-pointer">未采纳诊断（{audit.length}）</summary><ul className="space-y-1 mt-2">{audit.map((e) => <li key={e.id} className="break-all">{e.decision}：{e.reason}{e.score_id ? `；Score ID：${e.score_id}` : ""}</li>)}</ul></details>}
        </section>;
      })}
      <p className="text-xs text-muted-foreground">{MANAGED_LATEST_NOTICE}</p>
    </div>
  </Panel>;
}
