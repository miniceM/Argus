/** 管理环境默认值不能补造 Snapshot 的冻结环境证据。 */
export function readFrozenEnvironment(manifest: unknown): string | null {
  if (!manifest || typeof manifest !== "object") return null;
  const comparison = (manifest as Record<string, unknown>).comparison;
  if (!comparison || typeof comparison !== "object") return null;
  const value = (comparison as Record<string, unknown>).environment;
  return typeof value === "string" && /^[a-z0-9][a-z0-9._-]{0,63}$/.test(value) ? value : null;
}

export function environmentReasonMessage(codes: readonly string[]): string {
  const messages: Record<string, string> = {
    SNAPSHOT_ENVIRONMENT_MISSING: "快照缺少冻结环境证据，请在目标环境重新评测",
    SNAPSHOT_ENVIRONMENT_INVALID: "快照冻结环境无效，请在目标环境重新评测",
    BASELINE_ENVIRONMENT_MISMATCH: "基线快照与目标环境不一致，请重新评测后按当前 revision 替换",
  };
  return codes.map((code) => messages[code] ?? code).join("；");
}
