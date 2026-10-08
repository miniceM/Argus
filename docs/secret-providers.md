# Agent 凭据接入

## 默认：Argus Managed Secret

Credentials 是一等资源：稳定 ID、名称、类型（目前支持 `bearer_token`）、环境、Provider、启用状态与当前修订。
Console 的 Credentials 页面支持创建、查看使用关系、轮换、停用与删除；AgentVersion
按名称选择凭据，仅保存 `credential_id`。Token 写入一次，所有 API 响应只返回元数据。
默认 Managed Provider 无需 Vault 或 Kubernetes，可用于 Compose 部署。

Secret 用 AES-256-GCM 加密，随机 nonce，认证数据绑定 Credential ID、修订和主密钥 ID。
数据库只保存密文；主密钥来自数据库及备份目录之外的受限文件，不能与数据库备份打包。
Runner 失陷仍能读取主密钥；此方案防护数据库/备份泄露，不等同于外部 Vault 的隔离边界。

在宿主机安全目录生成文件（示例位置请按部署调整）：

```bash
umask 077
mkdir -p "$HOME/.config/argus-secrets"
python - <<'PY'
import base64, json, os, secrets
from pathlib import Path
root = Path.home() / '.config/argus-secrets'
(root / 'keyring.json').write_text(json.dumps({
    'active_key_id': 'key-1', 'keys': {'key-1': base64.b64encode(os.urandom(32)).decode()}
}))
(root / 'admin-token').write_text(secrets.token_urlsafe(32))
PY
chmod 400 "$HOME/.config/argus-secrets/"*
export ARGUS_MANAGED_SECRET_KEY_SOURCE="$HOME/.config/argus-secrets/keyring.json"
export ARGUS_CREDENTIAL_ADMIN_TOKEN_SOURCE="$HOME/.config/argus-secrets/admin-token"
docker compose --env-file .env.poc -f docker-compose.yml -f deploy/credentials.compose.yml up -d
```

文件需保持仅所有者可读；Compose 将文件挂载到 `/run/secrets/`，Runner 使用
`ARGUS_MANAGED_SECRET_KEY_FILE` 和 `ARGUS_CREDENTIAL_ADMIN_TOKEN_FILE` 读取。部署使用不同
UID 时，应设置文件所有者让 Runner 能读取，不能放宽为组或全局可读。
在 Console 创建名称、环境与 Token，管理授权 Token 由部署管理员安全交付。
创建 AgentVersion 时选同一环境的 Credential，即可发起正式评测；无鉴权 Agent 可不选。

写操作 `POST /api/v1/credentials`、`/{id}/rotate`、`/{id}/disable`、`/rewrap` 与
`DELETE /{id}` 需独立 Bearer 管理授权。未配置授权/主密钥则拒绝写入；错误响应不回显输入。
创建、轮换、停用、删除和重包裹留存不含 Secret 的审计动作、修订和管理 Token 指纹。
该指纹不代表具体用户身份。生产部署仍需网关鉴权和 HTTPS 保护整个 Console/API；本 PR
没有实现 SSO、租户 RBAC 或细粒度读取授权，这些继续由治理 Issue #6 承接。
浏览器只在当前操作表单保留 Token，成功或关闭后清空，不写 localStorage。

## 轮换、停用与主密钥恢复

- Secret 轮换：`POST /api/v1/credentials/{id}/rotate`，只提交新 `secret`。
  ID 不变，修订递增；不可变 AgentVersion/Frozen Manifest 不改写。
  每个新 Worker Attempt 发送前读取当前修订，将非秘密 `credential_id`、
  `credential_version`、`credential_provider` 写入 Attempt。已经准备好的调用使用其解析的修订。
- 停用：先查看 `GET /{id}/usage`。请求需 `confirm_name`；有版本引用时还需 `force: true`。
  停用后，未来解析明确失败；不能撤销已发送的请求。删除须名称确认，任何历史版本引用
  都会返回 409，数据库外键也阻止并发删除。
- 主密钥轮换：暂停凭据写入和 Worker 派发；在 Keyring 加入新的 32 字节密钥，切换
  `active_key_id`，保留旧密钥。用管理授权调用 `POST /api/v1/credentials/rewrap`，将所有旧修订
  重包裹到新密钥，业务 Secret 修订不变。任一密文不可读则整笔回滚。核验数据库全部密文的
  `key_id` 后恢复服务。旧备份恢复仍需原密钥，按备份保留周期安全保存旧 Keyring。
  移除旧密钥前也需确认没有旧进程/在途写入。文件更新应原子替换并保留受限权限。
- 密钥丢失：只能从独立安全备份恢复；数据库备份本身不能解密。没有明文回退或匿名调用。

## 可选企业 Provider：Vault KV v2 + Kubernetes 身份

仅管理员显式设置 `ARGUS_VAULT_ENABLED=true` 后可创建 Vault Credential，内部映射例如
`vault://secret/agents/banking#token`。普通 Agent 用户依旧选 Credential 名称/ID，API 不返回
内部映射。Vault 的实际 KV 修订记录到 Attempt；资源元数据 `version` 是 Argus 映射修订。
Vault Secret 内容由 Vault 管理员轮换，Argus 的 `/rotate` 只支持 Managed Secret。

Worker 的非秘密配置：

```text
ARGUS_VAULT_ENABLED=true
ARGUS_VAULT_ADDR=https://vault.internal.example
ARGUS_VAULT_ROLE=argus-worker
ARGUS_VAULT_KV_MOUNT=secret
ARGUS_VAULT_AUTH_MOUNT=kubernetes
ARGUS_VAULT_JWT_PATH=/var/run/secrets/kubernetes.io/serviceaccount/token
```

Vault Enterprise 可设置 `ARGUS_VAULT_NAMESPACE`。地址必须为有效 HTTPS origin/端口。
通过 Compose override 将上述变量传入 `eval-runner.environment` 并挂载 JWT；Kubernetes 中使用
受限 ServiceAccount 的 projected JWT，限制 Namespace/role/audience、KV 路径只读，使用短期 Token。
Worker 每次解析登录并读取，无长期 Vault Token、Secret 缓存或自动重试。并发量受 Worker 派发限制；
Vault 容量规划需计入每 Attempt 的登录与读取。登录/读取共用 5 秒预算，运行时额外施加
5 秒异步截止时间；超时线程可能仍清理连接，但不会因此继续发送 Agent 请求。

私有 CA：HTTPX 默认读取 certifi，单独更新系统 CA 不保证生效。将系统信任根与私有 CA 合并
为 PEM bundle，以只读方式挂载，例如 `/run/certs/vault-ca-bundle.pem`，并在 Runner 的
Compose override/Kubernetes 环境设置 `SSL_CERT_FILE=/run/certs/vault-ca-bundle.pem`。
必须保留 TLS 验证，不能使用 `verify=False`。本 PR 测试 MockTransport 的契约、失败和截止预算，
没有声称已验证真实 Vault/Kubernetes 的策略、负载或证书轮换；上线前由企业部署验收。

## 历史引用迁移与开发 PoC

已有 AgentVersion 的 `credential_ref` 保留兼容，不改写历史摘要或 Manifest。
迁移步骤：创建同环境 Credential → 创建新的 AgentVersion，填 `credential_id` 并清空
`credential_ref` → 用新版本发起评测。二者互斥。历史 Vault 引用仍要求原 Worker 身份配置。
历史 `env://` 在默认生产模式拒绝；已经冻结的生产 env 引用运行时也失败，需显式迁移。

开发可在 `.env.poc` 设置：

```text
ARGUS_SECRET_MODE=development
ARGUS_ALLOWED_CREDENTIAL_ENVS=DEMO_AUTH_TOKEN
DEMO_AUTH_TOKEN=<本地演示 Token>
```

根 Compose 显式把这三个变量传入 Runner，`env://DEMO_AUTH_TOKEN` 因此可解析。
其他白名单变量还需 Compose override 显式注入对应值，不能只改白名单。
这是 Runner 进程的变量，宿主管理员可读；轮换需要更新容器配置，不用于生产隔离承诺。
