import type { Page } from "@playwright/test";

type SnapshotFixtureItem = {
  dataset_item_id: string;
  quality_conclusion?: string | null;
};

type SingleSnapshotOptions = {
  launchId: string;
  snapshotId: string;
  items: unknown[];
  revision?: number;
};

/**
 * Installs the two immutable-result endpoints for a terminal Launch fixture.
 * Register this after any broad `/experiment-launches**` mock so Playwright's
 * newest-first route matching does not return the Launch DTO for snapshot URLs.
 */
export async function installSingleSnapshotFixture(
  page: Page,
  { launchId, snapshotId, items, revision = 1 }: SingleSnapshotOptions,
) {
  const rows = items as SnapshotFixtureItem[];
  const passCount = rows.filter(
    (item) => item.quality_conclusion?.toLowerCase() === "pass",
  ).length;
  const failCount = rows.filter(
    (item) => item.quality_conclusion?.toLowerCase() === "fail",
  ).length;
  const unknownCount = items.length - passCount - failCount;
  const createdAt = "2026-10-10T10:00:00Z";
  const sourceResultDigest = `sha256:e2e-result-${snapshotId}`;
  const manifestDigest = `sha256:e2e-manifest-${snapshotId}`;
  const evidenceState = "COMPLETE";

  await page.route(
    `**/api/v1/experiment-launches/${launchId}/result-snapshots`,
    (route) =>
      route.fulfill({
        json: {
          launch_id: launchId,
          latest_snapshot_id: snapshotId,
          latest_revision: revision,
          revisions: [
            {
              snapshot_id: snapshotId,
              revision,
              created_at: createdAt,
              source_result_digest: sourceResultDigest,
              manifest_digest: manifestDigest,
              evidence_state: evidenceState,
              evidence_reasons: [],
              releasable: failCount === 0 && unknownCount === 0,
              total_cases: items.length,
              quality_pass_count: passCount,
              quality_fail_count: failCount,
              quality_unknown_count: unknownCount,
              is_latest: true,
            },
          ],
        },
      }),
  );

  await page.route(
    `**/api/v1/experiment-launches/${launchId}/result-snapshots/${snapshotId}`,
    (route) =>
      route.fulfill({
        json: {
          launch_id: launchId,
          snapshot_id: snapshotId,
          revision,
          created_at: createdAt,
          source_result_digest: sourceResultDigest,
          manifest_digest: manifestDigest,
          evidence_state: evidenceState,
          evidence_reasons: [],
          releasable: failCount === 0 && unknownCount === 0,
          versions: {},
          summary: {},
          items,
        },
      }),
  );
}
