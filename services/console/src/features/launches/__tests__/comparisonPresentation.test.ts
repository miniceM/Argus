import { describe, it, expect } from "vitest";
import {
  isFormalComparisonUsable,
  readFormalComparisonCount,
  readFormalVerdictPresentation,
  readRegressionCount,
} from "../comparisonPresentation";

/**
 * PR 134 review gap F2 — the shared formal-comparison presentation rules.
 *
 * `comparability.comparable` and `formal.available` are two independent
 * eligibility fields. A formal relative verdict or a formal regression count may
 * only be surfaced when *both* are explicitly `true`; a missing field is not
 * comparable evidence and must never become a green "no regression".
 */
describe("readRegressionCount", () => {
  it("reads a real non-negative integer", () => {
    expect(readRegressionCount({ REGRESSION: 0 })).toEqual({ kind: "value", value: 0 });
    expect(readRegressionCount({ REGRESSION: 3 })).toEqual({ kind: "value", value: 3 });
  });

  it("keeps a missing dict unknown", () => {
    expect(readRegressionCount(null)).toEqual({ kind: "missing" });
    expect(readRegressionCount(undefined)).toEqual({ kind: "missing" });
    expect(readRegressionCount({})).toEqual({ kind: "missing" });
  });

  it("keeps a missing or null key unknown", () => {
    expect(readRegressionCount({ REGRESSION: null })).toEqual({ kind: "missing" });
  });

  it("rejects non-numbers and illegal numbers", () => {
    for (const bad of ["0", "3", true, {}, [], -1, -3, 0.5, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(readRegressionCount({ REGRESSION: bad } as any)).toEqual({ kind: "invalid" });
    }
  });
});

describe("isFormalComparisonUsable", () => {
  it("requires both server eligibility fields to be explicitly true", () => {
    expect(
      isFormalComparisonUsable({
        comparability: { comparable: true } as any,
        formal: { available: true } as any,
      }),
    ).toBe(true);
  });

  it("withholds the verdict when comparability is false", () => {
    expect(
      isFormalComparisonUsable({
        comparability: { comparable: false } as any,
        formal: { available: true } as any,
      }),
    ).toBe(false);
  });

  it("withholds the verdict when comparability never came back", () => {
    expect(isFormalComparisonUsable({ formal: { available: true } as any })).toBe(false);
    expect(isFormalComparisonUsable({ comparability: null, formal: { available: true } as any })).toBe(
      false,
    );
    expect(
      isFormalComparisonUsable({
        comparability: undefined,
        formal: { available: true } as any,
      }),
    ).toBe(false);
  });

  it("withholds the verdict when the formal verdict is unavailable or absent", () => {
    expect(
      isFormalComparisonUsable({ comparability: { comparable: true } as any, formal: { available: false } as any }),
    ).toBe(false);
    expect(isFormalComparisonUsable({ comparability: { comparable: true } as any })).toBe(false);
    expect(isFormalComparisonUsable({ comparability: { comparable: true } as any, formal: null })).toBe(
      false,
    );
  });

  it("never coerces truthy non-true values into eligibility", () => {
    expect(
      isFormalComparisonUsable({
        comparability: { comparable: 1 } as any,
        formal: { available: "yes" } as any,
      }),
    ).toBe(false);
    expect(
      isFormalComparisonUsable({
        comparability: { comparable: "true" } as any,
        formal: { available: 1 } as any,
      }),
    ).toBe(false);
  });
});

describe("readFormalComparisonCount", () => {
  it("allows a real count only when the comparison is formally usable", () => {
    const eligible = {
      comparability: { comparable: true } as any,
      formal: { available: true } as any,
    };
    expect(
      readFormalComparisonCount({ ...eligible, classificationCounts: { REGRESSION: 0 } }),
    ).toEqual({ usable: true, countUsable: true, count: { kind: "value", value: 0 } });
    expect(
      readFormalComparisonCount({ ...eligible, classificationCounts: { REGRESSION: 3 } }),
    ).toEqual({ usable: true, countUsable: true, count: { kind: "value", value: 3 } });
  });

  it("keeps a missing or illegal count numeric-unknown even when formally usable", () => {
    const eligible = {
      comparability: { comparable: true } as any,
      formal: { available: true } as any,
    };
    expect(readFormalComparisonCount({ ...eligible, classificationCounts: {} })).toEqual({
      usable: true,
      countUsable: false,
      count: { kind: "missing" },
    });
    expect(readFormalComparisonCount({ ...eligible, classificationCounts: { REGRESSION: "3" } as any })).toEqual(
      {
        usable: true,
        countUsable: false,
        count: { kind: "invalid" },
      },
    );
  });

  it("never reports a count as usable when either eligibility field fails", () => {
    expect(
      readFormalComparisonCount({
        comparability: { comparable: false } as any,
        formal: { available: true } as any,
        classificationCounts: { REGRESSION: 3 },
      }),
    ).toEqual({ usable: false, countUsable: false, count: { kind: "value", value: 3 } });
    expect(
      readFormalComparisonCount({
        formal: { available: true } as any,
        classificationCounts: { REGRESSION: 3 },
      }),
    ).toEqual({ usable: false, countUsable: false, count: { kind: "value", value: 3 } });
    expect(
      readFormalComparisonCount({
        comparability: { comparable: true } as any,
        formal: { available: false } as any,
        classificationCounts: { REGRESSION: 3 },
      }),
    ).toEqual({ usable: false, countUsable: false, count: { kind: "value", value: 3 } });
  });
});

describe("readFormalVerdictPresentation", () => {
  const eligible = {
    comparability: { comparable: true } as any,
    formal: { available: true } as any,
  };

  it("labels a formal regression and names the count", () => {
    const presentation = readFormalVerdictPresentation({
      ...eligible,
      formal: { available: true, verdict: "REGRESSION" } as any,
      classificationCounts: { REGRESSION: 3 },
    });
    expect(presentation.usable).toBe(true);
    expect(presentation.verdict).toBe("REGRESSION");
    expect(presentation.label).toBe("存在回归");
    expect(presentation.tone).toBe("fail");
    expect(presentation.detail).toContain("正式比较发现 3 个回归 Case");
  });

  it("explains a formal regression without inventing a count", () => {
    const presentation = readFormalVerdictPresentation({
      ...eligible,
      formal: { available: true, verdict: "REGRESSION" } as any,
      classificationCounts: {},
    });
    expect(presentation.usable).toBe(true);
    expect(presentation.label).toBe("存在回归");
    expect(presentation.detail).toContain("回归计数未返回或不可用");
    expect(presentation.detail).not.toContain("正式比较发现");
  });

  it("labels improvement and unchanged relative to the frozen quality gate", () => {
    const improvement = readFormalVerdictPresentation({
      ...eligible,
      formal: { available: true, verdict: "IMPROVEMENT" } as any,
      classificationCounts: { REGRESSION: 0 },
    });
    expect(improvement.label).toBe("有所改善");
    expect(improvement.tone).toBe("pass");
    expect(improvement.detail).toContain("仍以其冻结质量结论为准");

    const unchanged = readFormalVerdictPresentation({
      ...eligible,
      formal: { available: true, verdict: "UNCHANGED" } as any,
      classificationCounts: { REGRESSION: 0 },
    });
    expect(unchanged.label).toBe("无变化");
    expect(unchanged.tone).toBe("muted");
    expect(unchanged.detail).toContain("不等于 Candidate 达到发布门禁");
  });

  it("withholds the label and tone when the server never declared eligibility", () => {
    const notComparable = readFormalVerdictPresentation({
      comparability: { comparable: false } as any,
      formal: { available: true, verdict: "REGRESSION" } as any,
      classificationCounts: { REGRESSION: 3 },
    });
    expect(notComparable.usable).toBe(false);
    expect(notComparable.label).toBe("暂不可判定");
    expect(notComparable.tone).toBe("timeout");
    expect(notComparable.detail).toContain("比较前提不满足");

    const noComparability = readFormalVerdictPresentation({
      formal: { available: true, verdict: "REGRESSION" } as any,
      classificationCounts: { REGRESSION: 3 },
    });
    expect(noComparability.usable).toBe(false);
    expect(noComparability.label).toBe("暂不可判定");
    expect(noComparability.tone).toBe("timeout");
    expect(noComparability.detail).toContain("比较资格");

    const noFormal = readFormalVerdictPresentation({
      comparability: { comparable: true } as any,
      formal: { available: false, withheld_reasons: ["EVIDENCE_INCOMPLETE"] } as any,
      classificationCounts: { REGRESSION: 3 },
    });
    expect(noFormal.usable).toBe(false);
    expect(noFormal.label).toBe("暂不可判定");
    expect(noFormal.tone).toBe("timeout");
    expect(noFormal.detail).toContain("证据不足或 Baseline 未绑定");
  });

  it("keeps an unknown verdict undecided even when formally usable", () => {
    const presentation = readFormalVerdictPresentation({
      ...eligible,
      formal: { available: true, verdict: null } as any,
      classificationCounts: { REGRESSION: 0 },
    });
    expect(presentation.usable).toBe(true);
    expect(presentation.verdict).toBe(null);
    expect(presentation.label).toBe("暂不可判定");
    expect(presentation.tone).toBe("timeout");
    expect(presentation.detail).toContain("verdict 缺失或未知");
  });
});
