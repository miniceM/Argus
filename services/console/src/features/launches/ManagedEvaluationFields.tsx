import { Button, Field, SelectInput, TextInput } from "../../components/ui/Primitives";
import type { components } from "../../api/schema";

type Contract = components["schemas"]["ManagedEvaluationContract"];
type Score = components["schemas"]["ManagedScoreExpectation"];
type Rule = components["schemas"]["QualityRuleRequest"];
export type ManagedScoreCandidate = components["schemas"]["ManagedScoreCandidateResponse"];
export type ManagedDraft = { contract: Contract; rules: Rule[] };

export const newManagedDraft = (): ManagedDraft => ({
  contract: { project_id: "", environment: "production", target: { name: "remote-agent-http", type: "TOOL" },
    scores: [{ name: "", data_type: "boolean", required: true, rule_id: "" }] },
  rules: [{ evaluator_id: "", result_type: "boolean", required: true, critical: false, operator: "==", expected_value: true }],
});

// 编辑中的名称可能暂时重复；按表单位置关联规则，不能按名称串用另一行的阈值。
const ruleIndex = (value: ManagedDraft, index: number) =>
  value.contract.scores.slice(0, index).filter((score) => score.data_type !== "text").length;
const ruleFor = (value: ManagedDraft, index: number) =>
  value.contract.scores[index].data_type === "text" ? undefined : value.rules[ruleIndex(value, index)];

export function ManagedEvaluationFields({
  value,
  onChange,
  boundProjectId,
  candidates,
  candidatesLoading,
  candidatesError,
}: {
  value: ManagedDraft;
  onChange: (next: ManagedDraft) => void;
  boundProjectId?: string | null;
  candidates?: ManagedScoreCandidate[];
  candidatesLoading?: boolean;
  candidatesError?: string | null;
}) {
  const update = (index: number, patch: Partial<Score>, rulePatch?: Partial<Rule>) => {
    const scores = value.contract.scores.map((s, i) => i === index ? { ...s, ...patch } : s);
    const rules = scores.flatMap((score, i) => {
      if (score.data_type === "text") return [];
      const oldScore = value.contract.scores[i];
      const previous = ruleFor(value, i);
      const changedType = oldScore.data_type !== score.data_type;
      const rule: Rule = !previous || changedType
        ? { evaluator_id: score.name, result_type: score.data_type, required: score.required, critical: false,
          operator: score.data_type === "numeric" ? ">=" : "==",
          ...(score.data_type === "numeric" ? { threshold: 1 } : { expected_value: score.data_type === "boolean" ? true : "" }) }
        : { ...previous, evaluator_id: score.name, required: score.required };
      return [{ ...rule, ...(i === index ? rulePatch : {}) }];
    });
    onChange({ contract: { ...value.contract, scores }, rules });
  };
  return <section className="space-y-4" aria-label="Langfuse Managed 配置">
    <p className="text-sm text-timeout" role="note">待收集评分，暂不可用于正式发布。执行成功不等于质量 PASS。</p>
    <p className="text-xs text-muted-foreground">只冻结 Score 采纳条件；Langfuse Rule 使用 Evaluator 当前 latest，远程执行实现并未冻结。不在 Argus 编辑或执行 Evaluator。</p>
    <p className="text-xs text-muted-foreground">Managed 需要服务端 Langfuse 凭据、OTLP 同步与既有 Evaluation Rule。不要在此粘贴密钥。未配置 Langfuse 时请选择 Builtin；不会阻塞原有内置评测。</p>
    <Field
      label="Langfuse Project ID"
      hint={boundProjectId ? "已自动跟随当前 Agent 绑定的 Langfuse Project ID，无需重复填写。" : undefined}
    >
      {(field) => (
        <TextInput
          id={field.id}
          aria-label="Langfuse Project ID"
          value={boundProjectId ?? value.contract.project_id}
          readOnly={Boolean(boundProjectId)}
          onChange={(e) => {
            if (boundProjectId) return;
            onChange({ ...value, contract: { ...value.contract, project_id: e.target.value } });
          }}
        />
      )}
    </Field>
    {candidatesError && (
      <p className="text-xs text-timeout" role="status">
        无法从 Langfuse 自动加载候选 Score（{candidatesError}），已切换为手动填写模式，不阻塞直接提交。
      </p>
    )}
    <p className="text-xs text-muted-foreground">目标 Observation：remote-agent-http / TOOL；source=EVAL；environment 与上方 Launch 环境相同。请从 Langfuse 候选中选择或手动填写已有的 Rule ID 和 Score 名称，引用不会在此创建新 Rule。</p>
    {value.contract.scores.map((score, index) => {
      const rule = ruleFor(value, index);
      const matchedCandidate = candidates?.find(
        (c) => c.rule_id === score.rule_id && (c.evaluator_id === (score.evaluator_id ?? "") || !score.evaluator_id),
      );
      const selectedKey = matchedCandidate ? `${matchedCandidate.rule_id}::${matchedCandidate.evaluator_id}` : "";
      return <fieldset key={index} className="space-y-3 rounded-lg border border-border p-4">
        <legend className="text-sm font-semibold">外部 Score {index + 1}</legend>
        <Field label={`从 Langfuse 选择 Score ${index + 1}`}>
          {(field) => (
            <SelectInput
              id={field.id}
              aria-label={`从 Langfuse 选择 Score ${index + 1}`}
              value={selectedKey}
              onChange={(e) => {
                const val = e.target.value;
                if (!val) return;
                const picked = candidates?.find((c) => `${c.rule_id}::${c.evaluator_id}` === val);
                if (!picked) return;
                const nextType = picked.data_type ?? score.data_type;
                update(index, {
                  name: picked.verified && picked.score_name ? picked.score_name : "",
                  data_type: nextType,
                  required: nextType === "text" ? false : score.required,
                  rule_id: picked.rule_id,
                  evaluator_id: picked.evaluator_id,
                });
              }}
            >
              <option value="">
                {candidatesLoading ? "正在从 Langfuse 加载可用 Rule..." : "手动填写 / 从 Langfuse 选择可用 Score..."}
              </option>
              {(candidates ?? []).map((c) => {
                const key = `${c.rule_id}::${c.evaluator_id}`;
                return (
                  <option key={key} value={key}>
                    {c.verified && c.score_name ? c.score_name : "（未验证名称）"} · {c.evaluator_name} ({c.evaluator_type}) · {c.rule_id}
                  </option>
                );
              })}
            </SelectInput>
          )}
        </Field>
        {matchedCandidate && !matchedCandidate.verified && (
          <p className="text-xs text-timeout" role="note">
            名称尚未被真实 Score 验证，请在下方手动确认并填写 Score 名称。
          </p>
        )}
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <Field label={`Score 名称 ${index + 1}`}>{(field) => <TextInput id={field.id} aria-label={`Score 名称 ${index + 1}`} value={score.name}
            onChange={(e) => update(index, { name: e.target.value })} />}</Field>
          <Field label={`Score 类型 ${index + 1}`}>{(field) => <SelectInput id={field.id} aria-label={`Score 类型 ${index + 1}`} value={score.data_type}
            onChange={(e) => update(index, { data_type: e.target.value, required: e.target.value === "text" ? false : score.required })}>
            <option value="boolean">boolean</option><option value="numeric">numeric</option>
            <option value="categorical">categorical</option><option value="text">text（仅可选证据）</option>
          </SelectInput>}</Field>
          <Field label={`Rule ID ${index + 1}`}>{(field) => <TextInput id={field.id} aria-label={`Rule ID ${index + 1}`} value={score.rule_id}
            onChange={(e) => update(index, { rule_id: e.target.value })} />}</Field>
          <Field label={`Evaluator ID ${index + 1}（可选）`}>{(field) => <TextInput id={field.id} aria-label={`Evaluator ID ${index + 1}`} value={score.evaluator_id ?? ""}
            onChange={(e) => update(index, { evaluator_id: e.target.value || undefined })} />}</Field>
        </div>
        <label className="flex items-center gap-2 text-sm"><input className="h-4 w-4 accent-primary" type="checkbox" checked={score.required ?? true}
          disabled={score.data_type === "text"} onChange={(e) => update(index, { required: e.target.checked })} />必要评分 {index + 1}</label>
        {score.data_type === "categorical" && <Field label={`分类取值 ${index + 1}（逗号分隔）`}>{(field) => <TextInput id={field.id} aria-label={`分类取值 ${index + 1}`}
          value={(score.category_values ?? []).join(",")} onChange={(e) => update(index, { category_values: e.target.value.split(",").map((v) => v.trim()) })} />}</Field>}
        {score.data_type === "numeric" && <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <Field label={`比较运算符 ${index + 1}`}>{(field) => <SelectInput id={field.id} aria-label={`比较运算符 ${index + 1}`} value={rule?.operator ?? ">="}
            onChange={(e) => update(index, {}, { operator: e.target.value })}><option value=">=">≥</option><option value="<=">≤</option></SelectInput>}</Field>
          <Field label={`阈值 ${index + 1}`}>{(field) => <TextInput id={field.id} aria-label={`阈值 ${index + 1}`} type="number" step="any" value={rule?.threshold ?? ""}
            onChange={(e) => update(index, {}, { threshold: e.target.value === "" ? null : Number(e.target.value) })} />}</Field>
        </div>}
        {score.data_type === "boolean" && <Field label={`期望值 ${index + 1}`}>{(field) => <SelectInput id={field.id} aria-label={`期望值 ${index + 1}`} value={String(rule?.expected_value ?? true)}
          onChange={(e) => update(index, {}, { expected_value: e.target.value === "true" })}><option value="true">true</option><option value="false">false</option></SelectInput>}</Field>}
        {score.data_type === "categorical" && <Field label={`期望值 ${index + 1}`}>{(field) => <TextInput id={field.id} aria-label={`期望值 ${index + 1}`} value={String(rule?.expected_value ?? "")}
          onChange={(e) => update(index, {}, { expected_value: e.target.value })} />}</Field>}
        <Button variant="quiet" disabled={value.contract.scores.length === 1} onClick={() => onChange({
          contract: { ...value.contract, scores: value.contract.scores.filter((_, i) => i !== index) },
          rules: score.data_type === "text" ? value.rules : value.rules.filter((_, i) => i !== ruleIndex(value, index)),
        })}>移除 Score {index + 1}</Button>
      </fieldset>;
    })}
    <Button variant="secondary" disabled={value.contract.scores.length >= 50} onClick={() => onChange({
      contract: { ...value.contract, scores: [...value.contract.scores, newManagedDraft().contract.scores[0]] },
      rules: [...value.rules, newManagedDraft().rules[0]],
    })}>添加外部 Score</Button>
  </section>;
}
