import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "../../api/schema";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";
import { Badge } from "../../components/Badge";
import { Button, Field, Panel, SelectInput, TextInput } from "../../components/ui/Primitives";
import { Modal } from "../../components/ui/Overlay";

export function AgentLangfuseConnection({ agent }: { agent: components["schemas"]["AgentResponse"] }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [baseUrl, setBaseUrl] = useState("");
  const [projectId, setProjectId] = useState("");
  const [credentialId, setCredentialId] = useState("");
  const [authorization, setAuthorization] = useState("");
  const [error, setError] = useState<string | null>(null);
  const connection = agent.langfuse_connection;
  const credentials = useQuery({ queryKey: ["credentials"], enabled: open, queryFn: async () => {
    const result = await api.GET("/api/v1/credentials");
    if (result.error || !result.data) throw new Error("无法加载凭据");
    return result.data;
  }});
  const close = () => { setOpen(false); setAuthorization(""); setError(null); };
  const edit = () => {
    setEnabled(Boolean(connection)); setBaseUrl(connection?.base_url || "");
    setProjectId(connection?.project_id || ""); setCredentialId(connection?.credential_id || "");
    setAuthorization(""); setError(null); setOpen(true);
  };
  const mutation = useMutation({ mutationFn: async () => {
    if (!authorization.trim()) throw new Error("配置或清除项目连接需要管理授权 Token。");
    if (enabled && (!baseUrl.trim() || !projectId.trim() || !credentialId)) throw new Error("请填写项目地址、Project ID 并选择凭据。");
    const result = await api.PUT("/api/v1/agents/langfuse-connection", {
      params: { query: { id: agent.id } }, headers: { Authorization: `Bearer ${authorization}` },
      body: { langfuse_connection: enabled ? { base_url: baseUrl.trim(), project_id: projectId.trim(), credential_id: credentialId } : null },
    });
    if (result.error) throw result.error;
  }, onSuccess: () => {
    queryClient.invalidateQueries({ queryKey: queryKeys.agents.all });
    queryClient.invalidateQueries({ queryKey: ["credentials"] });
    close();
  }, onError: err => setError(formatApiError(err)) });

  return <>
    <Panel className="space-y-3 p-5 text-sm" aria-label="Langfuse 项目连接">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3 className="font-semibold">Langfuse 项目连接</h3>
        <Button variant="secondary" onClick={edit}>{connection ? "编辑 Langfuse 项目" : "配置 Langfuse 项目"}</Button>
      </div>
      <p className="text-xs text-muted-foreground">此 Agent 全部版本共用同一个项目和一套密钥，无需逐版本配置。每次 Launch 冻结实际连接，后续修改不改变历史记录。</p>
      <Badge tone="neutral">{connection ? "BOUND" : "GLOBAL_ENV"}</Badge>
      {connection ? <div className="space-y-1 break-all">
        <p className="font-mono">{connection.base_url}</p><p>Project：{connection.project_id}</p>
        <p>凭据引用：<span className="font-mono">{connection.credential_id}</span></p>
      </div> : <p className="text-muted-foreground">当前使用部署的全局 Langfuse 配置。</p>}
    </Panel>
    <Modal open={open} onClose={close} title="配置 Agent 的 Langfuse 项目" dismissable={!mutation.isPending} footer={<>
      <Button variant="secondary" onClick={close} disabled={mutation.isPending}>取消</Button>
      <Button type="submit" form="agent-langfuse-form" disabled={mutation.isPending}>
        {mutation.isPending ? "正在保存..." : connection && !enabled ? "确认清除绑定" : "确认保存连接"}
      </Button>
    </>}>
      <form id="agent-langfuse-form" className="space-y-4 p-6" onSubmit={e => { e.preventDefault(); setError(null); mutation.mutate(); }}>
        <label className="flex items-center gap-2 text-sm font-semibold">
          <input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />使用 Agent 独立 Langfuse 项目
        </label>
        <p className="text-xs text-muted-foreground">{enabled ? "全部版本的新 Launch 使用此项目；凭据环境必须与 Launch 一致。" : "新 Launch 使用全局配置。已有 Launch 继续使用各自冻结的连接。"}</p>
        {enabled && <>
          <Field label="Langfuse Base URL" required>{({ id }) => <TextInput id={id} required type="url" placeholder="https://langfuse.example.com" value={baseUrl} onChange={e => setBaseUrl(e.target.value)} />}</Field>
          <Field label="Langfuse Project ID" required>{({ id }) => <TextInput id={id} required value={projectId} onChange={e => setProjectId(e.target.value)} />}</Field>
          <Field label="Langfuse 凭据" required hint="选择 Basic Auth 凭据，username 为 pk，password 为 sk；origin 需授权此 Base URL。">
            {({ id }) => <SelectInput id={id} required value={credentialId} onChange={e => setCredentialId(e.target.value)}>
              <option value="">选择项目的 Basic Auth 凭据</option>
              {credentials.data?.filter(c => c.enabled && c.type === "basic_auth").map(c => <option key={c.id} value={c.id}>{c.name} · {c.environment} · {c.id.slice(-8)}</option>)}
            </SelectInput>}
          </Field>
        </>}
        <Field label="项目连接管理授权 Token" required hint="仅用于本次配置，不保存到 Agent 或浏览器。">
          {({ id }) => <TextInput id={id} type="password" required autoComplete="off" value={authorization} onChange={e => setAuthorization(e.target.value)} />}
        </Field>
        {credentials.isError && <p role="alert" className="text-sm text-fail">凭据列表加载失败，请关闭后重试。</p>}
        {error && <p role="alert" className="text-sm text-fail">{error}</p>}
      </form>
    </Modal>
  </>;
}
