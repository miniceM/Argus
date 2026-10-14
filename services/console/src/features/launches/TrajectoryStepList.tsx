import React, { useEffect, useRef } from "react";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/ui/Primitives";

type Diagnosis = import("../../api/schema").components["schemas"]["TrajectoryDiagnosisResponse"];
type Step = NonNullable<Diagnosis["trajectory"]>["steps"][number];

/** 只按显式因果边分层；同层没有已证先后，index 仅用于展示定位。 */
function causalGroups(steps: Step[]) {
  const remaining = new Set(steps.map(step => step.step_id));
  const groups: { steps: Step[]; unresolved: boolean }[] = [];
  while (remaining.size) {
    const next = steps.filter(step => remaining.has(step.step_id)
      && (step.causal_predecessor_ids ?? []).every(id => !remaining.has(id) && steps.some(s => s.step_id === id)));
    if (!next.length) {
      groups.push({ steps: steps.filter(step => remaining.has(step.step_id)), unresolved: true });
      break;
    }
    groups.push({ steps: next, unresolved: false });
    next.forEach(step => remaining.delete(step.step_id));
  }
  return groups;
}

export const TrajectoryStepList: React.FC<{
  diagnosis: Diagnosis;
  selectedStep: { id: string } | null;
  onLocateStep: (id: string) => void;
  onLocateRule: (id: string) => void;
}> = ({ diagnosis, selectedStep, onLocateStep, onLocateRule }) => {
  const refs = useRef(new Map<string, HTMLElement>());
  const selectedStepId = selectedStep?.id ?? null;
  useEffect(() => {
    const target = selectedStep ? refs.current.get(selectedStep.id) : null;
    target?.focus();
    target?.scrollIntoView?.({ block: "nearest" });
  }, [selectedStep]);
  if (!diagnosis.trajectory) return null;
  const groups = causalGroups(diagnosis.trajectory.steps);
  return <section aria-label="冻结轨迹 Step" className="space-y-3">
    <h4 className="font-semibold text-foreground">冻结 Step 与因果分组</h4>
    <p className="text-xs text-muted-foreground">按显式因果边分层；同层不宣称时间先后，编号仅用于定位。</p>
    {groups.map((group, index) => <section key={index} aria-label={`因果层 ${index + 1}`} className="space-y-2">
      <p className="text-xs text-muted-foreground">{group.unresolved ? "因果关系不足，不能确定分组" : `因果层 ${index + 1} · 同层 / 无已证先后`}</p>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {group.steps.map(step => <article key={step.step_id} tabIndex={-1} data-testid={`trajectory-step-${step.step_id}`}
          ref={element => { if (element) refs.current.set(step.step_id, element); else refs.current.delete(step.step_id); }}
          className={`min-w-0 space-y-2 rounded-lg border p-3 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus ${selectedStepId === step.step_id ? "border-primary bg-primary-subtle" : "border-border bg-surface"}`}>
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="quiet" className="text-xs font-mono" onClick={() => onLocateStep(step.step_id)} aria-label={`定位 Step ${step.step_id}`}>
              Step {step.index} · {step.step_id}
            </Button>
            <Badge tone="neutral">{step.type}</Badge><Badge tone={step.phase === "BLOCKED" ? "timeout" : "neutral"}>{step.phase}</Badge>
          </div>
          <p className="break-words font-mono text-xs text-foreground">{step.raw_name}</p>
          <p className="text-xs text-muted-foreground">工具：{step.tool_identity ?? "—"} · 执行：{step.status}</p>
          {step.error_code && <p className="break-words text-xs text-timeout">{step.error_code}</p>}
          <p className="text-micro text-muted-foreground">父节点：{step.parent_step_id ?? "—"} · 耗时：{step.duration_ms ?? "—"} ms</p>
          <div className="flex flex-wrap items-center gap-1 text-micro">
            <span>可信前置：</span>{(step.causal_predecessor_ids ?? []).length === 0 ? <span>无已证前置</span>
              : step.causal_predecessor_ids?.map(id => <Button key={id} variant="quiet" className="text-micro" onClick={() => onLocateStep(id)}>{id}</Button>)}
          </div>
          <details className="text-xs">
            <summary className="cursor-pointer text-foreground">获准参数 JSON</summary>
            <pre className="mt-2 overflow-x-auto rounded bg-surface-subtle p-2 font-mono text-micro text-foreground">{diagnosis.step_argument_display_json[step.step_id]}</pre>
            <div className="mt-2 space-y-1 text-micro text-muted-foreground">
              {Object.entries(step.argument_visibility).map(([path, visibility]) => <p key={path}>{path}：{visibility}</p>)}
            </div>
          </details>
          <div className="flex flex-wrap items-center gap-1 text-micro">
            <span>命中规则：</span>{diagnosis.rule_results.filter(rule => rule.evidence_step_ids.includes(step.step_id)).map(rule =>
              <Button key={rule.evaluator_id} variant="quiet" className="text-micro" aria-label={`返回规则 ${rule.evaluator_id}`}
                onClick={() => onLocateRule(rule.evaluator_id)}>{rule.evaluator_id}</Button>)}
          </div>
        </article>)}
      </div>
    </section>)}
  </section>;
};
