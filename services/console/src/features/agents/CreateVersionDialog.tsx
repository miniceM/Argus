import React, { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { X, Plus, Layers } from "lucide-react";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";

interface CreateVersionDialogProps {
  agentId: string;
  isOpen: boolean;
  onClose: () => void;
  onSuccess?: () => void;
}

export const CreateVersionDialog: React.FC<CreateVersionDialogProps> = ({
  agentId,
  isOpen,
  onClose,
  onSuccess,
}) => {
  const queryClient = useQueryClient();
  const [version, setVersion] = useState("");
  const [endpoint, setEndpoint] = useState("http://127.0.0.1:18081/invoke");
  const [timeoutSeconds, setTimeoutSeconds] = useState(30.0);
  const [maxRetries, setMaxRetries] = useState(2);
  const [maxConcurrency, setMaxConcurrency] = useState(4);
  const [rateLimitPerMinute, setRateLimitPerMinute] = useState(600);
  const [isIdempotent, setIsIdempotent] = useState(false);
  const [credentialRef, setCredentialRef] = useState("");
  const [artifactRef, setArtifactRef] = useState("");
  const [environment, setEnvironment] = useState("staging");
  const [requestMappingStr, setRequestMappingStr] = useState('{"query": "input.user_message"}');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: async () => {
      setErrorMsg(null);
      let requestMapping = {};
      try {
        if (requestMappingStr.trim()) {
          requestMapping = JSON.parse(requestMappingStr);
        }
      } catch {
        throw new Error("Request Mapping 必须是合法的 JSON 格式");
      }

      const res = await api.POST("/api/v1/agent-versions", {
        body: {
          agent_id: agentId,
          version: version.trim(),
          endpoint: endpoint.trim(),
          protocol: "HTTP_JSON",
          method: "POST",
          request_mapping: requestMapping,
          timeout_seconds: Number(timeoutSeconds),
          max_retries: Number(maxRetries),
          max_concurrency: Number(maxConcurrency),
          rate_limit_per_minute: Number(rateLimitPerMinute),
          is_idempotent: isIdempotent,
          credential_ref: credentialRef.trim() || null,
          artifact_ref: artifactRef.trim() || null,
          environment: environment.trim() || null,
          trace_propagation: "W3C",
        },
      });

      if (res.error) {
        throw res.error;
      }
      return res.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.versions(agentId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.detail(agentId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.agents.list() });
      onClose();
      if (onSuccess) onSuccess();
    },
    onError: (err) => {
      setErrorMsg(formatApiError(err));
    },
  });

  if (!isOpen) return null;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!version.trim() || !endpoint.trim()) {
      setErrorMsg("请填写版本号与远程 HTTP 端点");
      return;
    }
    mutation.mutate();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 backdrop-blur-xs p-4 overflow-y-auto">
      <div className="bg-white rounded-xl shadow-xl border border-slate-200 w-full max-w-2xl overflow-hidden animate-in fade-in zoom-in-95 duration-150 my-8">
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-200">
          <div className="flex items-center gap-2 text-slate-800">
            <Layers className="w-5 h-5 text-indigo-600" />
            <h2 className="text-base font-bold">创建 AgentVersion 规格快照</h2>
          </div>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-slate-600 rounded-lg p-1 hover:bg-slate-100 transition-colors"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-6 space-y-4 max-h-[80vh] overflow-y-auto">
          {errorMsg && (
            <div className="p-3 text-xs bg-rose-50 border border-rose-200 rounded-lg text-rose-700 font-medium">
              {errorMsg}
            </div>
          )}

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">
                版本号 (Tag) <span className="text-rose-500">*</span>
              </label>
              <input
                type="text"
                required
                placeholder="e.g. 1.0.0 或 v2"
                value={version}
                onChange={(e) => setVersion(e.target.value)}
                className="w-full px-3 py-2 text-sm border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-600 font-mono"
              />
              <p className="text-[11px] text-slate-400 mt-1">创建后将永久冻结且不可变</p>
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">运行环境</label>
              <input
                type="text"
                placeholder="e.g. production / staging"
                value={environment}
                onChange={(e) => setEnvironment(e.target.value)}
                className="w-full px-3 py-2 text-sm border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-600"
              />
            </div>
          </div>

          <div>
            <label className="block text-xs font-semibold text-slate-700 mb-1">
              远程调用端点 (HTTP POST) <span className="text-rose-500">*</span>
            </label>
            <input
              type="url"
              required
              placeholder="http://agent-host:8080/invoke"
              value={endpoint}
              onChange={(e) => setEndpoint(e.target.value)}
              className="w-full px-3 py-2 text-sm border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-600 font-mono"
            />
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">超时时间 (秒)</label>
              <input
                type="number"
                min={1}
                max={600}
                value={timeoutSeconds}
                onChange={(e) => setTimeoutSeconds(Number(e.target.value))}
                className="w-full px-3 py-1.5 text-sm border border-slate-300 rounded-lg font-mono"
              />
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">最大重试次数</label>
              <input
                type="number"
                min={0}
                max={10}
                value={maxRetries}
                onChange={(e) => setMaxRetries(Number(e.target.value))}
                className="w-full px-3 py-1.5 text-sm border border-slate-300 rounded-lg font-mono"
              />
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">最大并发数</label>
              <input
                type="number"
                min={1}
                max={50}
                value={maxConcurrency}
                onChange={(e) => setMaxConcurrency(Number(e.target.value))}
                className="w-full px-3 py-1.5 text-sm border border-slate-300 rounded-lg font-mono"
              />
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">每分钟限流 (RPM)</label>
              <input
                type="number"
                min={1}
                max={10000}
                value={rateLimitPerMinute}
                onChange={(e) => setRateLimitPerMinute(Number(e.target.value))}
                className="w-full px-3 py-1.5 text-sm border border-slate-300 rounded-lg font-mono"
              />
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">凭据引用 (SecretRef)</label>
              <input
                type="text"
                placeholder="env://API_TOKEN 或 vault://path"
                value={credentialRef}
                onChange={(e) => setCredentialRef(e.target.value)}
                className="w-full px-3 py-2 text-sm border border-slate-300 rounded-lg font-mono"
              />
              <p className="text-[11px] text-slate-400 mt-1">仅存储引用标识，禁止存入明文 Secret</p>
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-700 mb-1">产物标识 (ArtifactRef)</label>
              <input
                type="text"
                placeholder="git commit SHA 或 docker image digest"
                value={artifactRef}
                onChange={(e) => setArtifactRef(e.target.value)}
                className="w-full px-3 py-2 text-sm border border-slate-300 rounded-lg font-mono"
              />
            </div>
          </div>

          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="text-xs font-semibold text-slate-700">请求映射关系 (Request Mapping JSON)</label>
            </div>
            <textarea
              rows={3}
              value={requestMappingStr}
              onChange={(e) => setRequestMappingStr(e.target.value)}
              className="w-full px-3 py-2 text-xs font-mono border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-600"
            />
            <p className="text-[11px] text-slate-400 mt-1">
              点路径映射关系，例如：{`{"query": "input.user_message"}`}
            </p>
          </div>

          <div className="flex items-center gap-2 pt-1">
            <input
              type="checkbox"
              id="is_idempotent"
              checked={isIdempotent}
              onChange={(e) => setIsIdempotent(e.target.checked)}
              className="rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
            />
            <label htmlFor="is_idempotent" className="text-xs text-slate-700 select-none">
              该端点为幂等调用（发生 Read Timeout 时允许根据策略重试）
            </label>
          </div>

          <div className="flex items-center justify-end gap-3 pt-4 border-t border-slate-100">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-100 rounded-lg transition-colors cursor-pointer"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={mutation.isPending}
              className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg shadow-xs transition-colors disabled:opacity-50 cursor-pointer"
            >
              <Plus className="w-4 h-4" />
              <span>{mutation.isPending ? "创建中..." : "确认创建版本"}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
