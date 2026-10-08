/**
 * Issue #81 — frozen evaluation execution identity in the Console.
 *
 * The Console never re-derives this state: it reports what the backend froze and
 * verified. Missing evidence is shown as "历史契约未记录" instead of being
 * treated as verified.
 */

export type BindingVerification = {
  label: string;
  tone: "pass" | "timeout" | "neutral";
};

export type FrozenBindingEvidence = {
  id?: string;
  version?: string;
  binding_id?: string;
  binding_digest?: string;
  definition_digest?: string;
  content_digest?: string;
  implementation_artifact?: { digest?: string; locator?: string } | null;
  contract_status?: string;
  verification_status?: string;
};

export const HISTORICAL_CONTRACT_LABEL = "历史契约未记录";

/** Report evidence completeness; never claim a legacy Manifest was verified. */
export function bindingVerification(
  binding: FrozenBindingEvidence | undefined,
): BindingVerification {
  // Only an explicitly frozen contract may claim verification: a Manifest that
  // predates Issue #81 carries no contract_status at all.
  if (!binding || binding.contract_status !== "FROZEN_VERIFIED") {
    return { label: HISTORICAL_CONTRACT_LABEL, tone: "neutral" };
  }
  if (!binding.binding_digest || !binding.implementation_artifact?.digest) {
    return { label: "制品未记录", tone: "timeout" };
  }
  return { label: "已冻结校验", tone: "pass" };
}

/**
 * Recovery advice for a frozen-execution failure, mirroring the backend's
 * RECOVERY_HINTS. Keeping the wording identical makes the failure actionable
 * without opening the raw error.
 */
export const FROZEN_FAILURE_RECOVERY: Record<string, string> = {
  EVALUATOR_VERSION_UNAVAILABLE:
    "恢复与冻结版本一致的评测制品后重试，或另建一个 Launch 并选择当前可用版本；禁止改用其他版本继续本次评测。",
  EVALUATOR_BINDING_DIGEST_MISMATCH:
    "当前注册的评测定义与冻结摘要不一致，说明实现已被改动。请恢复冻结制品，或另建 Launch 以冻结当前实现。",
  EVALUATOR_ARTIFACT_UNRESOLVABLE: "冻结的实现制品无法解析到可执行内容。请恢复该制品后重试，或另建 Launch。",
  EVALUATOR_ARTIFACT_DIGEST_MISMATCH:
    "实现制品摘要与冻结记录不一致，存在篡改或版本漂移。请恢复冻结制品，或另建 Launch。",
  EVALUATOR_EXECUTOR_UNSUPPORTED:
    "当前 Runner 不支持该冻结执行器。请使用包含该执行器的 Runner 镜像恢复执行，或另建 Launch。",
  RUNNER_VERSION_MISMATCH: "当前 Runner 身份与冻结记录不一致。请使用冻结的 Runner 镜像恢复执行，或另建 Launch。",
  RUNNER_IDENTITY_UNAVAILABLE: "冻结记录或当前 Runner 缺少可验证的构建身份，无法继续执行。",
};

export function isFrozenIdentityFailure(errorText: string | null | undefined): boolean {
  if (!errorText) return false;
  return Object.keys(FROZEN_FAILURE_RECOVERY).some((code) => errorText.includes(code));
}

export function frozenFailureRecovery(
  errorText: string | null | undefined,
): string | null {
  if (!isFrozenIdentityFailure(errorText)) return null;
  const code = Object.keys(FROZEN_FAILURE_RECOVERY).find((key) =>
    (errorText ?? "").includes(key),
  );
  return code ? FROZEN_FAILURE_RECOVERY[code] : null;
}
