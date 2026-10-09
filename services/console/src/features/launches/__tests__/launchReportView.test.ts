import { describe, it, expect } from "vitest";
import { projectFrozenCase, validateSnapshotDetail } from "../launchReportView";

describe("launchReportView pure functions", () => {
  it("preserves explicit null measurements without falling back to any live values", () => {
    const frozenRecord = {
      dataset_item_id: "item-001",
      execution_status: "succeeded",
      eval_status: "succeeded",
      quality_conclusion: "fail",
      scores: { pii: 0.5 },
      trace_url: null,
      latency_ms: null,
      final_attempt_id: null,
    };

    const projected = projectFrozenCase(frozenRecord, "stable-exec-1");

    expect(projected.id).toBe("stable-exec-1");
    expect(projected.dataset_item_id).toBe("item-001");
    expect(projected.trace_url).toBeNull();
    expect(projected.latency_ms).toBeNull();
    expect(projected.final_attempt_latency_ms).toBeNull();
    expect(projected.final_attempt_id).toBeNull();
    expect(projected.is_frozen).toBe(true);
  });

  it("preserves real zero latency and real zero scores", () => {
    const frozenRecord = {
      dataset_item_id: "item-002",
      latency_ms: 0,
      scores: { latency: 0 },
      cost_evidence: { attempt_count: 0 },
    };

    const projected = projectFrozenCase(frozenRecord, null);

    expect(projected.latency_ms).toBe(0);
    expect(projected.final_attempt_latency_ms).toBe(0);
    expect(projected.scores.latency).toBe(0);
    expect(projected.attempt_count).toBe(0);
  });

  it("validates identity and marks missing items as contract error", () => {
    const invalidLaunch = validateSnapshotDetail(
      { launch_id: "other-launch", snapshot_id: "s1", items: [] },
      "l1",
      "s1",
    );
    expect(invalidLaunch.isValid).toBe(false);
    expect(invalidLaunch.error).toContain("快照归属 Launch 不一致");

    const invalidSnapshot = validateSnapshotDetail(
      { launch_id: "l1", snapshot_id: "other-snap", items: [] },
      "l1",
      "s1",
    );
    expect(invalidSnapshot.isValid).toBe(false);
    expect(invalidSnapshot.error).toContain("快照 ID 不一致");

    const missingItems = validateSnapshotDetail(
      { launch_id: "l1", snapshot_id: "s1" },
      "l1",
      "s1",
    );
    expect(missingItems.isValid).toBe(false);
    expect(missingItems.error).toContain("items 字段缺失或非数组");

    const validEmpty = validateSnapshotDetail(
      { launch_id: "l1", snapshot_id: "s1", items: [] },
      "l1",
      "s1",
    );
    expect(validEmpty.isValid).toBe(true);
    expect(validEmpty.items).toEqual([]);
  });
});
