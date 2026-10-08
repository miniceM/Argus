import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { components } from "../../api/schema";
import { Button, Field, SelectInput, TextInput } from "../../components/ui/Primitives";
import { Modal } from "../../components/ui/Overlay";

type Credential = components["schemas"]["CredentialResponse"];
type Operation = "create" | "rotate" | "disable" | "delete";
const credentialLabel = (credential: Credential) => `${credential.name} (${credential.id.slice(-8)})`;
const titles = { create: "创建凭据", rotate: "轮换凭据", disable: "停用凭据", delete: "删除凭据" };

export function CredentialsPage() {
  const queryClient = useQueryClient();
  const list = useQuery({ queryKey: ["credentials"], queryFn: async () => {
    const result = await api.GET("/api/v1/credentials");
    if (result.error || !result.data) throw new Error("无法加载凭据");
    return result.data;
  }});
  const [operation, setOperation] = useState<Operation | null>(null);
  const [selected, setSelected] = useState<Credential | null>(null);
  const [name, setName] = useState("");
  const [environment, setEnvironment] = useState("production");
  const [provider, setProvider] = useState<"managed" | "vault">("managed");
  const [reference, setReference] = useState("");
  const [secret, setSecret] = useState("");
  const [authorization, setAuthorization] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [force, setForce] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [usageTarget, setUsageTarget] = useState<Credential | null>(null);
  const [usage, setUsage] = useState<components["schemas"]["CredentialUsage"] | null>(null);
  const close = () => {
    setSecret(""); setAuthorization(""); setReference(""); setOperation(null); setError(null);
  };
  const open = (action: Operation, credential: Credential | null = null) => {
    setSelected(credential); setOperation(action); setName(""); setConfirmation("");
    setSecret(""); setAuthorization(""); setReference(""); setForce(false); setProvider("managed"); setError(null);
  };
  const showUsage = async (credential: Credential) => {
    const result = await api.GET("/api/v1/credentials/{credential_id}/usage", { params: { path: { credential_id: credential.id } } });
    if (result.error || !result.data) { setError("无法加载使用关系，请重试。"); return; }
    setUsageTarget(credential); setUsage(result.data);
  };
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!operation) return;
    setPending(true); setError(null);
    const headers = { Authorization: `Bearer ${authorization}` };
    try {
      let response;
      if (operation === "create") {
        response = await api.POST("/api/v1/credentials", { headers, body: {
          name, environment, provider, type: "bearer_token",
          ...(provider === "managed" ? { secret } : { provider_ref: reference }),
        }});
      } else if (selected) {
        const params = { path: { credential_id: selected.id } };
        if (operation === "rotate") response = await api.POST("/api/v1/credentials/{credential_id}/rotate", { headers, params, body: { secret } });
        if (operation === "disable") response = await api.POST("/api/v1/credentials/{credential_id}/disable", { headers, params, body: { confirm_name: confirmation, force } });
        if (operation === "delete") response = await api.DELETE("/api/v1/credentials/{credential_id}", { headers, params: { ...params, query: { confirm_name: confirmation } } });
      }
      if (!response || response.error) {
        // 不渲染服务端原始请求内容，避免代理错误回显 Secret。
        setError(response?.response.status === 409 ? "凭据仍被版本引用或已被并发修改。请查看使用关系；停用需明确确认，删除被引用凭据不被允许。" : "操作失败，请检查管理授权、主密钥配置和输入。");
        return;
      }
      close(); setUsage(null);
      await queryClient.invalidateQueries({ queryKey: ["credentials"] });
    } catch { setError("服务不可用，请稍后重试。"); }
    finally { setPending(false); }
  };
  return <div className="space-y-5">
    <div className="flex items-start justify-between gap-4">
      <div><h1 className="text-xl font-semibold text-foreground">Credentials</h1><p className="mt-1 text-sm text-foreground-secondary">创建凭据后，在 Agent 版本中选择名称。轮换保持同一 ID，新调用读取当前修订。</p></div>
      <Button onClick={() => open("create")}>创建凭据</Button>
    </div>
    {error && !operation && <p role="alert" className="text-sm text-fail">{error}</p>}
    {list.isPending && <p role="status">正在加载凭据…</p>}
    {list.isError && <p role="alert" className="text-fail">无法加载凭据。<Button variant="secondary" onClick={() => list.refetch()}>重试</Button></p>}
    {list.data?.length === 0 && <p className="rounded-lg border border-border bg-surface p-6 text-foreground-secondary">尚无凭据。默认使用 Argus 加密存储，无需部署 Vault。</p>}
    {list.data?.map(credential => <article key={credential.id} className="rounded-lg border border-border bg-surface p-4 space-y-3">
      <div><h2 className="font-semibold">{credential.name}</h2><p className="text-sm text-foreground-secondary">Bearer Token · {credential.environment} · {credential.provider} · {credential.enabled ? "已启用" : "已停用"} · 修订 {credential.version}</p><p className="text-xs font-mono text-muted-foreground">{credential.id}</p></div>
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" aria-label={`查看使用关系 ${credentialLabel(credential)}`} onClick={() => showUsage(credential)}>查看使用关系</Button>
        {credential.provider === "managed" && credential.enabled && <Button variant="secondary" aria-label={`轮换 ${credentialLabel(credential)}`} onClick={() => open("rotate", credential)}>轮换</Button>}
        {credential.enabled && <Button variant="secondary" aria-label={`停用 ${credentialLabel(credential)}`} onClick={() => open("disable", credential)}>停用</Button>}
        <Button variant="danger" aria-label={`删除 ${credentialLabel(credential)}`} onClick={() => open("delete", credential)}>删除</Button>
      </div>
    </article>)}
    <Modal open={usage !== null} onClose={() => setUsage(null)} title={usageTarget ? `凭据使用关系 · ${credentialLabel(usageTarget)}` : "凭据使用关系"}>
      <div className="p-6 space-y-2"><p className="text-sm text-foreground-secondary">不可变历史版本也会保护凭据不被删除。</p>
        {usage?.versions.length === 0 && <p>暂无版本引用。</p>}
        {usage?.versions.map(version => <p key={`${version.agent_id}:${version.version}`}>{version.agent_id} / {version.version} · {version.is_active ? "当前版本" : "历史版本"}</p>)}
      </div>
    </Modal>
    <Modal open={operation !== null} onClose={close} title={operation ? `${titles[operation]}${selected ? ` · ${credentialLabel(selected)}` : ""}` : "凭据"} dismissable={!pending} footer={<>
      <Button variant="secondary" onClick={close} disabled={pending}>取消</Button>
      <Button type="submit" form="credential-form" disabled={pending} variant={operation === "delete" || operation === "disable" ? "danger" : "primary"}>{pending ? "提交中…" : "确认"}</Button>
    </>}>
      <form id="credential-form" aria-label={operation === "create" ? "创建凭据表单" : "管理凭据表单"} onSubmit={submit} className="p-6 space-y-4">
        {error && <p role="alert" className="text-sm text-fail">{error}</p>}
        {operation === "create" && <>
          <Field label="凭据名称">{({ id }) => <TextInput id={id} required maxLength={128} value={name} onChange={e => setName(e.target.value)} />}</Field>
          <Field label="环境">{({ id }) => <TextInput id={id} required value={environment} onChange={e => setEnvironment(e.target.value)} />}</Field>
          <Field label="存储方式">{({ id }) => <SelectInput id={id} value={provider} onChange={e => { setProvider(e.target.value as "managed" | "vault"); setSecret(""); setReference(""); }}><option value="managed">Argus 加密存储（默认）</option><option value="vault">Vault（需管理员启用）</option></SelectInput>}</Field>
          {provider === "vault" && <Field label="Vault 内部映射" hint="仅由部署管理员登记。Agent 用户选择凭据名称，无需填写 Vault 路径。">{({ id }) => <TextInput id={id} required value={reference} onChange={e => setReference(e.target.value)} />}</Field>}
        </>}
        {(operation === "rotate" || (operation === "create" && provider === "managed")) && <Field label="Agent Token（仅输入一次）" hint="保存后无法查看。旧修订保留为密文。">{({ id }) => <TextInput id={id} type="password" autoComplete="off" required value={secret} onChange={e => setSecret(e.target.value)} />}</Field>}
        {(operation === "delete" || operation === "disable") && <>
          <p className="text-sm">{operation === "delete" ? "删除仅允许未被任何版本引用的凭据。" : "停用后，引用此凭据的后续调用将在发送请求前失败。"}</p>
          <Field label={`输入凭据名称确认：${selected?.name}`}>{({ id }) => <TextInput id={id} required value={confirmation} onChange={e => setConfirmation(e.target.value)} />}</Field>
          {operation === "disable" && <label className="flex gap-2 text-sm"><input type="checkbox" checked={force} onChange={e => setForce(e.target.checked)} />明确确认停用已被 Agent 版本引用的凭据</label>}
        </>}
        <Field label="管理授权 Token" hint="由部署管理员提供，仅用于本次写操作，不存入浏览器。">{({ id }) => <TextInput id={id} type="password" autoComplete="off" required value={authorization} onChange={e => setAuthorization(e.target.value)} />}</Field>
      </form>
    </Modal>
  </div>;
}
