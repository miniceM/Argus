# 受认证保护的同步 HTTP Agent 接入

适用：已有服务通过 HTTP POST 返回最终 JSON，业务服务不安装 Evaluation SDK。
本页描述工作区实现，部署、官方 DataAgent 和 Cloud 实测分别验收，不因配置成功就视为全链路通过。
API 字段、默认值与约束以 [OpenAPI](openapi.json) 为准；密钥挂载见 [凭据手册](credentials-user-manual.md)。

## 1. 按目标接口选择，不按 Token 的名字猜

| 目标要求 | Credentials 类型 | AgentVersion 认证方式 | 额外配置 |
|---|---|---|---|
| 不需要认证 | 无主凭据 | No Auth | 可加普通业务 Header |
| `Authorization: Bearer …` | Bearer Token | Bearer | 无 |
| `X-API-Key: …` | API Key | API Key | Header 名 `X-API-Key`，前缀留空 |
| 原始 `Authorization: …` | API Key | API Key | Header 名 `Authorization`，前缀留空或填写公开固定前缀（如 `Token `，含末尾空格） |
| HTTP Basic | Basic Auth | Basic | 在 Credentials 一次性输入用户名/密码，不填进版本 |
| 多个额外业务 Header | 普通值 / 敏感 Header 值凭据 | 与主认证组合 | 如 `X-Tenant`、`X-App-Id`；秘密只能选择引用 |

“自动（历史 Bearer / 无认证）”保留旧行为：有主凭据即 Bearer，否则匿名。新建配置建议明确选认证方式。
API Key 和敏感 Header 的 Secret 当前要求单个非空 ASCII Token、无空白；公开前缀单独配置。
Basic 用户名不可含冒号，用户名/密码可含空格，密码可含冒号，控制字符禁止。
不支持 Query API Key、自动 OAuth 登录/刷新、Cookie 会话、签名或请求前脚本。

## 2. Console 操作

1. **Credentials → 创建凭据**：选择类型、名称、环境、允许的目标 Origin 和秘密，填写本次管理授权。
   Origin 不含路径，如 `https://agent.example:8443`；新 API Key、Basic、敏感 Header 类型必须填写。
   列表不会显示秘密。Basic 轮换重新输入用户名和密码，不回填原值。
2. **Agents → 创建新版本**：先填写端点/环境，再选认证方式及同类型、同环境凭据。
   API Key 指定 Header 名/公开前缀。旧 `credential_ref` 仅用于 Bearer 历史迁移。
3. **自定义 HTTP Headers**：逐条选普通值或敏感凭据引用；后者使用 `header_value` 类型。
   主认证或任意 Header 引用了凭据，都要填写**凭据绑定管理授权 Token**，不是把管理 Token 当成 Agent 凭据。
4. 配置请求 JSON 映射和可选**回答字段路径**；生成新版本。认证/Headers/输出提取都冻结进该版本和后续 Launch。
   版本详情展示配置与引用；Attempt 抽屉展示每个实际 Header 的凭据 ID / 修订，不展示值。
5. 使用同环境的新版本发起评测。不要只看 HTTP 200：还要检查回答提取、Evaluator、质量结论与独立的 Langfuse 同步状态。

## 3. AgentScope Java DataAgent v2.0.4

锁定官方源码 `0b6743b263ba8e39eb417e35048026f1e0196548`，不要把当前网站的 Service Session/Turn 文档套到旧版 Demo。

- 服务方部署原生 DataAgent 并配置实际模型、`DATAAGENT_JWT_SECRET`，准备专用账号及目标 Agent 的 **RUN** 权限。
- 在受保护环境调用原生登录取得访问 JWT；Argus 保存访问 Token，不保存用户名登录密码或 JWT 签名密钥，不自动续期。
- 端点选择 `/api/agents/{agentId}/chat/send`，认证选 Bearer。
- 请求映射将数据集消息字段映射到 `message`。例如数据集用 `user_message` 时填写 `{"message":"input.user_message"}`。
- **会话隔离是服务部署前置，不能只看返回的 sessionKey 不同**。该版本 `/send` 缺省会生成 UUID，
  但其下游默认 `channels.chatui.dmScope=MAIN` 忽略 conversationId，实际不同用例会共用路由与历史。
  服务方须在有效的 `agentscope.json` 中配置 `channels.chatui.dmScope=PER_ACCOUNT_CHANNEL_PEER`，确认无 binding 的 sessionScope 覆盖，
  并验证最终 Session/RuntimeContext，而不只是请求 DTO。完成此前置后，单轮评测可省略 sessionKey，使用原生每请求 UUID；
  或映射数据集内为本次执行准备的独立 sessionKey，禁止跨用例/新执行复用固定值。正式多轮场景另行设计。
  sessionKey 不是业务幂等键；建议 `max_retries=0`，保持非幂等。不能用 `/new`、`/reset` 的短路返回冒充模型回答验收。
  本次原生推理运行未配置该 scope，会话独立性不合格；配置后的官方路由离线探针通过，但没有再次运行付费纵向验收（见验收报告）。
- 回答字段路径填 `reply`。采纳后的 `answer` 用于评分，原 `reply`、sessionKey 仍在结果中；缺失/类型不符判调用失败。
- 必测：正常回答、无效/过期 JWT（401）、资源可见但无 RUN（403）、资源不可见（404）。这三类授权结果依赖真实 Demo 的账号/权限，Java 夹具不能代替其证明。

v2.0.4 Chat Completions Starter 的入站认证由应用/网关添加，不因模型 `.apiKey()` 存在就推断其自带入站认证。
Service Beta1 的 Session/Turn 是另一个异步协议，不把 202 当最终回答；本期不适配。

## 4. 安全边界与失败行为

- Header 名大小写不敏感；重复、认证冲突、非法字符、CR/LF、超长值拒绝。最多 32 个自定义 Header。
  Host/Content-Length/Content-Type/hop-by-hop、W3C、X-Eval/X-Argus、幂等头由平台管理，不支持覆盖 JSON Content-Type。
- 普通值和固定前缀是公开配置，会进入版本/Manifest；不能填秘密。已知敏感 Header 名强制用引用，但平台无法自动识别任意业务字符串是否为秘密。
- 凭据的 Origin/type/environment 在绑定与运行时检查；引用由数据库 FK 保留，历史版本同样阻止删除。
  修订在每个 Attempt 准备时解析，实际使用身份写入审计；轮换不改冻结配置。已准备/已发送的请求不能撤回，停用阻断后续解析。
- 不跟随重定向（包括传入的 HTTP client 默认允许重定向时），HTTPS 默认校验证书。普通 HTTP 明文传输不安全，除隔离本机验收外应使用 HTTPS。
- 401/403/404 不自动重试。202、空响应、重定向以及输出提取失败非重试失败，不进入评分成功路径。
- 响应体/Headers 回显已解析秘密（包括 Basic 编码、解码组成部分及常见编码形式）时，标 `SENSITIVE_RESPONSE` 并丢弃内容，不进入 Trace/Checkpoint/错误详情。
  这不是通用数据防泄漏系统，不能识别任意变换或与本次凭据无关的秘密；远端仍须遵守数据最小化。

### 正式部署前置，而非本期已交付保证

Origin 约束只限制协议/主机/有效端口，**不锁定 DNS/IP，不防 DNS rebinding，不等于完整 SSRF 防御**；同 Origin 的路径也不是细粒度权限边界。
部署方必须配置 Runner 的受控出站网络/代理与 DNS：只允许获准 Agent 地址，阻断云元数据和未获准内网/公网目标，避免不可信代理改写路由。
老 Bearer 没有 Origin 限制的配置仅兼容保留，应新建受限凭据迁移。Console/API 仍需企业网关身份授权与 HTTPS。
未配置并实测上述网络边界前，不能宣称生产凭据桥接安全或内网验收已完成。

## 5. 本地可复验测试

仓库根目录（需 Python 开发环境；Java 验收另需 JDK 17+）：

```bash
.venv/bin/python -m pytest -q tests/test_http_agent_contract.py tests/test_http_agent_java_acceptance.py
pnpm --dir services/console exec vitest run src/components/__tests__/CreateVersionDialog.test.tsx src/components/__tests__/CredentialsPage.test.tsx src/components/__tests__/AttemptDrawer.test.tsx
pnpm --dir services/console build
ARGUS_E2E_API_PORT=18180 ARGUS_E2E_CONSOLE_PORT=18183 pnpm --dir services/console exec playwright test e2e/http-agent-auth.spec.ts e2e/credentials.spec.ts
```

`tests/fixtures/http_auth_java/AuthAgent.java` 是 loopback 专用真实 Java HTTP 测试应用，无评测 SDK、无模型；其认证是测试应用新增逻辑，不是 AgentScope 框架内置能力。
Python 验收经正常 Registry → Launch/冻结 Dataset → Worker → Java/TCP → Evaluator → Checkpoint/Snapshot，覆盖所有首期认证和多个 Header、轮换/停用与拒绝。
Console 验收使用实际 API/DB，但不把这份证据等同于同一次 Java/Worker 或 Langfuse Cloud 纵向验收。官方 Demo、Cloud、用户内网必须单列结果。

实际分层结果、付费调用预算和未通过项目见 [Issue #249 验收报告](validation/issue249-http-auth.md)。
