import { describe, it, expect } from "vitest";
import {
  resolveMetricDirection,
  evaluateMetricChange,
} from "../metricComparison";

describe("metricComparison resolution", () => {
  it("treats missing direction on call_cost as neutral change, never invented improvement", () => {
    const meta = { id: "call_cost", result_type: "numeric" };
    const direction = resolveMetricDirection(meta);

    expect(direction.isNumeric).toBe(true);
    expect(direction.hasKnownDirection).toBe(false);

    const change = evaluateMetricChange(0.5, 0.2, direction);
    expect(change.statusLabel).toBe("变化");
    expect(change.statusTone).toBe("neutral");
  });

  it("recognizes explicit lower_is_better and higher_is_better directions", () => {
    const lowerMeta = {
      id: "latency_ms",
      result_type: "numeric",
      direction: "lower_is_better",
    };
    const lowerDir = resolveMetricDirection(lowerMeta);
    expect(lowerDir.hasKnownDirection).toBe(true);
    expect(lowerDir.isLowerBetter).toBe(true);
    expect(evaluateMetricChange(100, 80, lowerDir).statusLabel).toBe("提升");
    expect(evaluateMetricChange(100, 120, lowerDir).statusLabel).toBe("退化");

    const higherMeta = {
      id: "accuracy",
      direction: "higher_is_better",
    };
    const higherDir = resolveMetricDirection(higherMeta);
    expect(higherDir.hasKnownDirection).toBe(true);
    expect(higherDir.isLowerBetter).toBe(false);
    expect(evaluateMetricChange(0.7, 0.9, higherDir).statusLabel).toBe("提升");
    expect(evaluateMetricChange(0.7, 0.5, higherDir).statusLabel).toBe("下降");
  });

  it("handles equal scores as 持平 with pass tone", () => {
    const meta = { id: "metric_a", direction: "higher_is_better" };
    const dir = resolveMetricDirection(meta);
    const change = evaluateMetricChange(0.8, 0.8, dir);
    expect(change.statusLabel).toBe("持平");
    expect(change.statusTone).toBe("pass");
  });
});
