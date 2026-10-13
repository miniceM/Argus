/**
 * Single validated data entry for comparison and case output reads (issue 119 gap U1).
 *
 * The comparison endpoints resolve a snapshot from the request, but nothing in the
 * HTTP layer guarantees the payload body belongs to the identity the page asked for.
 * The KPI overview, the paginated comparison table, the inline case accordion and
 * the case drawer used to declare their own fetch with different (or missing)
 * identity checks while sharing React Query keys, so one weak consumer could place
 * a foreign payload in the shared success cache that every other consumer then
 * rendered and exported.
 *
 * Every comparison read now goes through this module: one fetch, one validation,
 * and any identity/shape violation throws before the payload can reach `data`.
 * `items: []` remains a legal, formally empty report.
 */

import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";

type Comparison = import("../../api/schema").components["schemas"]["ComparisonResponse"];
type ComparisonCaseOutput = import("../../api/schema").components["schemas"]["ComparisonCaseOutputResponse"];

/**
 * Raised when a comparison payload does not prove it belongs to the requested
 * identity. Carries the requested identity (never the foreign payload), so the
 * error stays explainable and retryable without leaking foreign evidence.
 */
export class LaunchReportContractError extends Error {
  readonly kind = "contract";
  readonly launchId: string;
  readonly snapshotId: string;
  readonly datasetItemId?: string;

  constructor(
    message: string,
    request: { launchId: string; snapshotId: string; datasetItemId?: string },
  ) {
    super(message);
    this.name = "LaunchReportContractError";
    this.launchId = request.launchId;
    this.snapshotId = request.snapshotId;
    this.datasetItemId = request.datasetItemId;
  }
}

export type ComparisonPageRequest = {
  launchId: string;
  snapshotId: string;
  classification?: string | null;
  cursor?: number | null;
  limit?: number;
};

export type ComparisonCaseRequest = {
  launchId: string;
  snapshotId: string;
  datasetItemId: string;
};

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

/**
 * Fetches and *verifies* one comparison page.
 *
 * The returned value is the untouched generated DTO: no counts, versions or
 * classifications are invented here. By the time it lands in React Query `data`
 * the payload is known to belong to exactly the requested
 * `(launch_id, candidate_snapshot_id)`.
 */
export async function fetchComparisonPage(
  request: ComparisonPageRequest,
): Promise<Comparison> {
  const { launchId, snapshotId } = request;
  if (!isNonEmptyString(launchId) || !isNonEmptyString(snapshotId)) {
    throw new LaunchReportContractError(
      "缺少比较请求上下文 (launch_id / snapshot_id)",
      { launchId: launchId ?? "", snapshotId: snapshotId ?? "" },
    );
  }

  const res = await api.GET("/api/v1/experiment-launches/{launch_id}/comparison", {
    params: {
      path: { launch_id: launchId },
      query: {
        snapshot_id: snapshotId,
        classification: request.classification ?? undefined,
        limit: request.limit ?? 50,
        cursor: request.cursor ?? undefined,
      },
    },
  });
  if (res.error) throw res.error;

  const data = res.data as Comparison | null;
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new LaunchReportContractError("比较响应数据无效", { launchId, snapshotId });
  }
  if (!isNonEmptyString(data.launch_id) || data.launch_id !== launchId) {
    throw new LaunchReportContractError(
      `比较响应归属 Launch 不一致: 期望 ${launchId}, 实际 ${data.launch_id ?? "(缺失)"}`,
      { launchId, snapshotId },
    );
  }
  if (!isNonEmptyString(data.candidate_snapshot_id) || data.candidate_snapshot_id !== snapshotId) {
    throw new LaunchReportContractError(
      `比较响应归属快照不一致: 期望 ${snapshotId}, 实际 ${data.candidate_snapshot_id ?? "(缺失)"}`,
      { launchId, snapshotId },
    );
  }
  if (!data.summary || typeof data.summary !== "object" || Array.isArray(data.summary)) {
    throw new LaunchReportContractError("比较响应缺少有效的运行摘要 (summary)", {
      launchId,
      snapshotId,
    });
  }
  if (!("items" in data) || !Array.isArray(data.items)) {
    throw new LaunchReportContractError("比较响应缺少用例列表 (items 字段缺失或非数组)", {
      launchId,
      snapshotId,
    });
  }
  return data;
}

/**
 * Fetches and *verifies* one frozen case output for the comparison drawer and the
 * inline accordion. The response must prove the full triple identity
 * `(launch_id, candidate_snapshot_id, dataset_item_id)` and expose both consumable
 * output sides. A one-sided MISSING case stays legal; a foreign identity is not.
 */
export async function fetchComparisonCase(
  request: ComparisonCaseRequest,
): Promise<ComparisonCaseOutput> {
  const { launchId, snapshotId, datasetItemId } = request;
  if (
    !isNonEmptyString(launchId) ||
    !isNonEmptyString(snapshotId) ||
    !isNonEmptyString(datasetItemId)
  ) {
    throw new LaunchReportContractError(
      "缺少 Case 请求上下文 (launch_id / snapshot_id / dataset_item_id)",
      { launchId: launchId ?? "", snapshotId: snapshotId ?? "", datasetItemId: datasetItemId ?? "" },
    );
  }

  const res = await api.GET("/api/v1/experiment-launches/{launch_id}/comparison/case", {
    params: {
      path: { launch_id: launchId },
      query: { snapshot_id: snapshotId, dataset_item_id: datasetItemId },
    },
  });
  if (res.error) throw res.error;

  const data = res.data as ComparisonCaseOutput | null;
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new LaunchReportContractError("Case 输出响应数据无效", {
      launchId,
      snapshotId,
      datasetItemId,
    });
  }
  if (!isNonEmptyString(data.launch_id) || data.launch_id !== launchId) {
    throw new LaunchReportContractError(
      `Case 输出归属 Launch 不一致: 期望 ${launchId}, 实际 ${data.launch_id ?? "(缺失)"}`,
      { launchId, snapshotId, datasetItemId },
    );
  }
  if (!isNonEmptyString(data.candidate_snapshot_id) || data.candidate_snapshot_id !== snapshotId) {
    throw new LaunchReportContractError(
      `Case 输出归属快照不一致: 期望 ${snapshotId}, 实际 ${data.candidate_snapshot_id ?? "(缺失)"}`,
      { launchId, snapshotId, datasetItemId },
    );
  }
  if (!isNonEmptyString(data.dataset_item_id) || data.dataset_item_id !== datasetItemId) {
    throw new LaunchReportContractError(
      `Case 输出归属用例不一致: 期望 ${datasetItemId}, 实际 ${data.dataset_item_id ?? "(缺失)"}`,
      { launchId, snapshotId, datasetItemId },
    );
  }
  if (!data.baseline || typeof data.baseline !== "object" || Array.isArray(data.baseline)) {
    throw new LaunchReportContractError("Case 输出缺少可消费的 Baseline 侧", {
      launchId,
      snapshotId,
      datasetItemId,
    });
  }
  if (!data.candidate || typeof data.candidate !== "object" || Array.isArray(data.candidate)) {
    throw new LaunchReportContractError("Case 输出缺少可消费的 Candidate 侧", {
      launchId,
      snapshotId,
      datasetItemId,
    });
  }
  return data;
}

/**
 * KPI comparison read: a single DTO with its own cache key. It deliberately keeps
 * a different key and data shape from the paginated table below — the two reads
 * must not be merged for the sake of sharing code.
 */
export const comparisonKpiKey = (launchId: string, snapshotId: string) =>
  [...queryKeys.launches.comparison(launchId, snapshotId, "ALL"), "kpi-initial"] as const;

export function comparisonKpiQueryOptions(
  request: { launchId: string; snapshotId: string } | null | undefined,
) {
  const launchId = request?.launchId ?? null;
  const snapshotId = request?.snapshotId ?? null;
  return {
    queryKey: comparisonKpiKey(launchId ?? "none", snapshotId ?? "unresolved"),
    enabled: Boolean(launchId && snapshotId),
    queryFn: () =>
      fetchComparisonPage({ launchId: launchId!, snapshotId: snapshotId!, limit: 50 }),
  };
}

/**
 * Paginated comparison read: infinite pages under the existing table key. Each
 * page is validated independently, so a foreign page can never be appended to the
 * verified first page.
 */
export function comparisonPagesQueryOptions(
  request: { launchId: string; snapshotId: string } | null | undefined,
  filter: string,
) {
  const launchId = request?.launchId ?? null;
  const snapshotId = request?.snapshotId ?? null;
  return {
    queryKey: queryKeys.launches.comparison(launchId ?? "none", snapshotId ?? "unresolved", filter),
    initialPageParam: 0,
    queryFn: ({ pageParam }: { pageParam: number }) =>
      fetchComparisonPage({
        launchId: launchId!,
        snapshotId: snapshotId!,
        classification: filter === "ALL" ? undefined : filter,
        cursor: pageParam,
        limit: 50,
      }),
    getNextPageParam: (lastPage: Comparison) => lastPage.next_cursor ?? undefined,
    enabled: Boolean(launchId && snapshotId),
  };
}

/**
 * One case output entry shared by the inline accordion and the drawer: same key,
 * same fetch, same validation, so neither consumer can poison the other.
 */
export function caseOutputQueryOptions(request: {
  launchId: string;
  snapshotId: string | null;
  datasetItemId: string;
}) {
  const { launchId, snapshotId, datasetItemId } = request;
  return {
    queryKey: queryKeys.launches.case(launchId, snapshotId ?? "live", datasetItemId),
    enabled: Boolean(launchId && snapshotId && datasetItemId),
    queryFn: async (): Promise<ComparisonCaseOutput | null> => {
      // A pure live row has no frozen revision to compare against; the accordion
      // keeps showing live evidence instead of inventing a comparison output.
      if (!snapshotId) return null;
      return fetchComparisonCase({ launchId, snapshotId, datasetItemId });
    },
  };
}
