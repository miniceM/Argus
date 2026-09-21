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
      {/* Navigation Breadcrumbs */}
      <div>
        <Link
          to={`/agents/${agentId}`}
          className="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-500 hover:text-slate-800 transition-colors mb-2"
        >
          <ArrowLeft className="w-3.5 h-3.5" />
          <span>返回 Agent ({agentId}) 详情</span>
        </Link>
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-indigo-50 border border-indigo-100 flex items-center justify-center text-indigo-600 font-bold">
              <Layers className="w-6 h-6" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-xl font-bold text-slate-900 tracking-tight">
                  {agentId}
                  <span className="text-slate-400 font-normal mx-1.5">@</span>
                  <span className="font-mono text-indigo-600">{versionData.version}</span>
                </h2>
                {versionData.is_active ? (
                  <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200">
                    ACTIVE
                  </span>
                ) : (
                  <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold bg-slate-100 text-slate-500 border border-slate-200">
                    ARCHIVED
                  </span>
                )}
              </div>
              <p className="text-xs text-slate-500 mt-0.5 font-mono">ID: {versionData.id}</p>
            </div>
          </div>

          <div className="flex items-center gap-3 self-start sm:self-auto">
            {versionData.is_active && (
              <button
                type="button"
                onClick={() => {
                  if (confirm(`确定要归档版本 ${versionData.version} 吗？归档后将不能用于新的评测 Launch。`)) {
                    archiveMutation.mutate();
                  }
                }}
                disabled={archiveMutation.isPending}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold text-rose-700 bg-rose-50 hover:bg-rose-100 border border-rose-200 rounded-lg transition-colors cursor-pointer disabled:opacity-50"
              >
                <Archive className="w-3.5 h-3.5" />
                <span>{archiveMutation.isPending ? "正在归档..." : "归档此版本"}</span>
              </button>
            )}
          </div>
        </div>
      </div>

      {actionError && (
        <div className="p-3 text-xs bg-rose-50 border border-rose-200 rounded-lg text-rose-700 font-medium">
          {actionError}
        </div>
      )}

      {/* Snapshot Alert */}
      <div className="p-3.5 bg-blue-50/70 border border-blue-200 rounded-xl flex items-center gap-3 text-xs text-blue-800">
        <CheckCircle2 className="w-4 h-4 text-blue-600 shrink-0" />
        <div>
          <span className="font-semibold">不可变快照保证 (Immutable Specification Snapshot)：</span>
          <span className="ml-1 text-blue-700">
            该版本已固定规格指纹。未来任何配置变更必须注册为新版本号，历史评测将永久锁定此规格。
          </span>
        </div>
      </div>

      {/* Metadata Grid */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 bg-white p-5 rounded-xl border border-slate-200 shadow-xs">
        <div>
          <span className="text-xs font-medium text-slate-400 block mb-1">运行环境 (Environment)</span>
          <span className="text-sm font-semibold text-slate-800 flex items-center gap-1.5">
            <Cpu className="w-4 h-4 text-slate-400" />
            <span>{versionData.environment || "未指定 (Default)"}</span>
          </span>
        </div>
        <div>
          <span className="text-xs font-medium text-slate-400 block mb-1">规格指纹 (Spec Digest)</span>
          <span className="text-xs font-mono font-semibold text-slate-700 flex items-center gap-1.5" title={versionData.spec_digest}>
            <Fingerprint className="w-4 h-4 text-slate-400 shrink-0" />
            <span className="truncate">{versionData.spec_digest}</span>
          </span>
        </div>
        <div>
          <span className="text-xs font-medium text-slate-400 block mb-1">快照创建时间</span>
          <span className="text-sm font-semibold text-slate-800 flex items-center gap-1.5">
            <Calendar className="w-4 h-4 text-slate-400" />
            <span>{new Date(versionData.created_at).toLocaleString("zh-CN", { hour12: false })}</span>
          </span>
        </div>
      </div>

      {/* Invocation Endpoint & Trace */}
      <div className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-4">
        <div className="flex items-center gap-2 border-b border-slate-100 pb-3">
          <Globe className="w-4 h-4 text-indigo-600" />
          <h3 className="text-sm font-bold text-slate-900">调用网络契约 (Endpoint & Invocation)</h3>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
          <div>
            <span className="text-slate-400 block mb-1 font-medium">HTTP 调用端点 (Endpoint URL)</span>
            <div className="flex items-center gap-2 font-mono bg-slate-50 p-2.5 rounded-lg border border-slate-200 text-slate-800">
              <span className="font-bold text-indigo-600">{versionData.method}</span>
              <span className="truncate">{versionData.endpoint}</span>
            </div>
          </div>

          <div>
            <span className="text-slate-400 block mb-1 font-medium">协议与链路传播 (Protocol & Trace Propagation)</span>
            <div className="flex items-center gap-4 bg-slate-50 p-2.5 rounded-lg border border-slate-200 text-slate-700">
              <div>
                <span className="text-slate-400 mr-1">Protocol:</span>
                <span className="font-semibold uppercase">{versionData.protocol}</span>
              </div>
              <div className="h-3 w-px bg-slate-300" />
              <div>
                <span className="text-slate-400 mr-1">Trace Context:</span>
                <span className="font-mono font-semibold text-emerald-700">
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
        <div className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-4">
          <div className="flex items-center gap-2 border-b border-slate-100 pb-3">
            <Zap className="w-4 h-4 text-amber-500" />
            <h3 className="text-sm font-bold text-slate-900">执行策略与可靠性保护 (Execution Policy)</h3>
          </div>

          <div className="grid grid-cols-2 gap-3 text-xs">
            <div className="p-3 bg-slate-50 rounded-lg border border-slate-100">
              <span className="text-slate-400 block mb-1 flex items-center gap-1">
                <Clock className="w-3.5 h-3.5" /> 超时时间 (Timeout)
              </span>
              <span className="font-semibold text-slate-800 text-sm">
                {versionData.timeout_seconds} <span className="text-xs text-slate-500">秒</span>
              </span>
            </div>

            <div className="p-3 bg-slate-50 rounded-lg border border-slate-100">
              <span className="text-slate-400 block mb-1 flex items-center gap-1">
                <Repeat className="w-3.5 h-3.5" /> 最大重试 (Max Retries)
              </span>
              <span className="font-semibold text-slate-800 text-sm">
                {versionData.max_retries} <span className="text-xs text-slate-500">次</span>
              </span>
            </div>

            <div className="p-3 bg-slate-50 rounded-lg border border-slate-100">
              <span className="text-slate-400 block mb-1">最大并发 (Concurrency)</span>
              <span className="font-semibold text-slate-800 text-sm">{versionData.max_concurrency}</span>
            </div>

            <div className="p-3 bg-slate-50 rounded-lg border border-slate-100">
              <span className="text-slate-400 block mb-1">速率限制 (Rate Limit)</span>
              <span className="font-semibold text-slate-800 text-sm">
                {versionData.rate_limit_per_minute || "无限制"}
              </span>
            </div>

            <div className="col-span-2 p-3 bg-slate-50 rounded-lg border border-slate-100 flex items-center justify-between">
              <span className="text-slate-500">幂等安全 (Is Idempotent):</span>
              <span
                className={`px-2 py-0.5 rounded font-semibold ${
                  versionData.is_idempotent
                    ? "bg-emerald-50 text-emerald-700 border border-emerald-200"
                    : "bg-slate-200 text-slate-600"
                }`}
              >
                {versionData.is_idempotent ? "YES (允许安全重试)" : "NO (非幂等)"}
              </span>
            </div>
          </div>
        </div>

        {/* Security & Credentials */}
        <div className="bg-white rounded-xl border border-slate-200 p-5 shadow-xs space-y-4">
          <div className="flex items-center gap-2 border-b border-slate-100 pb-3">
            <Shield className="w-4 h-4 text-emerald-600" />
            <h3 className="text-sm font-bold text-slate-900">安全与凭据引用 (Security & Secrets)</h3>
          </div>

          <div className="space-y-3 text-xs">
            <div>
              <span className="text-slate-400 block mb-1.5 font-medium">安全凭据引用 (Credential Ref)</span>
              <SecretRef credentialRef={versionData.credential_ref} />
            </div>

            <div>
              <span className="text-slate-400 block mb-1 font-medium">产物制品引用 (Artifact Ref)</span>
              <div className="p-2.5 bg-slate-50 rounded-lg border border-slate-200 font-mono text-slate-700 truncate">
                {versionData.artifact_ref || "未配置 (无特定 Artifact 镜像或哈希)"}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Contract & Schemas */}
      <div className="space-y-4">
        <div className="flex items-center gap-2">
          <FileCode className="w-4 h-4 text-indigo-600" />
          <h3 className="text-sm font-bold text-slate-900">输入/输出映射与 JSON Schema 契约</h3>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div>
            <span className="text-xs font-semibold text-slate-600 block mb-1.5">
              请求体映射规则 (Request Mapping)
            </span>
            <JsonViewer
              data={versionData.request_mapping}
              title="Request Mapping (Jinja2/Expression)"
            />
          </div>

          <div>
            <span className="text-xs font-semibold text-slate-600 block mb-1.5">
              请求 Schema (Request Schema)
            </span>
            <JsonViewer
              data={versionData.request_schema}
              title="Request JSON Schema"
            />
          </div>

          <div>
            <span className="text-xs font-semibold text-slate-600 block mb-1.5">
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
