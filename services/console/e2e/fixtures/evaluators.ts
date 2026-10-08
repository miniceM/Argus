/**
 * Shared Evaluator catalog fixture for Playwright specs.
 *
 * Mirrors the Issue #80 contract: each Evaluator exposes every immutable
 * version plus release eligibility, so the Console can pin an exact version.
 */

export type EvaluatorVersionFixture = {
  version: string;
  result_type: string;
  scope: string;
  threshold: number;
  direction: string;
  critical: boolean;
  input_contract: Record<string, unknown>;
  output_contract: Record<string, unknown>;
  param_schema: Record<string, unknown>;
  implementation_ref: string;
  executor_type: string;
  content_digest: string;
  release_eligible: boolean;
  eligibility_reasons: string[];
  eligibility_messages: string[];
};

export type EvaluatorFixture = {
  id: string;
  name: string;
  version: string;
  scope: string;
  threshold: number;
  description: string;
  default_selected: boolean;
  composed_of: string[];
  direction: string;
  critical: boolean;
  result_type: string;
  definition_source: string;
  execution_owner: string;
  implementation_ref: string;
  executor_type: string;
  content_digest: string;
  release_eligible: boolean;
  eligibility_reasons: string[];
  default_version: string;
  versions: EvaluatorVersionFixture[];
};

const versionFixture = (
  version: string,
  overrides: Partial<EvaluatorVersionFixture> = {},
): EvaluatorVersionFixture => ({
  version,
  result_type: "numeric",
  scope: "item",
  threshold: 1.0,
  direction: "higher_is_better",
  critical: false,
  input_contract: { type: "object", description: "Agent 输出与期望输出" },
  output_contract: { type: "number", minimum: 0, maximum: 1 },
  param_schema: { type: "object", properties: {} },
  implementation_ref: `builtin:e2e@${version}`,
  executor_type: "builtin_python",
  content_digest: `sha256:e2e-digest-${version}`,
  release_eligible: true,
  eligibility_reasons: [],
  eligibility_messages: [],
  ...overrides,
});

export const itemEvaluatorFixture = (
  id: string,
  description: string,
  overrides: Partial<EvaluatorFixture> = {},
): EvaluatorFixture => {
  const versions = overrides.versions ?? [versionFixture("1.0.0")];
  return {
    id,
    name: `${id} 指标`,
    version: versions[0].version,
    scope: "item",
    threshold: versions[0].threshold,
    description,
    default_selected: false,
    composed_of: [],
    direction: "higher_is_better",
    critical: false,
    result_type: "numeric",
    definition_source: "ARGUS_BUILTIN",
    execution_owner: "ARGUS",
    implementation_ref: versions[0].implementation_ref,
    executor_type: "builtin_python",
    content_digest: versions[0].content_digest,
    release_eligible: true,
    eligibility_reasons: [],
    default_version: versions[0].version,
    versions,
    ...overrides,
  };
};

export const DIAGNOSTIC_IDS = [
  "escalation_match",
  "intent_match",
  "pii_safe",
  "required_tool_match",
];

/** The default catalog used by most Console specs. */
export function buildEvaluatorCatalog(): EvaluatorFixture[] {
  return [
    ...DIAGNOSTIC_IDS.map((id) =>
      itemEvaluatorFixture(id, `${id} 诊断指标`, { default_selected: true }),
    ),
    itemEvaluatorFixture("overall_pass", "历史复合指标", {
      default_selected: false,
      composed_of: DIAGNOSTIC_IDS,
    }),
    itemEvaluatorFixture("run_pass_rate", "整体通过率（派生运行指标）", {
      default_selected: false,
      scope: "run",
      release_eligible: false,
      eligibility_reasons: ["EXECUTION_OWNER_NOT_ARGUS"],
      versions: [
        versionFixture("1.0.0", {
          scope: "run",
          release_eligible: false,
          eligibility_reasons: ["EXECUTION_OWNER_NOT_ARGUS"],
          eligibility_messages: ["派生运行指标不能作为用例指标选择。"],
        }),
      ],
    }),
  ];
}
