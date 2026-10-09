/**
 * Pure projection and validation for immutable result snapshot reports.
 * Enforces strict isolation: frozen report measurements must NEVER bleed from live execution data.
 */

export interface ReportCaseItem {
  id: string | null;
  dataset_item_id: string;
  execution_status: string;
  eval_status: string;
  quality_conclusion: "pass" | "fail" | "unknown";
  scores: Record<string, number | undefined>;
  quality_evaluation: any | null;
  evaluation_results: any | null;
  trace_url: string | null;
  langfuse_trace_url: string | null;
  latency_ms: number | null;
  final_attempt_latency_ms: number | null;
  final_attempt_http_status: number | null;
  attempt_count: number | null;
  final_attempt_id: string | null;
  is_frozen: boolean;
  output_ref?: any;
}

export interface SnapshotValidationResult {
  isValid: boolean;
  error?: string;
  items?: any[];
}

/**
 * Validates that a SnapshotDetail response matches the expected identity and contract schema.
 */
export function validateSnapshotDetail(
  detail: any,
  expectedLaunchId: string,
  expectedSnapshotId: string,
): SnapshotValidationResult {
  if (!detail || typeof detail !== "object") {
    return { isValid: false, error: "快照响应数据无效" };
  }

  if (detail.launch_id && detail.launch_id !== expectedLaunchId) {
    return {
      isValid: false,
      error: `快照归属 Launch 不一致: 期望 ${expectedLaunchId}, 实际 ${detail.launch_id}`,
    };
  }

  if (detail.snapshot_id && detail.snapshot_id !== expectedSnapshotId) {
    return {
      isValid: false,
      error: `快照 ID 不一致: 期望 ${expectedSnapshotId}, 实际 ${detail.snapshot_id}`,
    };
  }

  if (!("items" in detail) || !Array.isArray(detail.items)) {
    return {
      isValid: false,
      error: "快照明细缺少用例记录 (items 字段缺失或非数组)",
    };
  }

  return {
    isValid: true,
    items: detail.items,
  };
}

/**
 * Pure projector for a frozen case item.
 * ONLY accepts frozen record and stable execution ID. Never receives a live item object.
 */
export function projectFrozenCase(
  row: Record<string, any>,
  stableExecutionId: string | null,
): ReportCaseItem {
  const datasetItemId = String(row.dataset_item_id ?? "");

  const conclusionRaw = (row.quality_conclusion ?? "unknown").toLowerCase();
  const qualityConclusion: "pass" | "fail" | "unknown" =
    conclusionRaw === "pass" ? "pass" : conclusionRaw === "fail" ? "fail" : "unknown";

  const traceUrl = row.trace_url || row.langfuse_trace_url || null;

  // Latency: strictly from row. Real 0 must be preserved. Null remains null.
  const latency = typeof row.latency_ms === "number" && Number.isFinite(row.latency_ms)
    ? row.latency_ms
    : null;

  // Attempt count: prefer cost_evidence.attempt_count, then row.attempt_count.
  let attemptCount: number | null = null;
  if (typeof row.cost_evidence?.attempt_count === "number" && Number.isFinite(row.cost_evidence.attempt_count)) {
    attemptCount = row.cost_evidence.attempt_count;
  } else if (typeof row.attempt_count === "number" && Number.isFinite(row.attempt_count)) {
    attemptCount = row.attempt_count;
  }

  return {
    id: stableExecutionId ?? null,
    dataset_item_id: datasetItemId,
    execution_status: row.execution_status ?? "succeeded",
    eval_status: row.eval_status ?? row.evaluation_status ?? "succeeded",
    quality_conclusion: qualityConclusion,
    scores: row.scores && typeof row.scores === "object" ? row.scores : {},
    quality_evaluation: row.quality_evaluation ?? null,
    evaluation_results: row.evaluation_results ?? null,
    trace_url: traceUrl,
    langfuse_trace_url: traceUrl,
    latency_ms: latency,
    final_attempt_latency_ms: latency,
    final_attempt_http_status: typeof row.final_attempt_http_status === "number" ? row.final_attempt_http_status : null,
    attempt_count: attemptCount,
    final_attempt_id: row.final_attempt_id ? String(row.final_attempt_id) : null,
    is_frozen: true,
    output_ref: row.output_ref ?? null,
  };
}
