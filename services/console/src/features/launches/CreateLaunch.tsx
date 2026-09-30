import React, { useState, useEffect } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  Bot,
  CheckSquare,
  Database,
  Rocket,
  Sliders,
} from "lucide-react";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";
import { Badge } from "../../components/Badge";
import { ErrorState, LoadingState } from "../../components/StateViews";
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
  const [evaluatorMode, setEvaluatorMode] = useState<"diagnostic" | "composite">("diagnostic");
  const [selectedEvaluators, setSelectedEvaluators] = useState<SelectionMap | null>(null);
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
  const compositeEvaluator = evaluators?.find((e) => e.scope === "item" && composedOf(e).length > 0);
  const isCompositeMode = evaluatorMode === "composite";

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
    if (isCompositeMode && composedOf(spec).length === 0) {
      return [{ id, version: selection[id], reason: "复合结论模式只能选择一个复合指标" }];
    }
    return [];
  });

  const hasInvalidSelection = selectedEvaluators !== null && evaluators !== undefined && (
    staleSelections.length > 0 ||
    (isCompositeMode
      ? !compositeEvaluator || selectedEvaluatorIds.length !== 1 || selectedEvaluatorIds[0] !== compositeEvaluator.id
      : selectedEvaluatorIds.some((id) => {
          const spec = evaluators?.find((e) => e.id === id);
          return Boolean(spec && composedOf(spec).length > 0);
        }))
  );

  const applySelection = (next: SelectionMap) => setSelectedEvaluators(next);

  const selectDiagnosticMode = () => {
    setEvaluatorMode("diagnostic");
    applySelection(diagnosticDefaults(evaluators ?? []));
  };

  const selectCompositeMode = () => {
    if (!compositeEvaluator) return;
    setEvaluatorMode("composite");
    const preferred =
      versionsOf(compositeEvaluator).find((v) => v.release_eligible) ?? versionsOf(compositeEvaluator)[0];
    if (preferred) applySelection({ [compositeEvaluator.id]: preferred.version });
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

          <fieldset className="space-y-3">
            <legend className="sr-only">选择评测结果模式</legend>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {/* token-lint-ignore: has-[...] takes a selector, not a token value. */}
              <label className="flex items-start gap-3 p-3 rounded-lg border border-border bg-canvas cursor-pointer has-[:checked]:border-primary has-[:checked]:bg-primary-subtle/50">
                <input
                  type="radio"
                  name="evaluatorMode"
                  checked={evaluatorMode === "diagnostic"}
                  onChange={selectDiagnosticMode}
                  className="mt-0.5 accent-primary"
                />
                <span>
                  <span className="block text-xs font-semibold text-foreground">逐项诊断（推荐）</span>
                  <span className="block text-micro text-muted-foreground mt-1">
                    分别记录各项评分，便于定位失败原因。所有已选指标达到阈值时，用例质量结论为通过；取消的指标不参与本次判定。
                  </span>
                </span>
              </label>
              {/* token-lint-ignore: has-[...] takes a selector, not a token value. */}
              <label className={`flex items-start gap-3 p-3 rounded-lg border border-border ${compositeEvaluator ? "bg-canvas cursor-pointer has-[:checked]:border-primary has-[:checked]:bg-primary-subtle/50" : "bg-surface-muted text-muted-foreground cursor-not-allowed"}`}>
                <input
                  type="radio"
                  name="evaluatorMode"
                  checked={evaluatorMode === "composite"}
                  onChange={selectCompositeMode}
                  disabled={!compositeEvaluator}
                  className="mt-0.5 accent-primary"
                />
                <span>
                  <span className="block text-xs font-semibold text-foreground">复合结论</span>
                  <span className="block text-micro text-muted-foreground mt-1">
                    只记录一个复合评分{compositeEvaluator ? `（${compositeEvaluator.id}）` : ""}；
                    {compositeEvaluator ? composedOf(compositeEvaluator).join("、") : "复合 Evaluator 尚不可用"} 均通过时，用例质量结论才通过，不额外记录组成项的独立评分。
                  </span>
                </span>
              </label>
            </div>
          </fieldset>

          <p className="text-micro text-muted-foreground" role="note">
            当前内置版本及默认阈值下，两种默认配置的质量通过条件等价，但结果明细不同；执行成功不等于质量通过。切换模式会重置指标选择。
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
              <button type="button" onClick={selectDiagnosticMode} className="font-semibold underline underline-offset-2 focus:outline-hidden focus:ring-2 focus:ring-fail rounded-sm">
                重新选择当前可用的默认指标版本
              </button>
            </div>
          )}

          {!isCompositeMode && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
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
          )}
        </div>

        {/* Section 4: Concurrency Execution Policy */}
        <div className="space-y-4">
          <div className="flex items-center gap-2 border-b border-border pb-2">
            <Sliders className="w-4 h-4 text-primary" />
            <h3 className="text-sm font-bold text-foreground">4. 执行调度并发设置</h3>
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
            disabled={createMutation.isPending || !selectedAgentVersion || hasInvalidSelection}
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
