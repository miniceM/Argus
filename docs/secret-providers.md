# Agent 凭据接入

AgentVersion 只保存 `credential_ref`，Worker 在发起业务 HTTP 请求前读取凭据。
引用存在但 Secret 缺失、Vault 拒绝或 Provider 不可用时，执行明确失败，不退化为匿名调用。
Secret 不返回 Registry API，不写入 Attempt 错误或 Langfuse Header。

## 生产：Vault KV v2 与 Kubernetes 身份

例如 `vault://secret/agents/banking#token`：`secret` 是已配置 KV v2 mount，
`agents/banking` 是 Secret 路径，`token` 是字段名。引用不能包含凭据、查询参数或路径穿越。

Worker 配置以下非秘密变量：

```text
ARGUS_VAULT_ADDR=https://vault.internal.example
ARGUS_VAULT_ROLE=argus-worker
ARGUS_VAULT_KV_MOUNT=secret
ARGUS_VAULT_AUTH_MOUNT=kubernetes
ARGUS_VAULT_JWT_PATH=/var/run/secrets/kubernetes.io/serviceaccount/token
```

Vault Enterprise 可额外配置 `ARGUS_VAULT_NAMESPACE`。Vault 地址必须为 HTTPS origin；
私有 CA 应加入容器信任链，不能关闭证书验证。

在 Vault 配置 Kubernetes Auth，限制 role 对应的 ServiceAccount、Namespace 与 JWT audience，
只授予所需 KV 路径的 `read`，使用短期、最小权限 Token。Worker 用挂载的 ServiceAccount JWT
登录，再读取 Secret；不需要在平台环境变量配置长期 Agent Token 或 Vault Token。
每次解析重新读取，Secret 轮换后下次调用生效。超时为 5 秒，不跟随重定向、不自动重试。
Vault 自身记录登录与读取审计；Argus 的完整用户授权和审计仍由治理 Issue #6 承接。

## 本地开发与 PoC：env://

必须显式设置 `ARGUS_SECRET_MODE=development`（隔离的 `ARGUS_DB_MODE=test` 默认允许）。
例如配置 `ARGUS_ALLOWED_CREDENTIAL_ENVS=DEMO_AUTH_TOKEN`，再将演示 Token 注入 Runner 容器。
`env://DEMO_AUTH_TOKEN` 读取的是 **Runner 进程**的变量，不是被测 Agent 的变量。
进程或宿主管理员可能读取这些值；更改后需更新容器配置并重新启动。此模式只用于开发和演示，
不能用来宣称已有生产凭据隔离、访问审计或自动轮换。

生产模式默认拒绝 `env://`；不支持的 Provider 也明确拒绝。无需鉴权的 Agent 留空引用。
