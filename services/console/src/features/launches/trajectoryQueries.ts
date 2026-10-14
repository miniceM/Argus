import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { LaunchReportContractError } from "./launchComparisonQueries";

type Diagnosis = import("../../api/schema").components["schemas"]["TrajectoryDiagnosisResponse"];
type Request = { launchId: string; snapshotId: string; datasetItemId: string };

const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const textOrNull = (value: unknown) => value === null || typeof value === "string";
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every(id => typeof id === "string");
const member = (value: unknown, choices: readonly string[]) => typeof value === "string" && choices.includes(value);
const conclusions = ["pass", "fail", "unknown"];

/** 检查实际渲染的字段；DTO 类型仍来自 OpenAPI，不让残缺 JSON 进入成功缓存。 */
function renderableDiagnosis(value: unknown) {
  if (!record(value) || !member(value.quality_conclusion, conclusions)
    || typeof value.execution_status !== "string" || !member(value.acquisition_state, ["PENDING", "READY", "UNAVAILABLE"])
    || !textOrNull(value.reason_code) || !textOrNull(value.langfuse_trace_url)
    || value.required_evidence_status !== null && !member(value.required_evidence_status, ["COMPLETE", "INCOMPLETE"])
    || !record(value.step_argument_display_json) || !Object.values(value.step_argument_display_json).every(v => typeof v === "string")) return false;
  if (value.completeness !== null && (!record(value.completeness)
    || !member(value.completeness.status, ["OBSERVED_COMPLETE", "PARTIAL", "ROOT_ONLY"])
    || !strings(value.completeness.warnings))) return false;
  if (!Array.isArray(value.rule_results) || !value.rule_results.every(rule => record(rule)
    && typeof rule.evaluator_id === "string" && typeof rule.evaluator_version === "string" && textOrNull(rule.binding_id)
    && member(rule.typed_status, ["succeeded", "failed", "skipped", "no_result"])
    && (rule.value === null || typeof rule.value === "boolean")
    && member(rule.conclusion, conclusions) && member(rule.frozen_conclusion, conclusions)
    && ((rule.typed_status === "succeeded" && typeof rule.value === "boolean") || rule.conclusion === "unknown")
    && typeof rule.required === "boolean" && typeof rule.critical === "boolean"
    && textOrNull(rule.reason_code) && strings(rule.evidence_step_ids))) return false;
  if (value.evidence_ref !== null && (!record(value.evidence_ref) || typeof value.evidence_ref.artifact_id !== "string"
    || value.evidence_ref.schema_version !== "1.0" || typeof value.evidence_ref.trajectory_digest !== "string"
    || typeof value.evidence_ref.trajectory_contract_digest !== "string")) return false;
  const trajectory = value.trajectory;
  if (trajectory === null) return value.evidence_ref === null && value.rule_results.every(rule => rule.evidence_step_ids.length === 0);
  if (!record(trajectory) || !record(trajectory.scope) || !record(trajectory.provenance)
    || !Array.isArray(trajectory.steps) || trajectory.steps.length > 1000) return false;
  return trajectory.steps.every(step => record(step) && typeof step.step_id === "string"
    && Number.isInteger(step.index) && typeof step.index === "number" && step.index >= 0
    && member(step.type, ["AGENT", "LLM", "TOOL", "RETRIEVER", "GUARDRAIL", "HUMAN", "CUSTOM"])
    && member(step.phase, ["REQUESTED", "EXECUTED", "BLOCKED", "EVENT"])
    && member(step.status, ["SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"])
    && typeof step.raw_name === "string" && textOrNull(step.tool_identity) && textOrNull(step.parent_step_id)
    && textOrNull(step.error_code) && (step.duration_ms === null || typeof step.duration_ms === "number" && Number.isFinite(step.duration_ms))
    && (step.causal_predecessor_ids === undefined || strings(step.causal_predecessor_ids))
    && record(step.argument_visibility) && Object.values(step.argument_visibility).every(v => member(v, ["AVAILABLE", "REDACTED", "MISSING", "INVALID"])));
}

export async function fetchTrajectoryDiagnosis(request: Request): Promise<Diagnosis> {
  const { launchId, snapshotId, datasetItemId } = request;
  const result = await api.GET("/api/v1/experiment-launches/{launch_id}/trajectory", {
    params: { path: { launch_id: launchId }, query: { snapshot_id: snapshotId, dataset_item_id: datasetItemId } },
  });
  if (result.error) throw result.error;
  // openapi-fetch 的递归 JSON inference 会宽化字段；运行期验证后使用生成 DTO。
  const data = result.data as Diagnosis | undefined;
  if (!data || data.launch_id !== launchId || data.snapshot_id !== snapshotId || data.dataset_item_id !== datasetItemId) {
    throw new LaunchReportContractError("轨迹响应归属与当前 Snapshot Case 不符", request);
  }
  if (!renderableDiagnosis(data)) {
    throw new LaunchReportContractError("轨迹响应格式不完整", request);
  }
  const trajectory = data.trajectory;
  if (trajectory && (trajectory.scope?.launch_id !== launchId || trajectory.scope?.dataset_item_id !== datasetItemId
    || !Array.isArray(trajectory.steps) || trajectory.steps.some(step => typeof data.step_argument_display_json[step.step_id] !== "string")
    || !data.evidence_ref || trajectory.provenance?.trajectory_digest !== data.evidence_ref.trajectory_digest
    || trajectory.provenance?.trajectory_contract_digest !== data.evidence_ref.trajectory_contract_digest)) {
    throw new LaunchReportContractError("轨迹制品归属或摘要不符", request);
  }
  return data;
}

export function trajectoryQueryOptions(request: { launchId: string; snapshotId: string | null; datasetItemId: string }) {
  return {
    queryKey: queryKeys.launches.trajectory(request.launchId, request.snapshotId ?? "unresolved", request.datasetItemId),
    enabled: Boolean(request.launchId && request.snapshotId && request.datasetItemId),
    retry: false,
    staleTime: Infinity,
    queryFn: () => fetchTrajectoryDiagnosis({ ...request, snapshotId: request.snapshotId! }),
  };
}
