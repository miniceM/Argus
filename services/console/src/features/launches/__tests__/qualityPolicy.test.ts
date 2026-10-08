/**
 * Issue #83 — the client-side mirror of the frozen quality policy contract.
 *
 * These are pure-function tests: the same rules the server enforces in
 * services/eval-runner/app/quality_policy.py must be visible in the form
 * before a request is sent.
 */
import { describe, it, expect } from "vitest";
import {
  defaultRuleDraft,
  describeRuleDraft,
  isEvidenceOnly,
  toQualityPolicyRequest,
  validateRuleDraft,
  validateRuleDrafts,
  type RuleDraft,
} from "../qualityPolicy";

type VersionInfo = import("../../../api/schema").components["schemas"]["EvaluatorVersionInfo"];

const version = (overrides: Partial<VersionInfo> = {}): VersionInfo =>
  ({
    version: "1.0.0",
    result_type: "numeric",
    scope: "item",
    threshold: 0.8,
    direction: "higher_is_better",
    critical: false,
    executor_type: "builtin_python",
    content_digest: "digest",
    release_eligible: true,
    ...overrides,
  }) as VersionInfo;

const numericRule = (overrides: Partial<RuleDraft> = {}): RuleDraft => ({
  required: true,
  operator: ">=",
  value: "0.8",
  critical: false,
  resultType: "numeric",
  note: "",
  ...overrides,
});

describe("defaultRuleDraft", () => {
  it("seeds a higher-is-better numeric rule from the frozen threshold", () => {
    expect(defaultRuleDraft(version())).toEqual({
      required: true,
      operator: ">=",
      value: "0.8",
      critical: false,
      resultType: "numeric",
      note: "",
    });
  });

  it("seeds a lower-is-better numeric rule with <=", () => {
    const draft = defaultRuleDraft(version({ direction: "lower_is_better", threshold: 0.2 }));
    expect(draft.operator).toBe("<=");
    expect(draft.value).toBe("0.2");
  });

  it("seeds boolean and categorical rules as explicit matches with no value yet", () => {
    expect(defaultRuleDraft(version({ result_type: "boolean" }))).toMatchObject({
      operator: "==",
      value: "",
      required: true,
    });
    expect(defaultRuleDraft(version({ result_type: "categorical" }))).toMatchObject({ operator: "==" });
  });

  // Text can be recorded but must never decide quality.
  it("seeds a text metric as evidence only", () => {
    const draft = defaultRuleDraft(version({ result_type: "text" }));
    expect(isEvidenceOnly(draft.resultType)).toBe(true);
    expect(draft.required).toBe(false);
    expect(draft.operator).toBe("");
  });
});

describe("validateRuleDraft", () => {
  it("accepts a valid numeric rule", () => {
    expect(validateRuleDraft(numericRule(), version())).toBeNull();
  });

  it("rejects a non-finite or empty threshold", () => {
    expect(validateRuleDraft(numericRule({ value: "" }), version())).toMatch(/有限数值/);
    expect(validateRuleDraft(numericRule({ value: "abc" }), version())).toMatch(/有限数值/);
  });

  it("rejects a required rule with no operator", () => {
    expect(validateRuleDraft(numericRule({ operator: "" }), version())).toMatch(/比较运算符/);
  });

  it("rejects an operator that does not match the result type", () => {
    // numeric accepts >= and <=, never ==
    expect(validateRuleDraft(numericRule({ operator: "==" }), version())).toMatch(/不支持运算符 ==/);
    // boolean / categorical accept only ==
    expect(
      validateRuleDraft(numericRule({ operator: ">=", resultType: "boolean" }), version()),
    ).toMatch(/不支持运算符 >=/);
  });

  // A boolean rule is an explicit typed match, never a coercion: the Console
  // must not let "1" or "true" stand in for the boolean true.
  it("requires an explicit true/false for boolean rules", () => {
    expect(validateRuleDraft(numericRule({ resultType: "boolean", operator: "==", value: "" }), version()))
      .toMatch(/true 或 false/);
    expect(validateRuleDraft(numericRule({ resultType: "boolean", operator: "==", value: "1" }), version()))
      .toMatch(/必须是 true 或 false/);
    expect(validateRuleDraft(numericRule({ resultType: "boolean", operator: "==", value: "true" }), version()))
      .toBeNull();
    expect(validateRuleDraft(numericRule({ resultType: "boolean", operator: "==", value: "false" }), version()))
      .toBeNull();
  });

  it("requires a categorical expected value inside the declared enum", () => {
    const categorical = version({ result_type: "categorical", category_values: ["greeting", "refund"] });
    expect(validateRuleDraft(numericRule({ resultType: "categorical", operator: "==", value: "" }), categorical))
      .toMatch(/期望取值/);
    expect(validateRuleDraft(numericRule({ resultType: "categorical", operator: "==", value: "greeting" }), categorical))
      .toBeNull();
    expect(validateRuleDraft(numericRule({ resultType: "categorical", operator: "==", value: "unknown" }), categorical))
      .toMatch(/不在该指标的分类枚举内/);
  });

  it("never rejects a text metric, because text is evidence only", () => {
    expect(
      validateRuleDraft(numericRule({ resultType: "text", operator: "", required: false }), version({ result_type: "text" })),
    ).toBeNull();
  });

  it("allows an optional rule with no operator to stand as a diagnostic", () => {
    expect(validateRuleDraft(numericRule({ operator: "", required: false }), version())).toBeNull();
  });
});

describe("validateRuleDrafts", () => {
  it("passes when at least one rule is required and legal", () => {
    expect(validateRuleDrafts({ intent_match: numericRule() }, { intent_match: version() })).toEqual([]);
  });

  it("reports the rule that is missing a value", () => {
    const issues = validateRuleDrafts(
      { intent_match: numericRule(), pii_safe: numericRule({ value: "" }) },
      { intent_match: version(), pii_safe: version() },
    );
    expect(issues).toHaveLength(1);
    expect(issues[0].evaluatorId).toBe("pii_safe");
  });

  // Fail-closed: with nothing required, no quality conclusion can be reached.
  it("rejects a policy in which every rule is optional", () => {
    const issues = validateRuleDrafts(
      { intent_match: numericRule({ required: false }) },
      { intent_match: version() },
    );
    expect(issues.some((issue) => issue.evaluatorId === "")).toBe(true);
  });

  it("rejects an empty policy", () => {
    const issues = validateRuleDrafts({}, {});
    expect(issues.some((issue) => issue.evaluatorId === "")).toBe(true);
  });
});

describe("toQualityPolicyRequest", () => {
  it("maps a numeric rule to a threshold", () => {
    expect(toQualityPolicyRequest({ intent_match: numericRule({ value: "0.85" }) })).toEqual({
      rules: [
        {
          evaluator_id: "intent_match",
          operator: ">=",
          threshold: 0.85,
          expected_value: null,
          result_type: "numeric",
          required: true,
          critical: false,
          note: null,
        },
      ],
    });
  });

  it("maps a lower-is-better rule to <=", () => {
    const request = toQualityPolicyRequest({ latency: numericRule({ operator: "<=", value: "0.2" }) });
    expect(request.rules[0].operator).toBe("<=");
    expect(request.rules[0].threshold).toBe(0.2);
  });

  it("maps a boolean rule to a typed expected_value, not a number", () => {
    const request = toQualityPolicyRequest({
      pii_safe: numericRule({ resultType: "boolean", operator: "==", value: "true" }),
    });
    expect(request.rules[0].expected_value).toBe(true);
    expect(request.rules[0].threshold).toBeNull();
  });

  it("keeps an evidence-only rule with a null operator", () => {
    const request = toQualityPolicyRequest({
      transcript: { ...numericRule(), resultType: "text", operator: "", required: false },
    });
    expect(request.rules[0].operator).toBeNull();
    expect(request.rules[0].threshold).toBeNull();
    expect(request.rules[0].required).toBe(false);
  });

  it("carries the critical flag and a trimmed note", () => {
    const request = toQualityPolicyRequest({
      intent_match: numericRule({ critical: true, note: "  发布门禁  " }),
    });
    expect(request.rules[0].critical).toBe(true);
    expect(request.rules[0].note).toBe("发布门禁");
  });
});

describe("describeRuleDraft", () => {
  it("describes each rule shape in plain language", () => {
    expect(describeRuleDraft(numericRule({ value: "0.8" }))).toBe("数值 >= 0.8");
    expect(describeRuleDraft(numericRule({ operator: "<=", value: "0.2" }))).toBe("数值 <= 0.2");
    expect(describeRuleDraft(numericRule({ resultType: "boolean", operator: "==", value: "true" })))
      .toBe("显式匹配 == true");
    expect(describeRuleDraft(numericRule({ operator: "", required: false })))
      .toBe("仅作为证据记录，不参与判定");
  });
});
