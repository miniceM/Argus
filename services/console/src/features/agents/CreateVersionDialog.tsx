import React, { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Layers } from "lucide-react";
import type { components } from "../../api/schema";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";
import { FieldHelp } from "../../components/FieldHelp";
import { Button, Field, SelectInput, TextArea, TextInput } from "../../components/ui/Primitives";
import { Modal } from "../../components/ui/Overlay";
import { AGENT_VERSION_FIELD_HELPS } from "./helpDocs";

type AuthScheme = components["schemas"]["HTTPAuth"]["scheme"] | "legacy";
type HeaderRow = { name: string; mode: "value" | "credential"; value: string; credentialId: string; prefix: string };
type FieldMappingRow = { target: string; source: string };
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
  const [credentialAuthorization, setCredentialAuthorization] = useState("");
  const [authScheme, setAuthScheme] = useState<AuthScheme>("legacy");
  const [authHeader, setAuthHeader] = useState("X-API-Key");
  const [authPrefix, setAuthPrefix] = useState("");
  const [httpHeaders, setHttpHeaders] = useState<HeaderRow[]>([]);
  const [answerPath, setAnswerPath] = useState("");
  const [responseFields, setResponseFields] = useState<FieldMappingRow[]>([]);
  const hasCredentialRefs = Boolean(credentialId || httpHeaders.some(header => header.mode === "credential" && header.credentialId));
  useEffect(() => {
    if (!isOpen || !hasCredentialRefs) setCredentialAuthorization("");
  }, [isOpen, hasCredentialRefs]);
  const credentials = useQuery({ queryKey: ["credentials"], enabled: isOpen, queryFn: async () => {
    const result = await api.GET("/api/v1/credentials");
    if (result.error || !result.data) throw new Error("无法加载凭据");
    return result.data;
  }});
  const [artifactRef, setArtifactRef] = useState("");
  const [environment, setEnvironment] = useState("staging");
  const [requestMappingRows, setRequestMappingRows] = useState<FieldMappingRow[]>([
    { target: "query", source: "input.user_message" },
  ]);
  const [requestMappingStr, setRequestMappingStr] = useState('{"query": "input.user_message"}');
  const applyRequestRows = (nextRows: FieldMappingRow[]) => {
    setRequestMappingRows(nextRows);
    const obj: Record<string, string> = {};
    for (const row of nextRows) {
      const k = row.target.trim();
      const v = row.source.trim();
      if (k) obj[k] = v;
    }
    setRequestMappingStr(JSON.stringify(obj));
  };
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

      const frozenHeaders: components["schemas"]["HTTPHeader"][] = httpHeaders.map(header => header.mode === "value"
        ? { name: header.name.trim(), value: header.value, prefix: "" }
        : { name: header.name.trim(), credential_id: header.credentialId, prefix: header.prefix });
      const extraResponseFields: Record<string, string> = {};
      for (const row of responseFields) {
        const target = row.target.trim();
        const source = row.source.trim();
        if (target && source) extraResponseFields[target] = source;
      }
      const trimmedAnswerPath = answerPath.trim();
      const hasExtraResponseFields = Object.keys(extraResponseFields).length > 0;
      const responseMappingPayload: components["schemas"]["ResponseMapping"] | null =
        !trimmedAnswerPath && !hasExtraResponseFields
          ? null
          : {
              ...(trimmedAnswerPath ? { answer_path: trimmedAnswerPath } : {}),
              ...(hasExtraResponseFields ? { fields: extraResponseFields } : {}),
            };
      const res = await api.POST("/api/v1/agent-versions", {
        ...(hasCredentialRefs ? { headers: { Authorization: `Bearer ${credentialAuthorization}` } } : {}),
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
          http_auth: authScheme === "legacy" ? null : authScheme === "api_key" ? { scheme: "api_key", header_name: authHeader.trim(), prefix: authPrefix } : { scheme: authScheme, prefix: "" },
          http_headers: frozenHeaders,
          response_mapping: responseMappingPayload,
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
      setCredentialAuthorization("");
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
    const requiresCredential =
      authScheme === "api_key" ||
      authScheme === "basic" ||
      (authScheme === "bearer" && !credentialRef.trim());
    if (requiresCredential && !credentialId) {
      setErrorMsg("已选择该认证方式，请选择要绑定的凭据（若无需鉴权请切换为 No Auth）。");
      return;
    }
    if (hasCredentialRefs && !credentialAuthorization.trim()) {
      setErrorMsg("绑定凭据需要管理授权 Token，请由管理员确认端点可信。");
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
                  onChange={(e) => { setEnvironment(e.target.value); setCredentialId(""); setHttpHeaders([]); }}
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

          <section aria-label="身份认证与凭据" className="space-y-3 rounded-lg border border-border bg-surface-muted p-4">
            <div className="flex items-center gap-1.5">
              <h3 className="text-sm font-semibold text-foreground">身份认证与凭据</h3>
              <FieldHelp {...AGENT_VERSION_FIELD_HELPS.credentialRef} />
            </div>

            <Field label="HTTP 认证方式" hint="默认兼容旧 Bearer/无认证配置；认证方式随新版本冻结。">
              {({ id }) => <SelectInput id={id} value={authScheme} onChange={e => { setAuthScheme(e.target.value as AuthScheme); setCredentialId(""); setCredentialRef(""); setCredentialAuthorization(""); }}>
                <option value="legacy">自动（历史 Bearer / 无认证）</option>
                <option value="none">No Auth</option>
                <option value="bearer">Bearer Token</option>
                <option value="api_key">API Key（Header）</option>
                <option value="basic">Basic Auth</option>
              </SelectInput>}
            </Field>

            {authScheme === "none" ? (
              <p className="text-xs text-foreground-secondary">此端点无需身份鉴权，调用时不附带认证请求头。</p>
            ) : (
              <div className="space-y-3 pt-1">
                <Field
                  label="选择凭据 (Credential)"
                  hint="先在 Credentials 创建凭据；仅列出当前环境中启用的匹配凭据"
                >
                  {({ id, ...aria }) => (
                    <SelectInput
                      {...aria}
                      id={id}
                      value={credentialId}
                      onChange={(e) => { setCredentialId(e.target.value); setCredentialRef(""); setCredentialAuthorization(""); }}
                      className="w-full font-mono"
                    >
                      <option value="">
                        {authScheme === "legacy" ? "不绑定凭据 / 使用下方历史引用" : "请选择要绑定的凭据..."}
                      </option>
                      {credentials.data?.filter(credential => credential.enabled && credential.environment === (environment.trim().toLowerCase() || "production") &&
                        (credential.type || "bearer_token") === (authScheme === "api_key" ? "api_key" : authScheme === "basic" ? "basic_auth" : "bearer_token")).map(credential => <option key={credential.id} value={credential.id}>{credential.name} · {credential.provider} · {credential.id.slice(-8)}</option>)}
                    </SelectInput>
                  )}
                </Field>

                {authScheme === "api_key" && <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <Field label="API Key Header 名称">{({ id }) => <TextInput id={id} required value={authHeader} onChange={e => setAuthHeader(e.target.value)} />}</Field>
                  <Field label="API Key 固定前缀" hint="原始 Authorization 可留空；Token 前缀需包含尾部空格。">{({ id }) => <TextInput id={id} value={authPrefix} onChange={e => setAuthPrefix(e.target.value)} />}</Field>
                </div>}

                {["legacy", "bearer"].includes(authScheme) && (
                  <details className="text-sm text-foreground-secondary">
                    <summary>开发 / 历史引用迁移</summary>
                    <Field label="历史凭据引用 (SecretRef)" hint="生产建议创建 Credential 后选择其名称。env:// 仅用于明确开启的开发模式。">
                      {({ id }) => <TextInput id={id} disabled={Boolean(credentialId)} placeholder="env://DEMO_AUTH_TOKEN" value={credentialRef} onChange={e => setCredentialRef(e.target.value)} />}
                    </Field>
                  </details>
                )}
              </div>
            )}
          </section>

          {hasCredentialRefs && <Field label="凭据绑定管理授权 Token" hint="绑定会允许 Runner 向此端点发送凭据，需管理员确认端点可信。授权仅用于本次操作。">
            {({ id }) => <TextInput id={id} type="password" autoComplete="off" required value={credentialAuthorization} onChange={e => setCredentialAuthorization(e.target.value)} />}
          </Field>}

          {credentials.isError && <p role="alert" className="text-sm text-fail">凭据列表加载失败，请关闭并重试，或前往 Credentials 检查服务状态。</p>}

          <section aria-label="请求字段映射" className="space-y-3 rounded-lg border border-border bg-surface-muted p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-1.5">
                <h3 className="text-sm font-semibold text-foreground">请求字段映射 (Request Mapping)</h3>
                <FieldHelp {...AGENT_VERSION_FIELD_HELPS.requestMapping} placement="top-left" />
              </div>
              <div className="flex flex-wrap items-center gap-1.5">
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => applyRequestRows([{ target: "query", source: "input.user_message" }])}
                >
                  默认模板
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => applyRequestRows([{ target: "message", source: "input.user_message" }])}
                >
                  DataAgent 模板
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() =>
                    applyRequestRows([
                      { target: "messages", source: "input.messages" },
                      { target: "customer_id", source: "input.customer_id" },
                    ])
                  }
                >
                  多轮 Messages 模板
                </Button>
              </div>
            </div>
            <p className="text-xs text-muted-foreground">
              将数据集输入点路径（如 input.user_message）映射为发送给 Agent 的请求 JSON 字段。
            </p>
            {requestMappingRows.map((row, index) => {
              const updateRow = (patch: Partial<FieldMappingRow>) => {
                const next = requestMappingRows.map((r, i) => (i === index ? { ...r, ...patch } : r));
                applyRequestRows(next);
              };
              return (
                <div key={index} className="grid grid-cols-1 sm:grid-cols-2 gap-3 rounded-lg border border-border bg-surface p-3">
                  <Field label={`请求字段 ${index + 1} 目标键名`}>
                    {({ id }) => (
                      <TextInput
                        id={id}
                        placeholder="e.g. message"
                        value={row.target}
                        onChange={(e) => updateRow({ target: e.target.value })}
                        className="font-mono text-xs"
                      />
                    )}
                  </Field>
                  <div className="flex items-end gap-2">
                    <div className="flex-1">
                      <Field label={`请求字段 ${index + 1} 数据源点路径`}>
                        {({ id }) => (
                          <TextInput
                            id={id}
                            placeholder="e.g. input.user_message"
                            value={row.source}
                            onChange={(e) => updateRow({ source: e.target.value })}
                            className="font-mono text-xs"
                          />
                        )}
                      </Field>
                    </div>
                    {requestMappingRows.length > 1 && (
                      <Button
                        type="button"
                        variant="secondary"
                        onClick={() => applyRequestRows(requestMappingRows.filter((_, i) => i !== index))}
                      >
                        移除
                      </Button>
                    )}
                  </div>
                </div>
              );
            })}
            <Button
              type="button"
              variant="secondary"
              disabled={requestMappingRows.length >= 32}
              onClick={() => applyRequestRows([...requestMappingRows, { target: "", source: "" }])}
            >
              添加请求字段映射
            </Button>

            <details className="text-xs text-foreground-secondary pt-1">
              <summary className="cursor-pointer">高级：直接编辑 Request Mapping JSON</summary>
              <div className="pt-2">
                <Field
                  label="请求映射关系 (Request Mapping JSON)"
                  hint={`点路径映射关系，例如：${'{"query": "input.user_message"}'}`}
                >
                  {({ id, ...aria }) => (
                    <TextArea
                      {...aria}
                      id={id}
                      rows={3}
                      value={requestMappingStr}
                      onChange={(e) => {
                        const raw = e.target.value;
                        setRequestMappingStr(raw);
                        try {
                          const parsed = JSON.parse(raw);
                          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
                            setRequestMappingRows(
                              Object.entries(parsed).map(([target, source]) => ({
                                target,
                                source: String(source),
                              }))
                            );
                          }
                        } catch {
                          // Ignore parse error while typing in raw JSON mode
                        }
                      }}
                      className="w-full text-xs font-mono"
                    />
                  )}
                </Field>
              </div>
            </details>
          </section>

          <section className="space-y-3" aria-label="自定义 HTTP Headers">
            <h3 className="text-sm font-semibold">自定义 HTTP Headers</h3>
            <p className="text-xs text-muted-foreground">普通值不允许存放秘密；敏感值选择凭据引用。平台追踪、幂等和传输头不可覆盖。</p>
            {httpHeaders.map((header, index) => {
              const update = (patch: Partial<HeaderRow>) => setHttpHeaders(rows => rows.map((row, i) => i === index ? { ...row, ...patch } : row));
              return <div key={index} className="space-y-2 rounded-lg border border-border p-3">
                <Field label={`Header ${index + 1} 名称`}>{({ id }) => <TextInput id={id} required value={header.name} onChange={e => update({ name: e.target.value })} />}</Field>
                <Field label={`Header ${index + 1} 值来源`}>{({ id }) => <SelectInput id={id} value={header.mode} onChange={e => update({ mode: e.target.value as HeaderRow["mode"], value: "", credentialId: "" })}>
                  <option value="value">普通值（非敏感）</option><option value="credential">敏感值凭据引用</option>
                </SelectInput>}</Field>
                {header.mode === "value" ? <Field label={`Header ${index + 1} 普通值`}>{({ id }) => <TextInput id={id} required value={header.value} onChange={e => update({ value: e.target.value })} />}</Field>
                : <>
                  <Field label={`Header ${index + 1} 凭据`}>{({ id }) => <SelectInput id={id} required value={header.credentialId} onChange={e => { update({ credentialId: e.target.value }); setCredentialAuthorization(""); }}>
                    <option value="">选择敏感 Header 值凭据</option>
                    {credentials.data?.filter(c => c.enabled && c.type === "header_value" && c.environment === environment.trim().toLowerCase()).map(c => <option key={c.id} value={c.id}>{c.name} · {c.id.slice(-8)}</option>)}
                  </SelectInput>}</Field>
                  <Field label={`Header ${index + 1} 固定前缀`}>{({ id }) => <TextInput id={id} value={header.prefix} onChange={e => update({ prefix: e.target.value })} />}</Field>
                </>}
                <Button type="button" variant="secondary" onClick={() => setHttpHeaders(rows => rows.filter((_, i) => i !== index))}>移除 Header {index + 1}</Button>
              </div>;
            })}
            <Button type="button" variant="secondary" disabled={httpHeaders.length >= 32} onClick={() => setHttpHeaders(rows => [...rows, { name: "", mode: "value", value: "", credentialId: "", prefix: "" }])}>添加 HTTP Header</Button>
          </section>

          <section aria-label="应答字段提取映射" className="space-y-3 rounded-lg border border-border bg-surface-muted p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-sm font-semibold text-foreground">应答字段提取映射 (Response Mapping)</h3>
              <div className="flex flex-wrap items-center gap-1.5">
                <Button type="button" variant="secondary" onClick={() => setAnswerPath("reply")}>
                  DataAgent (reply)
                </Button>
                <Button type="button" variant="secondary" onClick={() => setAnswerPath("choices.0.message.content")}>
                  OpenAI (choices.0.message.content)
                </Button>
              </div>
            </div>
            <Field label="回答字段路径" hint="留空保留原响应；DataAgent 填 reply，Chat Completions 填 choices.0.message.content。必须是非空文本，配置随版本冻结。">
              {({ id }) => <TextInput id={id} value={answerPath} onChange={e => setAnswerPath(e.target.value)} placeholder="reply" className="font-mono text-xs" />}
            </Field>
            {responseFields.map((row, index) => {
              const updateRespRow = (patch: Partial<FieldMappingRow>) =>
                setResponseFields((rows) => rows.map((r, i) => (i === index ? { ...r, ...patch } : r)));
              return (
                <div key={index} className="grid grid-cols-1 sm:grid-cols-2 gap-3 rounded-lg border border-border bg-surface p-3">
                  <Field label={`应答字段 ${index + 1} 目标键名`}>
                    {({ id }) => (
                      <TextInput
                        id={id}
                        placeholder="e.g. intent / tool_calls"
                        value={row.target}
                        onChange={(e) => updateRespRow({ target: e.target.value })}
                        className="font-mono text-xs"
                      />
                    )}
                  </Field>
                  <div className="flex items-end gap-2">
                    <div className="flex-1">
                      <Field label={`应答字段 ${index + 1} 响应点路径`}>
                        {({ id }) => (
                          <TextInput
                            id={id}
                            placeholder="e.g. meta.intent"
                            value={row.source}
                            onChange={(e) => updateRespRow({ source: e.target.value })}
                            className="font-mono text-xs"
                          />
                        )}
                      </Field>
                    </div>
                    <Button
                      type="button"
                      variant="secondary"
                      onClick={() => setResponseFields((rows) => rows.filter((_, i) => i !== index))}
                    >
                      移除应答字段 {index + 1}
                    </Button>
                  </div>
                </div>
              );
            })}
            <Button
              type="button"
              variant="secondary"
              disabled={responseFields.length >= 32}
              onClick={() => setResponseFields((rows) => [...rows, { target: "", source: "" }])}
            >
              添加应答字段映射
            </Button>
          </section>

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
