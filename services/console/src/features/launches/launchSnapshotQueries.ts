/**
 * Single data entry for immutable result snapshots (Issue 85 and its launch detail
 * sub-issues: page skeleton, KPI, cases, audit and runtime actions).
 *
 * Three consumers (LaunchDetail, ResultSnapshotPanel, ManifestAuditTab) used to declare the
 * *same* React Query key with three *different* query functions and three different opinions
 * about what a valid payload is. Because React Query deduplicates by key, one bypassing
 * consumer could place an unverified payload in the shared success cache that the other
 * consumers would then render and export.
 *
 * Every read of a snapshot now goes through this module: one key, one fetch, one validation,
 * and any failure throws so the invalid payload never reaches `data`.
 */

import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { SnapshotContractError, validateSnapshotDetail } from "./launchReportView";

type SnapshotList = import("../../api/schema").components["schemas"]["ResultSnapshotListResponse"];
type SnapshotDetail = import("../../api/schema").components["schemas"]["ResultSnapshotDetailResponse"];

export const snapshotListKey = (launchId: string) =>
  [...queryKeys.launches.all, "result-snapshots", launchId] as const;

export const snapshotDetailKey = (launchId: string, snapshotId: string | null) =>
  [...queryKeys.launches.all, "result-snapshot", launchId, snapshotId ?? "none"] as const;

/**
 * Marks a snapshot id the user typed/pasted/kept in the URL rather than one discovered from
 * the revision directory. Used to decide when a detail read must happen even though the
 * directory has not resolved the id yet.
 */
export type SnapshotDetailRequest = {
  launchId: string;
  snapshotId: string;
};

export async function fetchSnapshotList(launchId: string): Promise<SnapshotList> {
  const res = await api.GET("/api/v1/experiment-launches/{launch_id}/result-snapshots", {
    params: { path: { launch_id: launchId } },
  });
  if (res.error) throw res.error;
  const data = res.data as SnapshotList | null;
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new SnapshotContractError("快照版本列表响应数据无效", launchId, "");
  }
  if (data.revisions != null && !Array.isArray(data.revisions)) {
    throw new SnapshotContractError("快照版本列表响应缺少 revisions 列表", launchId, "");
  }
  return data;
}

/**
 * Fetches and *verifies* one frozen report.
 *
 * The returned value is the untouched API DTO — export must never serialise the projected
 * case view — and by the time it lands in React Query `data` the payload is known to belong
 * to exactly the requested (launch_id, snapshot_id).
 */
export async function fetchSnapshotDetail(
  launchId: string,
  snapshotId: string,
): Promise<SnapshotDetail> {
  const res = await api.GET(
    "/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}",
    { params: { path: { launch_id: launchId, snapshot_id: snapshotId } } },
  );
  if (res.error) throw res.error;

  const validation = validateSnapshotDetail(res.data, launchId, snapshotId);
  if (!validation.isValid) {
    throw new SnapshotContractError(
      validation.error ?? "快照响应数据无效",
      launchId,
      snapshotId,
    );
  }
  return res.data as SnapshotDetail;
}

export function snapshotListQueryOptions(launchId: string | null | undefined) {
  return {
    queryKey: snapshotListKey(launchId ?? "none"),
    enabled: Boolean(launchId),
    queryFn: () => fetchSnapshotList(launchId!),
  };
}

/**
 * `request.snapshotId` is resolved by the caller, so an explicitly requested revision keeps
 * its own cache entry even while the revision directory is still loading or failing.
 */
export function snapshotDetailQueryOptions(
  request: SnapshotDetailRequest | null | undefined,
) {
  const launchId = request?.launchId ?? null;
  const snapshotId = request?.snapshotId ?? null;
  return {
    queryKey: snapshotDetailKey(launchId ?? "none", snapshotId),
    enabled: Boolean(launchId && snapshotId),
    queryFn: () => fetchSnapshotDetail(launchId!, snapshotId!),
  };
}
