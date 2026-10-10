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
  dispatch_generation: number | null;
  final_attempt_id: string | null;
  is_frozen: boolean;
  output_ref?: any;
}

export interface SnapshotValidationResult {
  isValid: boolean;
  error?: string;
  items?: any[];
  /**
   * `identity` mismatches or a missing/oversized payload body; `http` is a transport failure
   * that must never be rewritten into an empty-but-valid report.
   */
  kind?: "contract" | "http";
}

/**
 * Error raised inside the shared snapshot query function.
 *
 * Throwing (instead of returning `null` / `[]`) is what keeps an unverified response out of
 * the React Query success cache, so *every* consumer of the shared cache key fails closed.
 */
export class SnapshotContractError extends Error {
  readonly kind = "contract";
  readonly launchId: string;
  readonly snapshotId: string;

  constructor(message: string, launchId: string, snapshotId: string) {
    super(message);
    this.name = "SnapshotContractError";
    this.launchId = launchId;
    this.snapshotId = snapshotId;
  }
}

/**
 * True when the payload carries the HTTP status of a transport failure.
 * Only used to keep "endpoint not found" distinct from "payload is untrustworthy".
 */
export function isHttpStatusError(error: unknown, status: number): boolean {
  if (!error || typeof error !== "object") return false;
  const candidate = (error as { response?: { status?: unknown } }).response;
  return candidate?.status === status;
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

/**
 * Validates that a SnapshotDetail response matches the expected identity and contract shape.
 *
 * Identity is mandatory, not advisory: `launch_id` / `snapshot_id` must both be present and
 * exactly equal to the requested values, otherwise a response for another revision would be
 * rendered (and exported) under the identity the user asked for.
 */
export function validateSnapshotDetail(
  detail: any,
  expectedLaunchId: string,
  expectedSnapshotId: string,
): SnapshotValidationResult {
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) {
    return { isValid: false, error: "快照响应数据无效", kind: "contract" };
  }

  if (!isNonEmptyString(expectedLaunchId) || !isNonEmptyString(expectedSnapshotId)) {
    return { isValid: false, error: "缺少快照请求上下文 (launch_id / snapshot_id)", kind: "contract" };
  }

  if (!isNonEmptyString(detail.launch_id) || detail.launch_id !== expectedLaunchId) {
    return {
      isValid: false,
      error: `快照归属 Launch 不一致: 期望 ${expectedLaunchId}, 实际 ${detail.launch_id ?? "(缺失)"}`,
      kind: "contract",
    };
  }

  if (!isNonEmptyString(detail.snapshot_id) || detail.snapshot_id !== expectedSnapshotId) {
    return {
      isValid: false,
      error: `快照 ID 不一致: 期望 ${expectedSnapshotId}, 实际 ${detail.snapshot_id ?? "(缺失)"}`,
      kind: "contract",
    };
  }

  if (!("items" in detail) || !Array.isArray(detail.items)) {
    return {
      isValid: false,
      error: "快照明细缺少用例记录 (items 字段缺失或非数组)",
      kind: "contract",
    };
  }

  for (const row of detail.items) {
    if (!row || typeof row !== "object" || Array.isArray(row)) {
      return { isValid: false, error: "快照用例记录为空或格式无效", kind: "contract" };
    }
    if (!isNonEmptyString(row.dataset_item_id)) {
      return { isValid: false, error: "快照用例记录缺少 dataset_item_id", kind: "contract" };
    }
  }

  return {
    isValid: true,
    items: detail.items,
    kind: "contract",
  };
}

/**
 * Pure projector for a frozen case item.
 * Historical rows must not be given a live execution ID: doing so would attach a newer
 * mutable Attempt timeline to an older immutable snapshot.
 */
export function projectFrozenCase(row: Record<string, any>): ReportCaseItem {
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
    id: null,
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
    dispatch_generation: typeof row.dispatch_generation === "number" ? row.dispatch_generation : null,
    final_attempt_id: row.final_attempt_id ? String(row.final_attempt_id) : null,
    is_frozen: true,
    output_ref: row.output_ref ?? null,
  };
}
