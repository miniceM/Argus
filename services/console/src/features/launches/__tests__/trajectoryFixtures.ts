import type { components } from "../../../api/schema";

type Diagnosis = components["schemas"]["TrajectoryDiagnosisResponse"];
const digest = `sha256:${"a".repeat(64)}`;

export const trajectoryFixture = (snapshotId = "snapshot-1"): Diagnosis => ({
  launch_id: "launch-1", snapshot_id: snapshotId, dataset_item_id: "case-1",
  acquisition_state: "READY", reason_code: null, execution_status: "succeeded",
  quality_conclusion: "fail", required_evidence_status: "COMPLETE",
  evidence_ref: { artifact_id: "artifact-1", trajectory_digest: digest, trajectory_contract_digest: digest, schema_version: "1.0" },
  completeness: { status: "PARTIAL", warnings: ["ARGUMENTS_NOT_COLLECTED"], capabilities: {
    tool_executions: "COMPLETE", tool_requests: "COMPLETE", tool_arguments: "PARTIAL", ordering: "PARTIAL", timing: "PARTIAL", human_events: "NONE",
  } },
  langfuse_trace_url: "https://langfuse.example/trace/trace-1",
  step_argument_display_json: {
    root: "null", "exec-a": '{"amount":0.12345678901234567890123456789,"n":9007199254740993}', "exec-b": "null",
  },
  trajectory: {
    schema_version: "1.0", input_redacted: null, output_redacted: null,
    scope: { launch_id: "launch-1", item_execution_id: "item-1", dataset_item_id: "case-1", attempt_id: "attempt-1",
      dispatch_generation: 1, trace_id: "a".repeat(32), root_observation_id: "root" },
    provenance: { provider: "langfuse", source_profile_id: "native-otel-v1", source_profile_version: "1.0",
      schema_version: "1.0", assembler_version: "1.0", tool_mapping_version: "1.0", tool_mapping_digest: digest,
      redaction_policy_version: "1.0", redaction_policy_digest: digest, trajectory_contract_digest: digest, trajectory_digest: digest },
    completeness: { status: "PARTIAL", warnings: ["ARGUMENTS_NOT_COLLECTED"], capabilities: {
      tool_executions: "COMPLETE", tool_requests: "COMPLETE", tool_arguments: "PARTIAL", ordering: "PARTIAL", timing: "PARTIAL", human_events: "NONE",
    } },
    steps: [
      { step_id: "root", observation_id: "root", parent_step_id: null, index: 0, type: "AGENT", raw_name: "Remote HTTP",
        tool_identity: null, phase: "EVENT", status: "UNKNOWN", started_at: null, ended_at: null, duration_ms: null,
        arguments_redacted: null, argument_visibility: {}, result_summary: null, error_code: null, causal_predecessor_ids: [] },
      { step_id: "exec-a", observation_id: "exec-a", parent_step_id: "root", index: 1, type: "TOOL", raw_name: `crm-${snapshotId}`,
        tool_identity: "crm_lookup", phase: "EXECUTED", status: "SUCCEEDED", started_at: null, ended_at: null, duration_ms: 1,
        arguments_redacted: { n: 9007199254740992, amount: 0.12345678901234568 }, argument_visibility: { "$.n": "AVAILABLE", "$.amount": "AVAILABLE" },
        result_summary: null, error_code: null, causal_predecessor_ids: [] },
      { step_id: "exec-b", observation_id: "exec-b", parent_step_id: "root", index: 2, type: "TOOL", raw_name: "ledger_read",
        tool_identity: "ledger_read", phase: "BLOCKED", status: "CANCELLED", started_at: null, ended_at: null, duration_ms: null,
        arguments_redacted: null, argument_visibility: { "$.customer_id": "REDACTED" }, result_summary: null, error_code: "APPROVAL_REQUIRED",
        causal_predecessor_ids: ["exec-a"] },
    ],
  },
  rule_results: [
    { evaluator_id: "trajectory.forbid_tool", evaluator_version: "1.0.0", binding_id: "binding-fail", typed_status: "succeeded",
      value: false, conclusion: "fail", frozen_conclusion: "fail", required: true, critical: true,
      reason_code: "TRAJECTORY_RULE_VIOLATED", evidence_step_ids: ["exec-a"] },
    { evaluator_id: "trajectory.require_tool", evaluator_version: "1.0.0", binding_id: "binding-pass", typed_status: "succeeded",
      value: true, conclusion: "pass", frozen_conclusion: "pass", required: true, critical: false, reason_code: "TRAJECTORY_RULE_SATISFIED", evidence_step_ids: ["exec-a"] },
    { evaluator_id: "trajectory.tool_order", evaluator_version: "1.0.0", binding_id: "binding-unknown", typed_status: "no_result",
      value: null, conclusion: "unknown", frozen_conclusion: "unknown", required: false, critical: false,
      reason_code: "TRAJECTORY_CAUSAL_ORDER_UNPROVEN", evidence_step_ids: [] },
  ],
});
