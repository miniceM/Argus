import React, { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  ArrowLeft,
  Calendar,
  CheckCircle2,
  Clock,
  Cpu,
  FileCode,
  Fingerprint,
  Globe,
  Layers,
  Repeat,
  Shield,
  Zap,
} from "lucide-react";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";
import { SecretRef } from "../../components/SecretRef";
import { JsonViewer } from "../../components/JsonViewer";
import { ErrorState, LoadingState } from "../../components/StateViews";
import { Badge } from "../../components/Badge";
import { Button, PageHeader, Panel } from "../../components/ui/Primitives";

export const AgentVersionDetail: React.FC = () => {
  const { agentId, version } = useParams<{ agentId: string; version: string }>();
  const queryClient = useQueryClient();
  const [actionError, setActionError] = useState<string | null>(null);

  const { data: versionData, isLoading, error, refetch } = useQuery({
    queryKey: queryKeys.agents.version(agentId || "", version || ""),
    queryFn: async () => {
      if (!agentId || !version) throw new Error("缺少 Agent ID 或 Version 参数");
      const res = await api.GET("/api/v1/agent-versions", {
        params: { query: { agent_id: agentId, version } },
      });
      if (res.error) throw res.error;
      // When version query param is provided, backend returns a single AgentVersionResponse object
      return res.data as unknown as import("../../api/schema").components["schemas"]["AgentVersionResponse"];
    },
    enabled: Boolean(agentId && version),
  });

  const archiveMutation = useMutation({
    mutationFn: async () => {
      if (!agentId || !version) return;
      setActionError(null);
      const res = await api.POST("/api/v1/agent-versions/archive", {
        body: { agent_id: agentId, version },
      });
      if (res.error) throw res.error;
      return res.data;
    },
    onSuccess: () => {
      if (agentId && version) {
        queryClient.invalidateQueries({
          queryKey: queryKeys.agents.version(agentId, version),
        });
        queryClient.invalidateQueries({
          queryKey: queryKeys.agents.versions(agentId),
        });
      }
    },
    onError: (err) => {
      setActionError(formatApiError(err));
    },
  });

  if (isLoading) return <LoadingState message="正在加载版本规格快照..." />;
  if (error) return <ErrorState message={formatApiError(error)} onRetry={() => refetch()} />;
  if (!versionData) return <ErrorState message="未找到该版本规格" />;

  return (
    <div className="space-y-6">
      <div className="space-y-3">
        <Link
          to={`/agents/${agentId}`}
          className="inline-flex items-center gap-1.5 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          <span>返回 Agent ({agentId}) 详情</span>
        </Link>
        <PageHeader
          title={(
            <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-primary-border bg-primary-subtle text-primary">
                <Layers className="h-4 w-4" />
              </span>
              <span className="break-all">{agentId}</span>
              <span className="text-muted-foreground font-normal">@</span>
              <span className="font-mono text-primary">{versionData.version}</span>
              <Badge tone={versionData.is_active ? "pass" : "neutral"}>
                {versionData.is_active ? "ACTIVE" : "ARCHIVED"}
              </Badge>
            </span>
          )}
          description={<span className="break-all font-mono">ID: {versionData.id}</span>}
          actions={versionData.is_active ? (
            <Button
              variant="danger"
              onClick={() => {
                if (confirm(`确定要归档版本 ${versionData.version} 吗？归档后将不能用于新的评测 Launch。`)) {
                  archiveMutation.mutate();
                }
              }}
              disabled={archiveMutation.isPending}
            >
              <Archive className="h-3.5 w-3.5" />
              <span>{archiveMutation.isPending ? "正在归档..." : "归档此版本"}</span>
            </Button>
          ) : undefined}
        />
      </div>

      {actionError && (
        <div className="p-3 text-xs bg-fail-subtle border border-fail-border rounded-lg text-fail font-medium">
          {actionError}
        </div>
      )}

      {/* Snapshot Alert */}
      <div className="p-3.5 bg-running-subtle/70 border border-running-border rounded-xl flex items-center gap-3 text-xs text-running-strong">
        <CheckCircle2 className="w-4 h-4 text-running shrink-0" />
        <div>
          <span className="font-semibold">不可变快照保证 (Immutable Specification Snapshot)：</span>
          <span className="ml-1 text-running-strong">
            该版本已固定规格指纹。未来任何配置变更必须注册为新版本号，历史评测将永久锁定此规格。
          </span>
        </div>
      </div>

      {/* Metadata Grid */}
      <Panel className="grid grid-cols-1 gap-4 p-5 shadow-xs md:grid-cols-3">
        <div>
          <span className="text-xs font-medium text-muted-foreground block mb-1">运行环境 (Environment)</span>
          <span className="text-sm font-semibold text-foreground flex items-center gap-1.5">
            <Cpu className="w-4 h-4 text-muted-foreground" />
            <span>{versionData.environment || "未指定 (Default)"}</span>
          </span>
        </div>
        <div>
          <span className="text-xs font-medium text-muted-foreground block mb-1">规格指纹 (Spec Digest)</span>
          <span className="text-xs font-mono font-semibold text-foreground-secondary flex items-center gap-1.5" title={versionData.spec_digest}>
            <Fingerprint className="w-4 h-4 text-muted-foreground shrink-0" />
            <span className="truncate">{versionData.spec_digest}</span>
          </span>
        </div>
        <div>
          <span className="text-xs font-medium text-muted-foreground block mb-1">快照创建时间</span>
          <span className="text-sm font-semibold text-foreground flex items-center gap-1.5">
            <Calendar className="w-4 h-4 text-muted-foreground" />
            <span>{new Date(versionData.created_at).toLocaleString("zh-CN", { hour12: false })}</span>
          </span>
        </div>
      </Panel>

      {/* Invocation Endpoint & Trace */}
      <div className="ui-panel p-5 shadow-xs space-y-4">
        <div className="flex items-center gap-2 border-b border-border pb-3">
          <Globe className="w-4 h-4 text-primary" />
          <h3 className="text-sm font-bold text-foreground">调用网络契约 (Endpoint & Invocation)</h3>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
          <div>
            <span className="text-muted-foreground block mb-1 font-medium">HTTP 调用端点 (Endpoint URL)</span>
            <div className="flex items-center gap-2 font-mono bg-canvas p-2.5 rounded-lg border border-border text-foreground">
              <span className="font-bold text-primary">{versionData.method}</span>
              <span className="truncate">{versionData.endpoint}</span>
            </div>
          </div>

          <div>
            <span className="text-muted-foreground block mb-1 font-medium">协议与链路传播 (Protocol & Trace Propagation)</span>
            <div className="flex items-center gap-4 bg-canvas p-2.5 rounded-lg border border-border text-foreground-secondary">
              <div>
                <span className="text-muted-foreground mr-1">Protocol:</span>
                <span className="font-semibold uppercase">{versionData.protocol}</span>
              </div>
              <div className="h-3 w-px bg-border-strong" />
              <div>
                <span className="text-muted-foreground mr-1">Trace Context:</span>
                <span className="font-mono font-semibold text-pass-strong">
                  {versionData.trace_propagation || "w3c"}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Execution Policy & Secret Ref */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Execution Policy */}
        <div className="ui-panel p-5 shadow-xs space-y-4">
          <div className="flex items-center gap-2 border-b border-border pb-3">
            <Zap className="w-4 h-4 text-timeout" />
            <h3 className="text-sm font-bold text-foreground">执行策略与可靠性保护 (Execution Policy)</h3>
          </div>

          <div className="grid grid-cols-2 gap-3 text-xs">
            <div className="p-3 bg-canvas rounded-lg border border-border">
              <span className="text-muted-foreground block mb-1 flex items-center gap-1">
                <Clock className="w-3.5 h-3.5" /> 超时时间 (Timeout)
              </span>
              <span className="font-semibold text-foreground text-sm">
                {versionData.timeout_seconds} <span className="text-xs text-muted-foreground">秒</span>
              </span>
            </div>

            <div className="p-3 bg-canvas rounded-lg border border-border">
              <span className="text-muted-foreground block mb-1 flex items-center gap-1">
                <Repeat className="w-3.5 h-3.5" /> 最大重试 (Max Retries)
              </span>
              <span className="font-semibold text-foreground text-sm">
                {versionData.max_retries} <span className="text-xs text-muted-foreground">次</span>
              </span>
            </div>

            <div className="p-3 bg-canvas rounded-lg border border-border">
              <span className="text-muted-foreground block mb-1">最大并发 (Concurrency)</span>
              <span className="font-semibold text-foreground text-sm">{versionData.max_concurrency}</span>
            </div>

            <div className="p-3 bg-canvas rounded-lg border border-border">
              <span className="text-muted-foreground block mb-1">速率限制 (Rate Limit)</span>
              <span className="font-semibold text-foreground text-sm">
                {versionData.rate_limit_per_minute || "无限制"}
              </span>
            </div>

            <div className="col-span-2 p-3 bg-canvas rounded-lg border border-border flex items-center justify-between">
              <span className="text-muted-foreground">幂等安全 (Is Idempotent):</span>
              <Badge tone={versionData.is_idempotent ? "pass" : "neutral"}>
                {versionData.is_idempotent ? "YES (允许安全重试)" : "NO (非幂等)"}
              </Badge>
            </div>
          </div>
        </div>

        {/* Security & Credentials */}
        <div className="ui-panel p-5 shadow-xs space-y-4">
          <div className="flex items-center gap-2 border-b border-border pb-3">
            <Shield className="w-4 h-4 text-pass" />
            <h3 className="text-sm font-bold text-foreground">安全与凭据引用 (Security & Secrets)</h3>
          </div>

          <div className="space-y-3 text-xs">
            <div>
              <span className="text-muted-foreground block mb-1.5 font-medium">安全凭据引用 (Credential Ref)</span>
              {versionData.credential_id ? <Link to="/credentials" className="text-primary font-mono">{versionData.credential_id}</Link> : <SecretRef credentialRef={versionData.credential_ref} />}
            </div>

            <div>
              <span className="text-muted-foreground block mb-1 font-medium">产物制品引用 (Artifact Ref)</span>
              <div className="p-2.5 bg-canvas rounded-lg border border-border font-mono text-foreground-secondary truncate">
                {versionData.artifact_ref || "未配置 (无特定 Artifact 镜像或哈希)"}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Contract & Schemas */}
      <div className="space-y-4">
        <div className="flex items-center gap-2">
          <FileCode className="w-4 h-4 text-primary" />
          <h3 className="text-sm font-bold text-foreground">输入/输出映射与 JSON Schema 契约</h3>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div>
            <span className="text-xs font-semibold text-foreground-secondary block mb-1.5">
              请求体映射规则 (Request Mapping)
            </span>
            <JsonViewer
              data={versionData.request_mapping}
              title="Request Mapping (Jinja2/Expression)"
            />
          </div>

          <div>
            <span className="text-xs font-semibold text-foreground-secondary block mb-1.5">
              请求 Schema (Request Schema)
            </span>
            <JsonViewer
              data={versionData.request_schema}
              title="Request JSON Schema"
            />
          </div>

          <div>
            <span className="text-xs font-semibold text-foreground-secondary block mb-1.5">
              响应 Schema (Response Schema)
            </span>
            <JsonViewer
              data={versionData.response_schema}
              title="Response JSON Schema"
            />
          </div>
        </div>
      </div>
    </div>
  );
};
