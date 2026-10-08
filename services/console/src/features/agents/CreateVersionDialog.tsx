import React, { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Layers } from "lucide-react";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";
import { FieldHelp } from "../../components/FieldHelp";
import { Button, Field, SelectInput, TextArea, TextInput } from "../../components/ui/Primitives";
import { Modal } from "../../components/ui/Overlay";
import { AGENT_VERSION_FIELD_HELPS } from "./helpDocs";

const FORM_ID = "create-version-form";

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
  const [credentialId, setCredentialId] = useState("");
  const credentials = useQuery({ queryKey: ["credentials"], enabled: isOpen, queryFn: async () => {
    const result = await api.GET("/api/v1/credentials");
    if (result.error || !result.data) throw new Error("无法加载凭据");
    return result.data;
  }});
  const [artifactRef, setArtifactRef] = useState("");
  const [environment, setEnvironment] = useState("staging");
  const [requestMappingStr, setRequestMappingStr] = useState('{"query": "input.user_message"}');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  // Per-field messages, because the form-level banner cannot say which
  // control is wrong. See `Field`'s `error` prop.
  const [fieldErrors, setFieldErrors] = useState<{
    version?: string;
    endpoint?: string;
  }>({});
  const formRef = useRef<HTMLFormElement>(null);

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
          credential_ref: credentialId ? null : credentialRef.trim() || null,
          credential_id: credentialId || null,
          artifact_ref: artifactRef.trim() || null,
          environment: environment.trim().toLowerCase() || null,
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

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const next: typeof fieldErrors = {};
    if (!version.trim()) next.version = "版本号为必填项。";
    if (!endpoint.trim()) next.endpoint = "远程调用端点为必填项。";
    setFieldErrors(next);
    if (Object.keys(next).length > 0) {
      setErrorMsg("请填写版本号与远程 HTTP 端点");
      return;
    }
    mutation.mutate();
  };

  // Focus lands here rather than inside the submit handler: `aria-invalid` is
  // only in the DOM once React has committed the new field errors, so querying
  // for it during the submit finds nothing.
  useEffect(() => {
    if (Object.keys(fieldErrors).length === 0) return;
    formRef.current
      ?.querySelector<HTMLElement>('[aria-invalid="true"]')
      ?.focus();
  }, [fieldErrors]);

  return (
    <Modal
      open={isOpen}
      onClose={onClose}
      title="创建 AgentVersion 规格快照"
      icon={<Layers aria-hidden="true" className="w-5 h-5 text-primary" />}
      // A half-written version snapshot must not be abandoned mid-flight.
      dismissable={!mutation.isPending}
      className="sm:max-w-modal-lg"
      footer={
        <>
          <Button type="button" variant="secondary" onClick={onClose}>
            取消
          </Button>
          <Button type="submit" form={FORM_ID} disabled={mutation.isPending}>
            <Plus aria-hidden="true" className="w-4 h-4" />
            <span>{mutation.isPending ? "创建中..." : "确认创建版本"}</span>
          </Button>
        </>
      }
    >
        <form
          id={FORM_ID}
          ref={formRef}
          onSubmit={handleSubmit}
          className="p-6 space-y-4"
        >
          {errorMsg && (
            <div className="p-3 text-xs bg-fail-subtle border border-fail-border rounded-lg text-fail font-medium">
              {errorMsg}
            </div>
          )}

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <Field
              label="版本号 (Tag)"
              required
              labelSuffix={<FieldHelp {...AGENT_VERSION_FIELD_HELPS.version} />}
              hint="创建后将永久冻结且不可变"
              error={fieldErrors.version}
            >
              {({ id, ...aria }) => (
                <TextInput
                  {...aria}
                  id={id}
                  type="text"
                  required
                  placeholder="e.g. 1.0.0 或 v2"
                  value={version}
                  onChange={(e) => {
                    setVersion(e.target.value);
                    if (fieldErrors.version) {
                      setFieldErrors((prev) => ({ ...prev, version: undefined }));
                    }
                  }}
                  className="w-full text-sm font-mono"
                />
              )}
            </Field>

            <Field
              label="运行环境"
              labelSuffix={
                <FieldHelp {...AGENT_VERSION_FIELD_HELPS.environment} placement="bottom-right" />
              }
            >
              {({ id }) => (
                <TextInput
                  id={id}
                  type="text"
                  placeholder="e.g. production / staging"
                  value={environment}
                  onChange={(e) => { setEnvironment(e.target.value); setCredentialId(""); }}
                  className="w-full text-sm"
                />
              )}
            </Field>
          </div>

          <Field
            label="远程调用端点 (HTTP POST)"
            required
            labelSuffix={<FieldHelp {...AGENT_VERSION_FIELD_HELPS.endpoint} />}
            error={fieldErrors.endpoint}
          >
            {({ id, ...aria }) => (
              <TextInput
                {...aria}
                id={id}
                type="url"
                required
                placeholder="http://agent-host:8080/invoke"
                value={endpoint}
                onChange={(e) => {
                  setEndpoint(e.target.value);
                  if (fieldErrors.endpoint) {
                    setFieldErrors((prev) => ({ ...prev, endpoint: undefined }));
                  }
                }}
                className="w-full text-sm font-mono"
              />
            )}
          </Field>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <Field label="超时时间 (秒)">
              {({ id }) => (
                <TextInput
                  id={id}
                  type="number"
                  min={1}
                  max={600}
                  value={timeoutSeconds}
                  onChange={(e) => setTimeoutSeconds(Number(e.target.value))}
                  className="w-full font-mono"
                />
              )}
            </Field>

            <Field label="最大重试次数">
              {({ id }) => (
                <TextInput
                  id={id}
                  type="number"
                  min={0}
                  max={10}
                  value={maxRetries}
                  onChange={(e) => setMaxRetries(Number(e.target.value))}
                  className="w-full font-mono"
                />
              )}
            </Field>

            <Field label="最大并发数">
              {({ id }) => (
                <TextInput
                  id={id}
                  type="number"
                  min={1}
                  max={50}
                  value={maxConcurrency}
                  onChange={(e) => setMaxConcurrency(Number(e.target.value))}
                  className="w-full font-mono"
                />
              )}
            </Field>

            <Field label="每分钟限流 (RPM)">
              {({ id }) => (
                <TextInput
                  id={id}
                  type="number"
                  min={1}
                  max={10000}
                  value={rateLimitPerMinute}
                  onChange={(e) => setRateLimitPerMinute(Number(e.target.value))}
                  className="w-full font-mono"
                />
              )}
            </Field>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <Field
              label="选择凭据 (Credential)"
              labelSuffix={<FieldHelp {...AGENT_VERSION_FIELD_HELPS.credentialRef} />}
              hint="先在 Credentials 创建凭据；仅列出当前环境中启用的凭据"
            >
              {({ id, ...aria }) => (
                <SelectInput
                  {...aria}
                  id={id}
                  value={credentialId}
                  onChange={(e) => { setCredentialId(e.target.value); setCredentialRef(""); }}
                  className="w-full font-mono"
                >
                  <option value="">无需鉴权 / 使用历史引用</option>
                  {credentials.data?.filter(credential => credential.enabled && credential.environment === (environment.trim().toLowerCase() || "production")).map(credential => <option key={credential.id} value={credential.id}>{credential.name} · {credential.provider}</option>)}
                </SelectInput>
              )}
            </Field>

            <Field
              label="产物标识 (ArtifactRef)"
              labelSuffix={
                <FieldHelp {...AGENT_VERSION_FIELD_HELPS.artifactRef} placement="bottom-right" />
              }
            >
              {({ id }) => (
                <TextInput
                  id={id}
                  type="text"
                  placeholder="git commit SHA 或 docker image digest"
                  value={artifactRef}
                  onChange={(e) => setArtifactRef(e.target.value)}
                  className="w-full font-mono"
                />
              )}
            </Field>
          </div>

          {credentials.isError && <p role="alert" className="text-sm text-fail">凭据列表加载失败，请关闭并重试，或前往 Credentials 检查服务状态。</p>}
          <details className="text-sm text-foreground-secondary">
            <summary>开发 / 历史引用迁移</summary>
            <Field label="历史凭据引用 (SecretRef)" hint="生产建议创建 Credential 后选择其名称。env:// 仅用于明确开启的开发模式。">
              {({ id }) => <TextInput id={id} disabled={Boolean(credentialId)} placeholder="env://DEMO_AUTH_TOKEN" value={credentialRef} onChange={e => setCredentialRef(e.target.value)} />}
            </Field>
          </details>

          <Field
            label="请求映射关系 (Request Mapping JSON)"
            labelSuffix={
              <FieldHelp {...AGENT_VERSION_FIELD_HELPS.requestMapping} placement="top-left" />
            }
            hint={`点路径映射关系，例如：${'{"query": "input.user_message"}'}`}
          >
            {({ id, ...aria }) => (
              <TextArea
                {...aria}
                id={id}
                rows={3}
                value={requestMappingStr}
                onChange={(e) => setRequestMappingStr(e.target.value)}
                className="w-full text-xs font-mono"
              />
            )}
          </Field>

          <div className="flex items-center gap-2 pt-1">
            <input
              type="checkbox"
              id="is_idempotent"
              checked={isIdempotent}
              onChange={(e) => setIsIdempotent(e.target.checked)}
              className="rounded border-border-strong text-primary focus:ring-focus"
            />
            <label htmlFor="is_idempotent" className="text-xs text-foreground-secondary select-none">
              该端点为幂等调用（发生 Read Timeout 时允许根据策略重试）
            </label>
          </div>

      </form>
    </Modal>
  );
};
