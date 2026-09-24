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
import { ErrorState, LoadingState } from "../../components/StateViews";

type EvaluatorResponse = import("../../api/schema").components["schemas"]["EvaluatorResponse"];
type AgentVersionResponse = import("../../api/schema").components["schemas"]["AgentVersionResponse"];

const composedOf = (evaluator: EvaluatorResponse) => evaluator.composed_of ?? [];

export const CreateLaunch: React.FC = () => {
  const navigate = useNavigate();

  // Form State
  const [launchName, setLaunchName] = useState<string>("");
  const [selectedAgentId, setSelectedAgentId] = useState<string>("");
  const [selectedAgentVersion, setSelectedAgentVersion] = useState<string>("");
  const [datasetName, setDatasetName] = useState<string>("banking-agent-regression");
  const [datasetVersionMode, setDatasetVersionMode] = useState<"latest" | "custom">("latest");
  const [customDatasetVersion, setCustomDatasetVersion] = useState<string>("");
  const [evaluatorMode, setEvaluatorMode] = useState<"diagnostic" | "composite">("diagnostic");
  const [selectedEvaluators, setSelectedEvaluators] = useState<string[] | null>(null);
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
        typeof item.default_selected !== "boolean" || !Array.isArray(item.composed_of)
      ))) {
        throw new Error("Evaluator 目录契约不完整，请升级服务端后重试");
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

  // Initialize once: an empty array after this point is an intentional user selection.
  useEffect(() => {
    if (evaluators && selectedEvaluators === null) {
      setSelectedEvaluators(
        evaluators
          .filter((e) => e.scope === "item" && e.default_selected === true && composedOf(e).length === 0)
          .map((e) => e.id)
          .sort()
      );
    }
  }, [evaluators, selectedEvaluators]);

  const selectedEvaluatorIds = selectedEvaluators ?? [];
  const compositeEvaluator = evaluators?.find((e) => e.scope === "item" && composedOf(e).length > 0);
  const isCompositeMode = evaluatorMode === "composite";
  const selectedSpecs = selectedEvaluatorIds.map((id) => evaluators?.find((e) => e.id === id));
  const hasInvalidSelection = selectedEvaluators !== null && evaluators !== undefined && (
    selectedSpecs.some((spec) => !spec || spec.scope !== "item") ||
    (isCompositeMode
      ? !compositeEvaluator || selectedEvaluatorIds.length !== 1 || selectedEvaluatorIds[0] !== compositeEvaluator.id
      : selectedSpecs.some((spec) => spec && composedOf(spec).length > 0))
  );

  const selectDiagnosticMode = () => {
    setEvaluatorMode("diagnostic");
    setSelectedEvaluators(
      (evaluators ?? [])
        .filter((e) => e.scope === "item" && e.default_selected === true && composedOf(e).length === 0)
        .map((e) => e.id)
        .sort()
    );
  };

  const selectCompositeMode = () => {
    if (compositeEvaluator) {
      setEvaluatorMode("composite");
      setSelectedEvaluators([compositeEvaluator.id]);
    }
  };

  const toggleEvaluator = (evalId: string) => {
    const target = evaluators?.find((e) => e.id === evalId);
    if (!target || target.scope !== "item" || composedOf(target).length > 0) return;
    setSelectedEvaluators((current) => {
      const selection = current ?? [];
      return selection.includes(evalId)
        ? selection.filter((id) => id !== evalId)
        : [...selection, evalId];
    });
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
        throw new Error("评测指标选择已失效，请重新选择");
      }

      const res = await api.POST("/api/v1/experiment-launches", {
        body: {
          name: launchName.trim() || undefined,
          agent_id: selectedAgentId,
          agent_version: selectedAgentVersion,
          dataset_name: datasetName.trim(),
          dataset_version: finalDatasetVersion,
          evaluator_ids: selectedIds,
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
      <div>
        <Link
          to="/launches"
          className="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-500 hover:text-slate-800 transition-colors mb-2"
        >
          <ArrowLeft className="w-3.5 h-3.5" />
          <span>返回发射台</span>
        </Link>
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-indigo-50 border border-indigo-100 flex items-center justify-center text-indigo-600 font-bold">
            <Rocket className="w-6 h-6" />
          </div>
          <div>
            <h2 className="text-xl font-bold text-slate-900 tracking-tight">发起新评测任务 (New Launch)</h2>
            <p className="text-xs text-slate-500 mt-0.5">
              指定被测 Agent、版本规格与数据集。系统将固化四维不可变快照并生成排队任务。
            </p>
          </div>
        </div>
      </div>

      {formError && (
        <div className="p-3.5 text-xs bg-rose-50 border border-rose-200 rounded-xl text-rose-700 font-medium">
          {formError}
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-6 bg-white p-6 rounded-xl border border-slate-200 shadow-xs">
        {/* Section 0: Launch Basic Info */}
        <div className="space-y-4">
          <div className="flex items-center gap-2 border-b border-slate-100 pb-2">
            <Rocket className="w-4 h-4 text-indigo-600" />
            <h3 className="text-sm font-bold text-slate-900">0. 评测任务基本信息</h3>
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1.5">
              评测任务名称 (Launch Name) <span className="text-slate-400 font-normal">(可选，留空由系统自动命名)</span>
            </label>
            <input
              type="text"
              value={launchName}
              onChange={(e) => setLaunchName(e.target.value)}
              placeholder="例如：release-v1.0-benchmark"
              className="w-full sm:w-96 px-3 py-2 text-xs bg-slate-50 border border-slate-200 rounded-lg focus:outline-hidden focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 text-slate-800"
            />
          </div>
        </div>

        {/* Section 1: Agent & Version */}
        <div className="space-y-4">
          <div className="flex items-center gap-2 border-b border-slate-100 pb-2">
            <Bot className="w-4 h-4 text-indigo-600" />
            <h3 className="text-sm font-bold text-slate-900">1. 被测 Agent 与版本规格快照</h3>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1.5">
                选择 Agent <span className="text-rose-500">*</span>
              </label>
              <select
                value={selectedAgentId}
                onChange={(e) => setSelectedAgentId(e.target.value)}
                required
                className="w-full px-3 py-2 text-xs bg-slate-50 border border-slate-200 rounded-lg focus:outline-hidden focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 text-slate-800"
              >
                {agents?.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name} ({a.id})
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1.5">
                选择版本规格 (Active Version) <span className="text-rose-500">*</span>
              </label>
              <select
                value={selectedAgentVersion}
                onChange={(e) => setSelectedAgentVersion(e.target.value)}
                disabled={isVersionsLoading || !versions || versions.length === 0}
                required
                className="w-full px-3 py-2 text-xs bg-slate-50 border border-slate-200 rounded-lg focus:outline-hidden focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 text-slate-800 font-mono disabled:opacity-50"
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
              {versions && versions.length === 0 && (
                <p className="text-[11px] text-rose-500 mt-1">
                  当前 Agent 没有处于 ACTIVE 状态的版本。请先去 Agent 详情页创建新版本。
                </p>
              )}
            </div>
          </div>
        </div>

        {/* Section 2: Dataset & Version */}
        <div className="space-y-4">
          <div className="flex items-center gap-2 border-b border-slate-100 pb-2">
            <Database className="w-4 h-4 text-indigo-600" />
            <h3 className="text-sm font-bold text-slate-900">2. 评测数据集 (Langfuse Dataset)</h3>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1.5">
                数据集名称 (Dataset Name) <span className="text-rose-500">*</span>
              </label>
              <input
                type="text"
                value={datasetName}
                onChange={(e) => setDatasetName(e.target.value)}
                required
                placeholder="calc-agent-eval"
                className="w-full px-3 py-2 text-xs bg-slate-50 border border-slate-200 rounded-lg focus:outline-hidden focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 text-slate-800 font-mono"
              />
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1.5">
                数据集版本模式 (Dataset Version) <span className="text-rose-500">*</span>
              </label>
              <div className="flex items-center gap-4 py-1 text-xs">
                <label className="flex items-center gap-1.5 cursor-pointer">
                  <input
                    type="radio"
                    name="versionMode"
                    value="latest"
                    checked={datasetVersionMode === "latest"}
                    onChange={() => setDatasetVersionMode("latest")}
                    className="text-indigo-600"
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
                    className="text-indigo-600"
                  />
                  <span>指定快照时间戳</span>
                </label>
              </div>

              {datasetVersionMode === "custom" && (
                <input
                  type="text"
                  value={customDatasetVersion}
                  onChange={(e) => setCustomDatasetVersion(e.target.value)}
                  placeholder="例如: 2026-09-20T08:35:12Z"
                  className="mt-2 w-full px-3 py-2 text-xs bg-slate-50 border border-slate-200 rounded-lg focus:outline-hidden focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 text-slate-800 font-mono"
                />
              )}
            </div>
          </div>
        </div>

        {/* Section 3: Evaluators */}
        <div className="space-y-4">
          <div className="flex items-center justify-between border-b border-slate-100 pb-2">
            <div className="flex items-center gap-2">
              <CheckSquare className="w-4 h-4 text-indigo-600" />
              <h3 className="text-sm font-bold text-slate-900">3. 评测指标与门禁 (Evaluators)</h3>
            </div>
            <span className="text-xs text-slate-400">已选 {selectedEvaluatorIds.length} 项</span>
          </div>

          <fieldset className="space-y-3">
            <legend className="sr-only">选择评测结果模式</legend>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <label className="flex items-start gap-3 p-3 rounded-lg border border-slate-200 bg-slate-50 cursor-pointer has-[:checked]:border-indigo-300 has-[:checked]:bg-indigo-50/50">
                <input
                  type="radio"
                  name="evaluatorMode"
                  checked={evaluatorMode === "diagnostic"}
                  onChange={selectDiagnosticMode}
                  className="mt-0.5 text-indigo-600 focus:ring-indigo-500"
                />
                <span>
                  <span className="block text-xs font-semibold text-slate-900">逐项诊断（推荐）</span>
                  <span className="block text-[11px] text-slate-500 mt-1">
                    分别记录各项评分，便于定位失败原因。所有已选指标达到阈值时，用例质量结论为通过；取消的指标不参与本次判定。
                  </span>
                </span>
              </label>
              <label className={`flex items-start gap-3 p-3 rounded-lg border border-slate-200 ${compositeEvaluator ? "bg-slate-50 cursor-pointer has-[:checked]:border-indigo-300 has-[:checked]:bg-indigo-50/50" : "bg-slate-100 text-slate-400 cursor-not-allowed"}`}>
                <input
                  type="radio"
                  name="evaluatorMode"
                  checked={evaluatorMode === "composite"}
                  onChange={selectCompositeMode}
                  disabled={!compositeEvaluator}
                  className="mt-0.5 text-indigo-600 focus:ring-indigo-500"
                />
                <span>
                  <span className="block text-xs font-semibold text-slate-900">复合结论</span>
                  <span className="block text-[11px] text-slate-500 mt-1">
                    只记录一个复合评分{compositeEvaluator ? `（${compositeEvaluator.id}）` : ""}；
                    {compositeEvaluator ? composedOf(compositeEvaluator).join("、") : "复合 Evaluator 尚不可用"} 均通过时，用例质量结论才通过，不额外记录组成项的独立评分。
                  </span>
                </span>
              </label>
            </div>
          </fieldset>

          <p className="text-[11px] text-slate-500" role="note">
            当前内置版本及默认阈值下，两种默认配置的质量通过条件等价，但结果明细不同；执行成功不等于质量通过。切换模式会重置指标选择。
          </p>

          {hasInvalidSelection && (
            <div className="text-xs text-rose-700 bg-rose-50 border border-rose-200 rounded-lg p-3 space-y-2" role="alert">
              <p>Evaluator 目录已变化，当前选择不再有效。重新应用当前可用的逐项诊断默认指标后再创建 Launch。</p>
              <button type="button" onClick={selectDiagnosticMode} className="font-semibold underline underline-offset-2 focus:outline-hidden focus:ring-2 focus:ring-rose-500 rounded-sm">
                重新选择逐项诊断默认指标
              </button>
            </div>
          )}

          {!isCompositeMode && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {(evaluators ?? []).filter((ev) => composedOf(ev).length === 0).map((ev) => {
                const isItemScope = ev.scope === "item";
                const isSelected = selectedEvaluatorIds.includes(ev.id);
                return (
                  <label
                    key={ev.id}
                    className={`p-3 rounded-lg border text-xs transition-colors flex items-start gap-3 ${
                      !isItemScope
                        ? "bg-slate-100/70 border-slate-200 text-slate-400 cursor-not-allowed opacity-60"
                        : isSelected
                          ? "bg-indigo-50/50 border-indigo-300 text-slate-900 cursor-pointer"
                          : "bg-slate-50 border-slate-200 text-slate-500 hover:bg-slate-100 cursor-pointer"
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
                      className="mt-0.5 rounded border-slate-300 text-indigo-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-700 disabled:cursor-not-allowed"
                    />
                    <span>
                      <span className="flex items-center gap-2 font-semibold text-slate-900">
                        <span className={!isItemScope ? "text-slate-500" : ""}>{ev.id}</span>
                        <span className={`px-1.5 py-0.5 rounded text-[10px] uppercase font-mono ${isItemScope ? "bg-slate-200 text-slate-700" : "bg-amber-100 text-amber-700"}`}>
                          {ev.scope}
                        </span>
                        {!isItemScope && <span className="text-[10px] text-amber-600 font-normal">（聚合指标，暂不支持在单次 Launch 中直接运行）</span>}
                      </span>
                      <span className="block text-[11px] text-slate-500 mt-0.5">{ev.description || "确定性规则评测器"}</span>
                    </span>
                  </label>
                );
              })}
            </div>
          )}
        </div>

        {/* Section 4: Concurrency Execution Policy */}
        <div className="space-y-4">
          <div className="flex items-center gap-2 border-b border-slate-100 pb-2">
            <Sliders className="w-4 h-4 text-indigo-600" />
            <h3 className="text-sm font-bold text-slate-900">4. 执行调度并发设置</h3>
          </div>

          <div className="max-w-xs">
            <label className="block text-xs font-semibold text-slate-700 mb-1.5">
              最大并发执行数 (Concurrency)
            </label>
            <input
              type="number"
              min={1}
              max={10}
              value={concurrency}
              onChange={(e) => setConcurrency(Math.max(1, parseInt(e.target.value) || 1))}
              className="w-full px-3 py-2 text-xs bg-slate-50 border border-slate-200 rounded-lg focus:outline-hidden focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 text-slate-800"
            />
            <p className="text-[11px] text-slate-400 mt-1">
              受 AgentVersion 配置的最大并发数限制，推荐 1~3。
            </p>
          </div>
        </div>

        {/* Actions */}
        <div className="flex items-center justify-end gap-3 pt-4 border-t border-slate-100">
          <Link
            to="/launches"
            className="px-4 py-2 text-xs font-semibold text-slate-600 hover:text-slate-800 bg-white border border-slate-200 hover:bg-slate-50 rounded-lg transition-colors"
          >
            取消
          </Link>
          <button
            type="submit"
            disabled={createMutation.isPending || !selectedAgentVersion || hasInvalidSelection}
            className="inline-flex items-center gap-2 px-5 py-2 text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg shadow-xs transition-colors cursor-pointer disabled:opacity-50"
          >
            <Rocket className="w-3.5 h-3.5" />
            <span>{createMutation.isPending ? "正在固化快照并创建..." : "创建评测任务 (Create Launch)"}</span>
          </button>
        </div>
      </form>
    </div>
  );
};
