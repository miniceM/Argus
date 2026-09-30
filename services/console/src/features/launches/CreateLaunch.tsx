import React, { useState, useEffect } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  Bot,
  CheckSquare,
  Database,
  Rocket,
  Scale,
  Sliders,
} from "lucide-react";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";
import { Badge } from "../../components/Badge";
import { ErrorState, LoadingState } from "../../components/StateViews";
import {
  allowedOperators,
  defaultRuleDraft,
  describeRuleDraft,
  isEvidenceOnly,
  toQualityPolicyRequest,
  validateRuleDrafts,
  type RuleDraft,
  type RuleIssue,
} from "./qualityPolicy";
import {
  Button,
  Field,
  PageHeader,
  buttonClassName,
} from "../../components/ui/Primitives";

type EvaluatorResponse = import("../../api/schema").components["schemas"]["EvaluatorResponse"];
type EvaluatorVersionInfo = import("../../api/schema").components["schemas"]["EvaluatorVersionInfo"];
type AgentVersionResponse = import("../../api/schema").components["schemas"]["AgentVersionResponse"];

const composedOf = (evaluator: EvaluatorResponse) => evaluator.composed_of ?? [];
const versionsOf = (evaluator: EvaluatorResponse): EvaluatorVersionInfo[] => evaluator.versions ?? [];
const reasonsOf = (value: { eligibility_reasons?: string[] }): string[] => value.eligibility_reasons ?? [];
const messagesOf = (value: { eligibility_messages?: string[] }): string[] => value.eligibility_messages ?? [];

const findVersion = (
  evaluator: EvaluatorResponse,
  version: string,
): EvaluatorVersionInfo | undefined => versionsOf(evaluator).find((v) => v.version === version);

const versionMessages = (evaluator: EvaluatorResponse, version: string): string[] =>
  messagesOf(findVersion(evaluator, version) ?? { eligibility_messages: [] });

/** The exact version the user confirmed for an evaluator, per evaluator id. */
type SelectionMap = Record<string, string>;

const diagnosticDefaults = (evaluators: EvaluatorResponse[]): SelectionMap =>
  evaluators
    .filter((e) => e.scope === "item" && e.default_selected === true && composedOf(e).length === 0)
    .reduce<SelectionMap>((acc, e) => {
      const preferred = versionsOf(e).find((v) => v.release_eligible) ?? versionsOf(e)[0];
      if (preferred) acc[e.id] = preferred.version;
      return acc;
    }, {});


export const CreateLaunch: React.FC = () => {
  const navigate = useNavigate();

  // Form State
  const [launchName, setLaunchName] = useState<string>("");
  const [selectedAgentId, setSelectedAgentId] = useState<string>("");
  const [selectedAgentVersion, setSelectedAgentVersion] = useState<string>("");
  const [datasetName, setDatasetName] = useState<string>("banking-agent-regression");
  const [environment, setEnvironment] = useState<string>("production");
  const [datasetVersionMode, setDatasetVersionMode] = useState<"latest" | "custom">("latest");
  const [customDatasetVersion, setCustomDatasetVersion] = useState<string>("");
  const [selectedEvaluators, setSelectedEvaluators] = useState<SelectionMap | null>(null);
  /** User edits to the policy, keyed by `evaluator_id@version`. */
  const [ruleOverrides, setRuleOverrides] = useState<Record<string, RuleDraft>>({});
  const [concurrency, setConcurrency] = useState<number>(1);
  const [formError, setFormError] = useState<string | null>(null);

  // 1. Fetch Agents List
  const { data: agents, isLoading: isAgentsLoading, error: agentsError } = useQuery({
    queryKey: queryKeys.agents.list(),
    queryFn: async () => {
      const res = await api.GET("/api/v1/agents");
      if (res.error) throw res.error;
      return Array.isArray(res.data) ? res.data : [res.data];
    },
  });

  // 2. Fetch Versions of Selected Agent
  const { data: versions, isLoading: isVersionsLoading } = useQuery<AgentVersionResponse[]>({
    queryKey: queryKeys.agents.versions(selectedAgentId),
    queryFn: async () => {
      if (!selectedAgentId) return [];
      const res = await api.GET("/api/v1/agent-versions", {
        params: { query: { agent_id: selectedAgentId } },
      });
      if (res.error) throw res.error;
      const list = Array.isArray(res.data) ? res.data : [res.data];
      // Only active versions can be used for new launches
      return (list as AgentVersionResponse[]).filter((v) => v.is_active);
    },
    enabled: Boolean(selectedAgentId),
  });

  // 3. Fetch Evaluators Specs
  const { data: evaluators, isLoading: isEvaluatorsLoading, error: evaluatorsError } = useQuery<EvaluatorResponse[]>({
    queryKey: queryKeys.evaluators.list(),
    queryFn: async () => {
      const res = await api.GET("/api/v1/evaluators");
      if (!res.data) throw new Error("获取 Evaluators 失败");
      const list = Array.isArray(res.data) ? res.data : [res.data];
      if (list.some((item) => (
        typeof item.default_selected !== "boolean" ||
        !Array.isArray(item.composed_of) ||
        !Array.isArray(item.versions) ||
        item.versions.length === 0 ||
        typeof item.default_version !== "string"
      ))) {
        throw new Error("Evaluator 目录契约不完整（缺少版本信息），请升级服务端后重试");
      }
      return list as EvaluatorResponse[];
    },
  });

  // Auto select first agent when loaded
  useEffect(() => {
    if (agents && agents.length > 0 && !selectedAgentId) {
      setSelectedAgentId(agents[0].id);
    }
  }, [agents, selectedAgentId]);

  // Auto select latest active version when versions loaded
  useEffect(() => {
    if (versions && versions.length > 0) {
      setSelectedAgentVersion(versions[0].version);
    } else {
      setSelectedAgentVersion("");
    }
  }, [versions]);

  // Initialize once. A previously confirmed selection is restored so that a
  // Catalog refresh never silently swaps the version the user picked (#80).
  useEffect(() => {
    if (evaluators && selectedEvaluators === null) {
      setSelectedEvaluators(diagnosticDefaults(evaluators));
    }
  }, [evaluators, selectedEvaluators]);

  const selection: SelectionMap = selectedEvaluators ?? {};
  const selectedEvaluatorIds = Object.keys(selection).sort();

  // A selection is invalid when the pinned id/version is no longer offered, is
  // out of scope, or has become ineligible for release evidence.
  const staleSelections = selectedEvaluatorIds.flatMap((id) => {
    const spec = evaluators?.find((e) => e.id === id);
    if (!spec) return [{ id, version: selection[id], reason: "该指标已不在当前目录中" }];
    if (spec.scope !== "item") {
      return [{ id, version: selection[id], reason: `scope「${spec.scope}」不支持在此流程运行` }];
    }
    const picked = findVersion(spec, selection[id]);
    if (!picked) {
      return [{ id, version: selection[id], reason: `版本 ${selection[id]} 已不在当前目录中` }];
    }
    if (!picked.release_eligible) {
      return [{ id, version: selection[id], reason: messagesOf(picked).join("；") }];
    }
    return [];
  });

  // Issue #83: a new Launch is configured per diagnostic metric. The composite
  // overall_pass metric is no longer selectable here; historical composite
  // Launches stay readable on the detail page.
  const hasInvalidSelection = selectedEvaluators !== null && evaluators !== undefined && (
    staleSelections.length > 0 ||
    selectedEvaluatorIds.some((id) => {
      const spec = evaluators?.find((e) => e.id === id);
      return Boolean(spec && composedOf(spec).length > 0);
    })
  );

  /** The exact frozen version behind each selected metric, for rule defaults. */
  const selectedVersions = selectedEvaluatorIds.reduce<Record<string, EvaluatorVersionInfo | undefined>>(
    (acc, id) => {
      const spec = evaluators?.find((e) => e.id === id);
      acc[id] = spec ? findVersion(spec, selection[id]) : undefined;
      return acc;
    },
    {},
  );

  // A metric starts from its own frozen direction and threshold; the state only
  // holds what the user changed. Deriving the effective draft during render
  // (instead of seeding it from an effect) means a freshly selected metric is
  // valid on the very first render — the form is never briefly blocked by an
  // empty policy. The key includes the pinned version, so re-pinning a metric
  // re-seeds its defaults instead of inheriting another version's rules.
  const ruleDrafts: Record<string, RuleDraft> = selectedEvaluatorIds.reduce<
    Record<string, RuleDraft>
  >((acc, id) => {
    const version = selectedVersions[id];
    if (!version) return acc;
    const key = `${id}@${version.version}`;
    acc[id] = ruleOverrides[key] ?? defaultRuleDraft(version);
    return acc;
  }, {});

  const ruleIssues: RuleIssue[] = validateRuleDrafts(ruleDrafts, selectedVersions);
  // With nothing selected there is no policy to judge yet — the submit path
  // explains the missing selection itself, so the gate stays out of the way.
  const hasInvalidRules = selectedEvaluatorIds.length > 0 && ruleIssues.length > 0;

  const updateRule = (evaluatorId: string, patch: Partial<RuleDraft>) => {
    const version = selectedVersions[evaluatorId];
    if (!version) return;
    const key = `${evaluatorId}@${version.version}`;
    setRuleOverrides((current) => ({
      ...current,
      [key]: { ...(current[key] ?? defaultRuleDraft(version)), ...patch } as RuleDraft,
    }));
  };

  const applySelection = (next: SelectionMap) => setSelectedEvaluators(next);

  const resetSelectionToDefaults = () => {
    applySelection(diagnosticDefaults(evaluators ?? []));
  };

  const toggleEvaluator = (evalId: string) => {
    const target = evaluators?.find((e) => e.id === evalId);
    if (!target || target.scope !== "item" || composedOf(target).length > 0) return;
    setSelectedEvaluators((current) => {
      const base = current ?? {};
      const next = base[evalId] !== undefined
        ? Object.fromEntries(Object.entries(base).filter(([key]) => key !== evalId))
        : (() => {
            const preferred = versionsOf(target).find((v) => v.release_eligible) ?? versionsOf(target)[0];
            return preferred ? { ...base, [evalId]: preferred.version } : base;
          })();
      return next;
    });
  };

  const selectedVersion = (evaluator: EvaluatorResponse): EvaluatorVersionInfo | undefined =>
    findVersion(evaluator, selection[evaluator.id] ?? evaluator.default_version);

  const selectVersion = (evalId: string, version: string) => {
    setSelectedEvaluators((current) => ({ ...(current ?? {}), [evalId]: version }));
  };

  const createMutation = useMutation({
    mutationFn: async () => {
      setFormError(null);

      if (!selectedAgentId) throw new Error("请选择被测 Agent");
      if (!selectedAgentVersion) throw new Error("请选择 Agent 规格版本（必须是已激活版本）");
      if (!datasetName.trim()) throw new Error("请输入评测集名称 (Dataset Name)");

      const finalDatasetVersion =
        datasetVersionMode === "latest"
          ? undefined
          : customDatasetVersion.trim() || undefined;

      if (datasetVersionMode === "custom" && !customDatasetVersion.trim()) {
        throw new Error("请输入自定义评测集版本号（如 ISO-8601 UTC 时间戳）");
      }

      const selectedIds = selectedEvaluatorIds;
      if (selectedIds.length === 0) {
        throw new Error("请至少选择一个评测指标 (Evaluator)");
      }

      if (hasInvalidSelection) {
        const detail = staleSelections
          .map((item) => `${item.id}@${item.version}：${item.reason}`)
          .join("；");
        throw new Error(`评测指标选择已失效，无法创建任务。${detail}`);
      }

      // Issue #83: the confirmed rules are submitted verbatim; an illegal rule
      // is rejected by the server, and mirrored here so the user sees why.
      const ruleDetail = ruleIssues
        .map((issue) => (issue.evaluatorId ? `${issue.evaluatorId}：${issue.message}` : issue.message))
        .join("；");
      if (ruleDetail && selectedIds.length > 0) {
        throw new Error(`质量判定规则不合法，无法创建任务。${ruleDetail}`);
      }

      // Issue #80: submit the exact user-confirmed versions, never a moving alias.
      const evaluator_selections = selectedIds.map((id) => ({
        id,
        version: selection[id],
      }));

      const res = await api.POST("/api/v1/experiment-launches", {
        body: {
          name: launchName.trim() || undefined,
          agent_id: selectedAgentId,
          agent_version: selectedAgentVersion,
          dataset_name: datasetName.trim(),
          dataset_version: finalDatasetVersion,
          environment: environment.trim() || "production",
          evaluator_selections,
          max_concurrency: concurrency,
          quality_policy: toQualityPolicyRequest(ruleDrafts),
        },
      });

      if (res.error) throw res.error;
      return res.data;
    },
    onSuccess: (data) => {
      navigate(`/launches/${data.id}`);
    },
    onError: (err) => {
      setFormError(formatApiError(err));
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    createMutation.mutate();
  };

  if (isAgentsLoading || isEvaluatorsLoading) {
    return <LoadingState message="正在准备评测配置项..." />;
  }

  if (agentsError || evaluatorsError) {
    return <ErrorState message={formatApiError(agentsError || evaluatorsError)} />;
  }

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div className="space-y-3">
        <Link
          to="/launches"
          className="inline-flex items-center gap-1.5 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          <span>返回发射台</span>
        </Link>
        <PageHeader
          title={(
            <span className="flex items-center gap-2">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-primary-border bg-primary-subtle text-primary">
                <Rocket className="h-4 w-4" />
              </span>
              发起新评测任务 (New Launch)
            </span>
          )}
          description="指定被测 Agent、版本规格与数据集。系统将固化四维不可变快照并生成排队任务。"
        />
      </div>

      {formError && (
        <div className="p-3.5 text-xs bg-fail-subtle border border-fail-border rounded-xl text-fail font-medium">
          {formError}
        </div>
      )}

      <form onSubmit={handleSubmit} className="ui-panel space-y-6 p-5 shadow-xs sm:p-6">
        {/* Section 0: Launch Basic Info */}
        <div className="space-y-4">
          <div className="flex items-center gap-2 border-b border-border pb-2">
            <Rocket className="w-4 h-4 text-primary" />
            <h3 className="text-sm font-bold text-foreground">0. 评测任务基本信息</h3>
          </div>

          <div>
            <Field
              label={
                <>
                  评测任务名称 (Launch Name){" "}
                  <span className="text-muted-foreground font-normal">(可选，留空由系统自动命名)</span>
                </>
              }
            >
              {({ id }) => (
                <input
                  id={id}
                  type="text"
                  value={launchName}
                  onChange={(e) => setLaunchName(e.target.value)}
                  placeholder="例如：release-v1.0-benchmark"
                  className="ui-control w-full sm:w-96 text-xs"
                />
              )}
            </Field>
          </div>
          <div>
            <Field
              label="Environment"
              required
              hint="按 Agent + Environment 自动冻结当前 Baseline。"
            >
              {({ id, ...aria }) => (
                <input
                  {...aria}
                  id={id}
                  type="text"
                  value={environment}
                  onChange={(e) => setEnvironment(e.target.value)}
                  maxLength={64}
                  required
                  placeholder="production"
                  className="ui-control w-full sm:w-96 text-xs"
                />
              )}
            </Field>
          </div>
        </div>

        {/* Section 1: Agent & Version */}
        <div className="space-y-4">
          <div className="flex items-center gap-2 border-b border-border pb-2">
            <Bot className="w-4 h-4 text-primary" />
            <h3 className="text-sm font-bold text-foreground">1. 被测 Agent 与版本规格快照</h3>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <Field label="选择 Agent" required>
                {({ id }) => (
                  <select
                    id={id}
                    value={selectedAgentId}
                    onChange={(e) => setSelectedAgentId(e.target.value)}
                    required
                    className="ui-control w-full text-xs"
                  >
                    {agents?.map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.name} ({a.id})
                      </option>
                    ))}
                  </select>
                )}
              </Field>
            </div>

            <div>
              <Field label="选择版本规格 (Active Version)" required>
                {({ id }) => (
                  <select
                    id={id}
                    value={selectedAgentVersion}
                    onChange={(e) => setSelectedAgentVersion(e.target.value)}
                    disabled={isVersionsLoading || !versions || versions.length === 0}
                    required
                    className="ui-control w-full text-xs font-mono disabled:opacity-50"
                  >
                    {isVersionsLoading && <option>加载版本中...</option>}
                    {!isVersionsLoading && (!versions || versions.length === 0) && (
                      <option value="">该 Agent 暂无可用的激活版本</option>
                    )}
                    {versions?.map((v) => (
                      <option key={v.id} value={v.version}>
                        {v.version} (env: {v.environment || "default"})
                      </option>
                    ))}
                  </select>
                )}
              </Field>
              {versions && versions.length === 0 && (
                <p className="text-micro text-fail mt-1">
                  当前 Agent 没有处于 ACTIVE 状态的版本。请先去 Agent 详情页创建新版本。
                </p>
              )}
            </div>
          </div>
        </div>

        {/* Section 2: Dataset & Version */}
        <div className="space-y-4">
          <div className="flex items-center gap-2 border-b border-border pb-2">
            <Database className="w-4 h-4 text-primary" />
            <h3 className="text-sm font-bold text-foreground">2. 评测数据集 (Langfuse Dataset)</h3>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <Field label="数据集名称 (Dataset Name)" required>
                {({ id }) => (
                  <input
                    id={id}
                    type="text"
                    value={datasetName}
                    onChange={(e) => setDatasetName(e.target.value)}
                    required
                    placeholder="calc-agent-eval"
                    className="ui-control w-full text-xs font-mono"
                  />
                )}
              </Field>
            </div>

            <div>
              <fieldset>
                <legend className="text-xs font-semibold text-foreground-secondary mb-1.5">
                  数据集版本模式 (Dataset Version) <span className="text-fail">*</span>
                </legend>
              <div className="flex items-center gap-4 py-1 text-xs">
                <label className="flex items-center gap-1.5 cursor-pointer">
                  <input
                    type="radio"
                    name="versionMode"
                    value="latest"
                    checked={datasetVersionMode === "latest"}
                    onChange={() => setDatasetVersionMode("latest")}
                    className="text-primary"
                  />
                  <span>最新版本 (latest)</span>
                </label>
                <label className="flex items-center gap-1.5 cursor-pointer">
                  <input
                    type="radio"
                    name="versionMode"
                    value="custom"
                    checked={datasetVersionMode === "custom"}
                    onChange={() => setDatasetVersionMode("custom")}
                    className="text-primary"
                  />
                  <span>指定快照时间戳</span>
                </label>
              </div>
              </fieldset>

              {datasetVersionMode === "custom" && (
                <input
                  type="text"
                  value={customDatasetVersion}
                  onChange={(e) => setCustomDatasetVersion(e.target.value)}
                  placeholder="例如: 2026-09-20T08:35:12Z"
                  className="ui-control mt-2 w-full text-xs font-mono"
                />
              )}
            </div>
          </div>
        </div>

        {/* Section 3: Evaluators */}
        <div className="space-y-4">
          <div className="flex items-center justify-between border-b border-border pb-2">
            <div className="flex items-center gap-2">
              <CheckSquare className="w-4 h-4 text-primary" />
              <h3 className="text-sm font-bold text-foreground">3. 评测指标与门禁 (Evaluators)</h3>
            </div>
            <span className="text-xs text-muted-foreground">已选 {selectedEvaluatorIds.length} 项</span>
          </div>

          <p className="text-micro text-muted-foreground" role="note">
            逐项诊断模式：分别记录各项评分并各自配置判定规则。执行成功不等于质量通过；
            取消勾选的指标只记录评分，不参与本次质量判定。
          </p>

          {hasInvalidSelection && (
            <div className="text-xs text-fail bg-fail-subtle border border-fail-border rounded-lg p-3 space-y-2" role="alert">
              <p className="font-semibold">当前评测指标版本不可用，无法创建任务。</p>
              <ul className="list-disc pl-4 space-y-1">
                {staleSelections.map((item) => (
                  <li key={`${item.id}@${item.version}`}>
                    <span className="font-mono">{item.id}@{item.version}</span>：{item.reason}
                  </li>
                ))}
              </ul>
              <button type="button" onClick={resetSelectionToDefaults} className="font-semibold underline underline-offset-2 focus:outline-hidden focus:ring-2 focus:ring-fail rounded-sm">
                重新选择当前可用的默认指标版本
              </button>
            </div>
          )}

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3" data-testid="evaluator-catalog">
              {(evaluators ?? []).filter((ev) => composedOf(ev).length === 0).map((ev) => {
                const isItemScope = ev.scope === "item";
                const isSelected = selectedEvaluatorIds.includes(ev.id);
                const picked = isItemScope ? selectedVersion(ev) : undefined;
                const selectable = isItemScope && Boolean(picked?.release_eligible);
                return (
                  <div
                    key={ev.id}
                    className={`p-3 rounded-lg border text-xs transition-colors flex flex-col gap-2 ${
                      !isItemScope
                        ? "bg-surface-muted/70 border-border text-muted-foreground opacity-60"
                        : isSelected
                          ? "bg-primary-subtle/50 border-primary-border text-foreground"
                          : "bg-canvas border-border text-muted-foreground hover:bg-surface-muted"
                    }`}
                  >
                    <label
                      className={`flex items-start gap-3 w-full min-w-0 ${
                        isItemScope ? "cursor-pointer" : "cursor-not-allowed"
                      }`}
                      aria-disabled={!isItemScope}
                    >
                      <input
                        type="checkbox"
                        data-testid={`evaluator-toggle-${ev.id}`}
                        checked={isSelected}
                        disabled={!isItemScope}
                        onChange={() => toggleEvaluator(ev.id)}
                        onKeyDown={(event) => {
                          if (event.key !== "Enter") return;
                          event.preventDefault();
                          if (event.repeat || event.nativeEvent.isComposing) return;
                          toggleEvaluator(ev.id);
                        }}
                        className="mt-0.5 rounded border-border-strong text-primary focus-visible:ring-2 focus-visible:ring-focus disabled:cursor-not-allowed"
                      />
                      <span className="min-w-0 flex-1 space-y-1">
                        <span className="flex flex-wrap items-center gap-2 font-semibold text-foreground">
                          <span>{ev.name || ev.id}</span>
                          <span className="font-mono text-micro font-normal text-muted-foreground">{ev.id}</span>
                          <span className={`px-1.5 py-0.5 rounded text-2xs uppercase font-mono ${isItemScope ? "bg-surface-muted text-foreground-secondary" : "bg-timeout-subtle text-timeout"}`}>
                            {ev.scope}
                          </span>
                          <Badge tone={selectable ? "pass" : "timeout"}>
                            {isItemScope
                              ? selectable ? "可用于发布评测" : "不可用于发布评测"
                              : "不适用于单次 Launch"}
                          </Badge>
                        </span>
                        <span className="block text-micro text-muted-foreground">{ev.description || "确定性规则评测器"}</span>
                        {!isItemScope && (
                          <span className="block text-micro text-timeout">
                            派生运行指标，不能作为用例指标选择{reasonsOf(ev).length > 0 ? `（${reasonsOf(ev).join("、")}）` : ""}
                          </span>
                        )}
                        {isItemScope && !selectable && (
                          <span className="block text-micro text-timeout">
                            {versionMessages(ev, selection[ev.id] ?? ev.default_version).join("；") || "该版本当前不可用于发布评测"}
                          </span>
                        )}
                      </span>
                    </label>
                    {isItemScope && (
                      <div className="w-full space-y-1.5 pl-6">
                        <label className="flex items-center gap-1.5 text-micro text-muted-foreground">
                          <span>版本</span>
                          <select
                            aria-label={`${ev.id} 版本`}
                            value={selection[ev.id] ?? ""}
                            disabled={!isSelected}
                            onChange={(event) => selectVersion(ev.id, event.target.value)}
                            className="ui-control text-micro py-0.5 disabled:opacity-60"
                          >
                            {versionsOf(ev).length === 0 && <option value="">无可用版本</option>}
                            {versionsOf(ev).map((version) => (
                              <option key={version.version} value={version.version}>
                                {version.version}
                                {version.version === ev.default_version ? "（默认）" : ""}
                                {version.release_eligible ? "" : "（不可用于发布评测）"}
                              </option>
                            ))}
                          </select>
                          {picked && (
                            <span>结果类型：{picked.result_type}</span>
                          )}
                        </label>
                        {isSelected && picked && (
                          <details className="text-micro text-muted-foreground">
                            <summary className="cursor-pointer focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus rounded-sm">
                              查看输入/输出契约与制品标识
                            </summary>
                            <div className="mt-1 space-y-1 rounded border border-border bg-surface-muted px-2 py-1.5">
                              <p>
                                执行器：<span className="font-mono">{picked.executor_type}</span>
                                {" · "}归属：<span className="font-mono">{ev.execution_owner}</span>
                                {" · "}来源：<span className="font-mono">{ev.definition_source}</span>
                              </p>
                              <p className="font-mono break-all">实现引用：{picked.implementation_ref ?? "无"}</p>
                              <p className="font-mono break-all">内容摘要：{picked.content_digest}</p>
                              <p className="font-mono break-all">输入契约：{JSON.stringify(picked.input_contract ?? {})}</p>
                              <p className="font-mono break-all">输出契约：{JSON.stringify(picked.output_contract ?? {})}</p>
                            </div>
                          </details>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
          </div>
        </div>

        {/* Section 4: Quality Policy (Issue #83) */}
        <div className="space-y-4">
          <div className="flex items-center justify-between border-b border-border pb-2">
            <div className="flex items-center gap-2">
              <Scale className="w-4 h-4 text-primary" />
              <h3 className="text-sm font-bold text-foreground">4. 质量判定规则 (Quality Policy)</h3>
            </div>
            <span className="text-xs text-muted-foreground">
              必要规则 {Object.values(ruleDrafts).filter((d) => d.required && !isEvidenceOnly(d.resultType)).length} 条
            </span>
          </div>

          <p className="text-micro text-muted-foreground" role="note">
            每条必要规则都必须有明确证据才能判定：证据缺失或不适用时该用例为「证据不足 UNKNOWN」，
            而不是「不通过 FAIL」。取消「参与判定」后该指标只作为可选诊断记录，不影响质量结论。
          </p>

          {selectedEvaluatorIds.length === 0 ? (
            <p className="text-xs text-muted-foreground">请先在上方选择至少一个评测指标。</p>
          ) : (
            <div className="space-y-3" data-testid="quality-policy-editor">
              {selectedEvaluatorIds.map((id) => {
                const draft = ruleDrafts[id];
                const version = selectedVersions[id];
                const evidenceOnly = draft ? isEvidenceOnly(draft.resultType) : false;
                if (!draft || !version) return null;
                const issue = ruleIssues.find((item) => item.evaluatorId === id);
                const operators = allowedOperators(draft.resultType);
                return (
                  <div
                    key={id}
                    data-testid={`quality-rule-${id}`}
                    className={`rounded-lg border p-3 space-y-2.5 ${
                      issue
                        ? "border-fail-border bg-fail-subtle/40"
                        : draft.required
                          ? "border-border bg-canvas"
                          : "border-border bg-surface-muted/60"
                    }`}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold text-foreground">{id}</span>
                      <span className="px-1.5 py-0.5 rounded text-2xs uppercase font-mono bg-surface-muted text-foreground-secondary">
                        {draft.resultType}
                      </span>
                      <Badge tone={draft.required ? "pass" : "neutral"}>
                        {draft.required ? "参与判定" : "仅记录诊断"}
                      </Badge>
                      {draft.critical && <Badge tone="timeout">关键规则</Badge>}
                    </div>

                    {evidenceOnly ? (
                      <p className="text-micro text-muted-foreground">
                        该指标返回文本，只能作为证据展示，不能作为质量判定规则（服务端同样拒绝文本判定规则）。
                      </p>
                    ) : (
                      <div className="flex flex-wrap items-end gap-3">
                        <label className="flex items-center gap-1.5 text-micro text-foreground-secondary">
                          <input
                            type="checkbox"
                            data-testid={`quality-rule-required-${id}`}
                            checked={draft.required}
                            onChange={(e) => updateRule(id, { required: e.target.checked })}
                            className="rounded border-border-strong text-primary focus-visible:ring-2 focus-visible:ring-focus"
                          />
                          <span>参与判定（必要规则）</span>
                        </label>

                        <label className="flex items-center gap-1.5 text-micro text-foreground-secondary">
                          <span>判定条件</span>
                          <select
                            aria-label={`${id} 判定运算符`}
                            value={draft.operator}
                            onChange={(e) => updateRule(id, { operator: e.target.value as RuleDraft["operator"] })}
                            className="ui-control text-micro py-0.5"
                          >
                            <option value="">仅作为证据</option>
                            {operators.map((operator) => (
                              <option key={operator} value={operator}>
                                {draft.resultType === "numeric" ? `${operator} 阈值` : `${operator} 显式匹配`}
                              </option>
                            ))}
                          </select>
                        </label>

                        {draft.operator !== "" && (
                          draft.resultType === "boolean" ? (
                            <label className="flex items-center gap-1.5 text-micro text-foreground-secondary">
                              <span>期望取值</span>
                              <select
                                aria-label={`${id} 期望取值`}
                                value={draft.value}
                                onChange={(e) => updateRule(id, { value: e.target.value })}
                                className="ui-control text-micro py-0.5"
                              >
                                <option value="">请选择</option>
                                <option value="true">true</option>
                                <option value="false">false</option>
                              </select>
                            </label>
                          ) : draft.resultType === "categorical" ? (
                            <label className="flex items-center gap-1.5 text-micro text-foreground-secondary">
                              <span>期望取值</span>
                              <select
                                aria-label={`${id} 期望取值`}
                                value={draft.value}
                                onChange={(e) => updateRule(id, { value: e.target.value })}
                                className="ui-control text-micro py-0.5"
                              >
                                <option value="">请选择</option>
                                {(version.category_values ?? []).map((value) => (
                                  <option key={value} value={value}>{value}</option>
                                ))}
                              </select>
                            </label>
                          ) : (
                            <label className="flex items-center gap-1.5 text-micro text-foreground-secondary">
                              <span>阈值</span>
                              <input
                                type="number"
                                step="any"
                                aria-label={`${id} 阈值`}
                                value={draft.value}
                                onChange={(e) => updateRule(id, { value: e.target.value })}
                                className="ui-control w-28 text-micro py-0.5 font-mono"
                              />
                            </label>
                          )
                        )}

                        <label className="flex items-center gap-1.5 text-micro text-foreground-secondary">
                          <input
                            type="checkbox"
                            aria-label={`${id} 关键规则`}
                            checked={draft.critical}
                            onChange={(e) => updateRule(id, { critical: e.target.checked })}
                            className="rounded border-border-strong text-primary focus-visible:ring-2 focus-visible:ring-focus"
                          />
                          <span>关键规则</span>
                        </label>
                      </div>
                    )}

                    <p className="text-micro text-muted-foreground font-mono">
                      规则：{describeRuleDraft(draft)}
                    </p>

                    {issue && (
                      <p className="text-micro text-fail font-medium" role="alert">{issue.message}</p>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          {hasInvalidRules && ruleIssues.some((issue) => issue.evaluatorId === "") && (
            <p className="text-xs text-fail" role="alert">
              {ruleIssues.find((issue) => issue.evaluatorId === "")?.message}
            </p>
          )}
        </div>

        {/* Section 5: Concurrency Execution Policy */}
        <div className="space-y-4">
          <div className="flex items-center gap-2 border-b border-border pb-2">
            <Sliders className="w-4 h-4 text-primary" />
            <h3 className="text-sm font-bold text-foreground">5. 执行调度并发设置</h3>
          </div>

          <div className="max-w-xs">
            <Field
              label="最大并发执行数 (Concurrency)"
              hint="受 AgentVersion 配置的最大并发数限制，推荐 1~3。"
            >
              {({ id, ...aria }) => (
                <input
                  {...aria}
                  id={id}
                  type="number"
                  min={1}
                  max={10}
                  value={concurrency}
                  onChange={(e) => setConcurrency(Math.max(1, parseInt(e.target.value) || 1))}
                  className="ui-control w-full text-xs"
                />
              )}
            </Field>
          </div>
        </div>

        {/* Actions */}
        <div className="flex items-center justify-end gap-3 pt-4 border-t border-border">
          <Link
            to="/launches"
            className={buttonClassName("secondary", "text-xs")}
          >
            取消
          </Link>
          <Button
            type="submit"
            disabled={createMutation.isPending || !selectedAgentVersion || hasInvalidSelection || hasInvalidRules}
            className="text-xs"
          >
            <Rocket className="w-3.5 h-3.5" />
            <span>{createMutation.isPending ? "正在固化快照并创建..." : "创建评测任务 (Create Launch)"}</span>
          </Button>
        </div>
      </form>
    </div>
  );
};
