import React, { useState } from "react";
import {
  AlertCircle,
  Bot,
  Check,
  CheckCircle2,
  Copy,
  Database,
  Download,
  FileCode,
  Scale,
  Sliders,
  Zap,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import {
  snapshotDetailQueryOptions,
  snapshotListQueryOptions,
} from "../launchSnapshotQueries";
import { Button } from "../../../components/ui/Primitives";
import { Badge } from "../../../components/Badge";
import { JsonViewer } from "../../../components/JsonViewer";
import { SnapshotEvidenceBadge } from "../resultSnapshot";
import { bindingVerification } from "../frozenIdentity";
import { useCopyFeedback } from "../useCopyFeedback";

type LaunchResponse = import("../../../api/schema").components["schemas"]["ExperimentLaunchResponse"];
type SnapshotRevision = import("../../../api/schema").components["schemas"]["ResultSnapshotRevisionResponse"];
type SnapshotList = import("../../../api/schema").components["schemas"]["ResultSnapshotListResponse"];
type SnapshotDetail = import("../../../api/schema").components["schemas"]["ResultSnapshotDetailResponse"];

interface ManifestAuditTabProps {
  launch: LaunchResponse;
  activeSnapshot: SnapshotRevision | null;
  onSelectSnapshot?: (snapshotId: string) => void;
}

export const ManifestAuditTab: React.FC<ManifestAuditTabProps> = ({
  launch,
  activeSnapshot,
  onSelectSnapshot,
}) => {
  const manifest = (launch.manifest || {}) as any;
  const manifestAgent = manifest.agent || {};
  const manifestEvaluators = manifest.evaluators || [];
  const manifestRunner = manifest.runner || {};
  const manifestPolicy = manifest.execution_policy || manifest.policy || {};
  const manifestPolicyData = manifest.quality_policy || {};
  const frozenPolicyRules = manifestPolicyData?.rules ?? [];

  // Fetch revisions list — shared entry so this tab can never observe an unverified payload
  const historyQuery = useQuery<SnapshotList>(snapshotListQueryOptions(launch.id));

  const revisions: SnapshotRevision[] = historyQuery.data?.revisions ?? [];
  const [showRawJson, setShowRawJson] = useState(false);
  const [downloadNotice, setDownloadNotice] = useState<string | null>(null);
  // The exported JSON belongs to exactly one (launch, snapshot) pair.
  const copyJson = useCopyFeedback(2000, `${launch.id}:${activeSnapshot?.snapshot_id ?? "none"}`);

  // Fetch complete snapshot detail for JSON export
  const detailQuery = useQuery<SnapshotDetail>(
    snapshotDetailQueryOptions(
      launch.id && activeSnapshot?.snapshot_id
        ? { launchId: launch.id, snapshotId: activeSnapshot.snapshot_id }
        : null,
    ),
  );

  const handleCopyJson = () => {
    const payload = detailQuery.data;
    if (!payload) return;
    void copyJson.copy(JSON.stringify(payload, null, 2));
  };
  // Download JSON
  const handleDownloadJson = () => {
    if (!detailQuery.data || !activeSnapshot) return;
    const payload = detailQuery.data;
    const jsonStr = JSON.stringify(payload, null, 2);
    const blob = new Blob([jsonStr], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `snapshot-${launch.id}-${activeSnapshot.snapshot_id}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    setDownloadNotice("已成功生成并下载原始快照 JSON 文件！");
    setTimeout(() => setDownloadNotice(null), 3000);
  };

  const isExportDisabled = Boolean(!activeSnapshot || detailQuery.isLoading || detailQuery.error || !detailQuery.data);

  const shareUrl = activeSnapshot
    ? `${typeof window !== "undefined" ? window.location.origin : ""}/launches/${launch.id}?snapshot_id=${activeSnapshot.snapshot_id}`
    : "";

  return (
    <div className="space-y-6">
      {/* 1. 四维版本不可变快照卡片 (4D Manifest) */}
      <div className="bg-surface border border-border rounded-xl p-5 shadow-xs space-y-4">
        <div className="flex items-center justify-between border-b border-border pb-3 flex-wrap gap-2">
          <div className="flex items-center gap-2">
            <CheckCircle2 className="w-4 h-4 text-primary" />
            <h3 className="text-sm font-bold text-foreground">
              四维不可变冻结快照 (4D Manifest Snapshot)
            </h3>
            <span
              data-testid="manifest-schema-version"
              className="px-2 py-0.5 rounded text-micro font-mono bg-primary-subtle text-primary border border-primary-border font-semibold"
            >
              Schema v{manifest.schema_version || manifest.manifest_version || "1.0"}
            </span>
          </div>

          <div className="flex items-center gap-2">
            <Button
              variant="secondary"
              className="h-7 text-xs px-2.5"
              onClick={handleCopyJson}
              disabled={isExportDisabled || copyJson.isPending}
              title={copyJson.isError ? "剪贴板写入失败，请检查浏览器权限" : undefined}
            >
              {copyJson.isSuccess ? (
                <Check className="w-3.5 h-3.5 text-pass" />
              ) : copyJson.isError ? (
                <AlertCircle className="w-3.5 h-3.5 text-fail" />
              ) : (
                <Copy className="w-3.5 h-3.5" />
              )}
              <span>{copyJson.isSuccess ? "已复制 JSON" : copyJson.isError ? "复制失败" : "复制 JSON"}</span>
            </Button>
            <Button
              variant="secondary"
              className="h-7 text-xs px-2.5"
              onClick={handleDownloadJson}
              disabled={isExportDisabled}
            >
              <Download className="w-3.5 h-3.5" />
              <span>下载原始 JSON</span>
            </Button>
            <Button
              variant="secondary"
              className="h-7 text-xs px-2.5 text-primary"
              onClick={() => setShowRawJson(!showRawJson)}
            >
              <FileCode className="w-3.5 h-3.5" />
              <span>{showRawJson ? "收起 Manifest JSON" : "查看完整 Manifest JSON"}</span>
            </Button>
          </div>
        </div>

        {downloadNotice && (
          <div className="p-2.5 bg-pass-subtle text-pass border border-pass-border rounded-lg text-xs font-medium">
            {downloadNotice}
          </div>
        )}

        {/* 四维网格 */}
        <div data-testid="manifest-4d-grid" className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 text-xs">
          {/* Dimension 1: Agent Snapshot */}
          <div className="p-3.5 bg-surface-subtle rounded-lg border border-border space-y-2">
            <div className="flex items-center gap-1.5 font-semibold text-foreground">
              <Bot className="w-4 h-4 text-primary" />
              <span>1. Agent 规格快照</span>
            </div>
            <div className="space-y-1 text-muted-foreground">
              <div>
                <span>Agent: </span>
                <strong className="text-foreground">{manifestAgent.id || launch.agent_id}</strong>
              </div>
              <div>
                <span>Version: </span>
                <strong className="font-mono text-foreground">{manifestAgent.version || launch.agent_version}</strong>
              </div>
              <div className="truncate" title={manifestAgent.endpoint || ""}>
                <span>Endpoint: </span>
                <span className="font-mono text-micro">{manifestAgent.endpoint || "-"}</span>
              </div>
              <div className="truncate" title={manifestAgent.spec_digest || ""}>
                <span>Digest: </span>
                <span className="font-mono text-micro">{manifestAgent.spec_digest?.slice(0, 12)}...</span>
              </div>
            </div>
          </div>

          {/* Dimension 2: Dataset Snapshot */}
          <div className="p-3.5 bg-surface-subtle rounded-lg border border-border space-y-2">
            <div className="flex items-center gap-1.5 font-semibold text-foreground">
              <Database className="w-4 h-4 text-primary" />
              <span>2. 数据集快照</span>
            </div>
            <div className="space-y-1 text-muted-foreground">
              <div>
                <span>Name: </span>
                <strong className="text-foreground">{manifest.dataset?.dataset_name || manifest.dataset?.name || launch.dataset_name}</strong>
              </div>
              <div>
                <span>Version: </span>
                <span className="font-mono text-micro truncate block" title={manifest.dataset?.dataset_version || manifest.dataset?.version || ""}>
                  {manifest.dataset?.dataset_version || manifest.dataset?.version || "-"}
                </span>
              </div>
              <div className="truncate" title={manifest.dataset?.snapshot_digest || ""}>
                <span>Digest: </span>
                <span data-testid="dataset-snapshot-digest" className="font-mono text-micro">
                  {manifest.dataset?.snapshot_digest ? `${manifest.dataset.snapshot_digest.slice(0, 12)}...` : "-"}
                </span>
              </div>
              <div>
                <span>Items: </span>
                <span className="font-mono font-semibold text-foreground">
                  {manifest.dataset?.items_count ?? manifest.dataset?.items?.length ?? "-"}
                </span>
              </div>
            </div>
          </div>

          {/* Dimension 3: Evaluators */}
          <div className="p-3.5 bg-surface-subtle rounded-lg border border-border space-y-2">
            <div className="flex items-center gap-1.5 font-semibold text-foreground">
              <Zap className="w-4 h-4 text-primary" />
              <span>3. 评测门禁指标 ({manifestEvaluators.length})</span>
              <span className="sr-only">3. 门禁规则集</span>
            </div>
            <ul className="space-y-1.5 max-h-48 overflow-y-auto">
              {manifestEvaluators.map((ev: any) => {
                const evalId = ev.id || ev.name || "unknown";
                const verification = bindingVerification(ev);
                return (
                  <li key={evalId} className="px-2 py-1.5 rounded border border-border bg-surface text-micro space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-micro font-semibold text-foreground">{evalId}</span>
                      <span className="font-mono text-micro text-muted-foreground">
                        {ev.version ? `v${ev.version}` : "历史契约未记录版本"}
                      </span>
                      {ev.implementation_ref && (
                        <span className="truncate font-mono text-micro text-muted-foreground" title={ev.implementation_ref}>
                          {ev.implementation_ref}
                        </span>
                      )}
                      <Badge tone={verification.tone} data-testid={`binding-verification-${evalId}`}>
                        {verification.label}
                      </Badge>
                    </div>
                    {(ev.binding_digest || ev.definition_digest || ev.implementation_artifact?.digest) && (
                      <details className="text-micro text-muted-foreground">
                        <summary className="cursor-pointer focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus rounded-sm">
                          查看冻结摘要与制品标识
                        </summary>
                        <dl className="mt-1 space-y-0.5 font-mono break-all text-2xs">
                          <div>
                            <dt className="inline text-muted-foreground">Binding: </dt>
                            <dd className="inline">{ev.binding_id ?? "历史契约未记录"}</dd>
                          </div>
                          <div>
                            <dt className="inline text-muted-foreground">Binding Digest: </dt>
                            <dd className="inline">{ev.binding_digest ?? "历史契约未记录"}</dd>
                          </div>
                          <div>
                            <dt className="inline text-muted-foreground">Definition Digest: </dt>
                            <dd className="inline">{ev.definition_digest ?? ev.content_digest ?? "历史契约未记录"}</dd>
                          </div>
                          <div>
                            <dt className="inline text-muted-foreground">Artifact: </dt>
                            <dd className="inline">
                              {ev.implementation_artifact?.digest ?? "历史契约未记录"}
                              {ev.implementation_artifact?.locator ? ` (${ev.implementation_artifact.locator})` : ""}
                            </dd>
                          </div>
                          <div>
                            <dt className="inline text-muted-foreground">Executor: </dt>
                            <dd className="inline">{ev.executor_type ?? "历史契约未记录"}</dd>
                          </div>
                        </dl>
                      </details>
                    )}
                  </li>
                );
              })}
            </ul>
            <p className="text-micro text-muted-foreground">
              冻结身份在创建时校验、执行前再次校验；版本或制品不可用时评测会明确停止并保持 UNKNOWN，不会改用其他版本。
            </p>
          </div>

          {/* Dimension 4: Execution Policy & Runner */}
          <div className="p-3.5 bg-surface-subtle rounded-lg border border-border space-y-2">
            <div className="flex items-center gap-1.5 font-semibold text-foreground">
              <Sliders className="w-4 h-4 text-primary" />
              <span>4. Runner 执行引擎</span>
            </div>
            <div className="space-y-1 text-muted-foreground">
              <div>
                <span>Runner: </span>
                <span data-testid="runner-version" className="font-mono text-micro text-foreground font-semibold">
                  {manifestRunner.runner_version || "1.0.0"}
                </span>
              </div>
              <div className="truncate" title={manifestRunner.mapping_engine_version || ""}>
                <span>Engine: </span>
                <span className="font-mono text-micro">{manifestRunner.mapping_engine_version || "-"}</span>
              </div>
              <div>
                <span>Concurrency: </span>
                <span className="font-semibold text-foreground">{manifestPolicy.max_concurrency ?? manifestRunner.concurrency ?? 1}</span>
              </div>
              <div>
                <span>Timeout: </span>
                <span>{manifestPolicy.timeout_seconds ?? 30}s</span>
              </div>
              <div>
                <span>Retries: </span>
                <span>{manifestPolicy.max_retries ?? 0}</span>
              </div>
            </div>
          </div>
        </div>

        {/* 原始 JSON 查看抽屉 / 面板 */}
        {showRawJson && (
          <div className="pt-3 border-t border-border">
            <span className="text-xs font-semibold text-foreground block mb-2">
              完整冻结快照数据 (launch.manifest & result_snapshot)
            </span>
            <JsonViewer data={detailQuery.data || manifest} title="Immutable Manifest JSON" />
          </div>
        )}
      </div>

      {/* 2. 冻结质量策略详情 */}
      <div className="bg-surface border border-border rounded-xl p-5 shadow-xs space-y-3" data-testid="frozen-quality-policy">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border pb-3">
          <div className="flex items-center gap-2">
            <Scale className="w-4 h-4 text-primary" />
            <h3 className="text-sm font-bold text-foreground">冻结质量策略 (Frozen Quality Policy)</h3>
          </div>
          {manifestPolicyData?.policy_id && (
            <span className="font-mono text-micro text-muted-foreground break-all">
              {manifestPolicyData.policy_id}
              {manifestPolicyData.version ? `@${manifestPolicyData.version}` : ""}
            </span>
          )}
        </div>

        {!manifestPolicyData || frozenPolicyRules.length === 0 ? (
          <p className="text-xs text-muted-foreground" data-testid="frozen-quality-policy-legacy">
            该任务在质量策略独立配置之前创建（历史契约），质量结论沿用当时的复合判定口径，
            无法按当前策略逐条解释。
          </p>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-3 text-micro text-muted-foreground font-mono">
              <span data-testid="frozen-policy-digest">
                策略摘要：{manifestPolicyData.policy_digest ?? "未记录"}
              </span>
              {manifest.measurement_digest && (
                <span data-testid="frozen-measurement-digest">
                  测量口径摘要：{manifest.measurement_digest}
                </span>
              )}
            </div>
            {manifestPolicyData.description && (
              <p className="text-micro text-muted-foreground">{manifestPolicyData.description}</p>
            )}
            <ul className="space-y-1.5">
              {frozenPolicyRules.map((rule: any) => {
                const expression = rule.operator === ">=" || rule.operator === "<="
                  ? `${rule.operator} ${rule.threshold ?? "—"}`
                  : rule.operator === "=="
                    ? `== ${rule.expected_value === null || rule.expected_value === undefined ? "—" : String(rule.expected_value)}`
                    : "仅作为证据";
                return (
                  <li
                    key={`${rule.evaluator_id}-${expression}`}
                    className="flex flex-wrap items-center gap-2 text-xs"
                  >
                    <span className="font-mono font-semibold text-foreground">{rule.evaluator_id}</span>
                    <span className="text-muted-foreground">结果类型 {rule.result_type ?? "numeric"}</span>
                    <span className="font-mono text-muted-foreground">{expression}</span>
                    <Badge tone={rule.required ? "pass" : "neutral"}>
                      {rule.required ? "必要" : "可选诊断"}
                    </Badge>
                    {rule.critical && <Badge tone="timeout">关键</Badge>}
                  </li>
                );
              })}
            </ul>
          </>
        )}
      </div>

      {/* 3. 快照修订历史面板 (Result Snapshot Panel) */}
      <div className="bg-surface border border-border rounded-xl p-5 shadow-xs space-y-4" data-testid="result-snapshot-panel">
        <div className="flex items-center justify-between border-b border-border pb-3 flex-wrap gap-2">
          <div>
            <h3 className="text-sm font-bold text-foreground">结果报告与修订历史 (Result Snapshot & History)</h3>
            <p className="text-xs text-muted-foreground mt-0.5">
              每次终态重试或评测更新均会固定为独立 Revision，保障历史版本防篡改
            </p>
          </div>
          {activeSnapshot && <SnapshotEvidenceBadge state={activeSnapshot.evidence_state} />}
        </div>

        {activeSnapshot && (
          <div className="space-y-2 text-xs">
            <div className="flex items-baseline gap-2 flex-wrap">
              <span className="text-muted-foreground">当前查看版本:</span>
              <strong className="text-foreground" data-testid="snapshot-revision">
                Revision {activeSnapshot.revision}
              </strong>
              {activeSnapshot.is_latest && (
                <span className="text-micro text-pass font-semibold" data-testid="snapshot-latest-tag">
                  (最新)
                </span>
              )}
            </div>
            <p className="text-micro text-muted-foreground font-mono" data-testid="snapshot-id">
              Snapshot ID: {activeSnapshot.snapshot_id}
            </p>
            <p className="text-micro text-muted-foreground" data-testid="snapshot-quality-counts">
              PASS {activeSnapshot.quality_pass_count} · FAIL {activeSnapshot.quality_fail_count} · UNKNOWN {activeSnapshot.quality_unknown_count}
            </p>
            <p className="text-micro text-muted-foreground font-mono break-all" data-testid="snapshot-share-url">
              固定分享链接: {shareUrl}
            </p>
          </div>
        )}

        {revisions.length > 1 && (
          <div className="pt-3 border-t border-border">
            <h4 className="text-xs font-semibold text-foreground mb-2">历史版本列表 (Revision History)</h4>
            <ul className="space-y-1.5" data-testid="snapshot-history">
              {revisions.map((row) => (
                <li key={row.snapshot_id}>
                  <button
                    type="button"
                    onClick={() => onSelectSnapshot?.(row.snapshot_id)}
                    aria-current={row.snapshot_id === activeSnapshot?.snapshot_id ? "true" : undefined}
                    data-testid={`snapshot-revision-${row.revision}`}
                    className={`w-full text-left text-xs px-3 py-2 rounded-lg border transition cursor-pointer flex items-center justify-between ${
                      row.snapshot_id === activeSnapshot?.snapshot_id
                        ? "border-primary bg-primary-subtle text-primary font-semibold"
                        : "border-border bg-surface-subtle text-muted-foreground hover:bg-surface-muted"
                    }`}
                  >
                    <span>
                      Revision {row.revision} {row.is_latest ? "(最新)" : ""}
                    </span>
                    <span className="font-mono text-micro">
                      PASS {row.quality_pass_count} / FAIL {row.quality_fail_count} / UNKNOWN {row.quality_unknown_count}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
};
