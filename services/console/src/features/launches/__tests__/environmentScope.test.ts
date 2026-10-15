import { describe, expect, it } from "vitest";
import { readFrozenEnvironment, environmentReasonMessage } from "../environmentScope";

describe("冻结环境资格", () => {
  it("只接受规范冻结证据，不向管理列或 Agent 环境回落", () => {
    expect(readFrozenEnvironment({ comparison: { environment: "qa-blue" } })).toBe("qa-blue");
    for (const manifest of [null, {}, { agent: { environment: "production" } },
      { comparison: { environment: " Production " } }, { comparison: { environment: "bad/env" } },
      { comparison: { environment: 42 } }, { comparison: { environment: "" } }]) {
      expect(readFrozenEnvironment(manifest)).toBeNull();
    }
  });
  it("历史无效绑定原因可读且未知原因不被隐藏", () => {
    expect(environmentReasonMessage(["SNAPSHOT_ENVIRONMENT_MISSING"])).toContain("缺少冻结环境");
    expect(environmentReasonMessage(["SNAPSHOT_ENVIRONMENT_INVALID"])).toContain("无效");
    expect(environmentReasonMessage(["BASELINE_ENVIRONMENT_MISMATCH"])).toContain("不一致");
    expect(environmentReasonMessage(["FUTURE_REASON"])).toBe("FUTURE_REASON");
  });
});
