# 按 Agent 连接 Langfuse 项目

共用 Argus 的团队可为每个 Agent 配置一个 Langfuse 项目和一套项目密钥。
该 Agent 的所有已有版本与后续版本共用此连接，创建版本时无需重复配置，
也不支持按版本覆盖项目。每次 Launch 单独冻结实际使用的连接。
Dataset 读取、Observation / DatasetRun 投影、Builtin Score 写入和 Managed Score
采纳使用同一绑定。Langfuse 仍管理 Dataset、Evaluator、Rule、Trace 和 Score；
Argus 只保存连接引用、冻结输入与采纳证据，不提供项目管理或多租户权限体系。

## 配置

1. 按[凭据部署说明](secret-providers.md)配置 Managed Secret 主密钥和管理授权。
2. 在 **Credentials** 创建同环境的 `basic_auth` 凭据：username 为项目 Public Key，
   password 为 Secret Key，allowed origins 精确填写 Langfuse 的 HTTP(S) origin。
   密钥只在此表单写入一次，Agent 与 Launch 不保存明文。
3. 在 **Agent 详情 → 配置 Langfuse 项目** 勾选“使用 Agent 独立 Langfuse 项目”，
   填写 Base URL、Project ID，选择上述凭据，并提交“项目连接管理授权 Token”。
   之后创建 v1、v2 等版本时只填写业务调用配置，无需再次填写项目或密钥。
4. 创建 Launch，检查版本环境及 `BOUND` 来源。Managed 配置中的 Project ID
   必须与绑定一致；只填该文本字段不会切换凭据。Rule 继续在 Langfuse 配置。

Base URL 只接受 origin，不接受路径、查询参数、用户信息或片段。凭据必须启用、
类型为 Basic Auth，且允许该 origin 和环境。REST / OTLP 请求不跟随重定向，
TLS 使用正常证书校验。私网部署可使用管理员明确授权的 origin；这不构成
按租户隔离的网络边界，DNS 与出口访问仍由部署方控制。

Agent 的 API 字段为 `langfuse_connection`；可在注册 Agent 时传入，或通过
`PUT /api/v1/agents/langfuse-connection?id=...` 配置。配置和清除均需管理授权。
YAML bootstrap 将此字段放在 Agent 节点，与 `versions` 平级；版本级配置会被拒绝。
完整请求与响应以
[OpenAPI](openapi.json) 为准。绑定字段仅包含 `base_url`、`project_id`、
`credential_id`。迁移 022 追加 Agent 连接及凭据引用表，保留迁移 021 和旧版本列，
不改写历史版本、摘要、Manifest 或 Snapshot。若试用旧 PR 已有版本级绑定，
须由管理员在 Agent 上明确配置；未配置时新建 Launch 失败，不静默改用全局项目。

## 冻结、轮换与退出

新 Launch 的 Manifest 记录 `langfuse_connection.source`：

| 来源 | 路由 |
|---|---|
| `BOUND` | 使用冻结的 Base URL、Project ID、Credential ID；解析失败不回落全局 |
| `GLOBAL_ENV` | 沿用已有全局环境配置与同步策略，无需修改既有单项目部署 |

已绑定 Agent 的任一版本从该项目读取并冻结 Dataset，不受全局 `ARGUS_DATASET_SOURCE` 的本地
Demo 选择影响。创建时验证凭据实际所属项目；每次 Agent Attempt 前重新解析并核验。
Managed 的 environment、Observation 身份、Score 类型、Rule / Evaluator 来源
核验仍生效。项目不匹配、缺分或来源不可信保持 UNKNOWN，不以默认值补齐。

Manifest 中 `credential_version` 是创建时实际解析的修订。Attempt 的
`credential_usage` 记录派发前实际解析的修订（`Langfuse-Authorization`），
与 Agent 入口认证分别留存。后续 Dataset / OTLP / Score / 链接操作的解析修订
写入已有 Credential 审计表的 `RESOLVE_LANGFUSE_*` 动作；解析记录不等于远端写入成功。
这些审计均不含密钥。轮换后后续解析使用新修订，已准备的调用不能被追溯撤销。

停用或不可解密时返回 `CREDENTIAL_UNAVAILABLE`；Agent 调用前失败的 Attempt
保持 PREPARED。实际凭据项目错配返回 `MANAGED_PROJECT_MISMATCH`；远端 401 / 403
分别返回 `AUTHENTICATION_FAILED` / `AUTHORIZATION_FAILED`。OTLP 失败由原 Outbox
退避重试，只改变同步状态，不把既有质量结论改成失败或通过。

修改目标项目或清除绑定在 Agent 详情完成，无需创建新 AgentVersion。更改只影响
之后新建的 Launch；已有 Launch 的执行、恢复、OTLP 和 Score 继续使用冻结连接。
清除绑定后新的 Launch 使用全局配置（旧版本级绑定的兼容限制见上文）。
凭据环境仍须与 Launch 环境一致；此配置不授予跨环境使用凭据的权限。
若读取 Dataset 期间连接被并发修改，本次创建失败，重试后使用统一的新连接。

当前及历史 Agent 项目凭据引用会保留至该 Agent 及其本地证据被清理，期间不可
删除，停用需显式强制确认；这也包括绑定后尚未创建 Launch 的凭据。Credentials
使用关系区分当前与历史项目引用。旧 Manifest、Snapshot、版本 digest 和 Gate
保持原样；历史 Manifest 缺少来源字段时仍按旧全局路径读取，不伪造冻结来源。

## 连接与恢复成本

绑定路径使用短生命周期的 REST 客户端及现有 Outbox 直发 HTTP OTLP，强制使用
`otel` 投影，不为每个项目注册 SDK 单例、TracerProvider、后台 exporter 或永久缓存。
每次 REST 操作结束关闭客户端；OTLP 复用现有 16 个远端调用槽位与持久化重试。
因此无需按项目数量积累 exporter 或实现空闲 exporter 回收。

执行成功后的 Managed 收集继续复用原队列、generation / lease、取消与迟到结果
隔离；同步入口遗漏首轮提交时由原 Reconciler 补交。只重试评测不会重调 Agent。
历史质量证据不会用当前 Langfuse latest 回填。详细使用流程见
[Managed Evaluation 指南](langfuse-managed-evaluation.md)。

## 验证与边界

2026-11-12 的执行结果及无密钥双项目回执见
[验证记录](validation/issue255-langfuse-projects/README.md)。

`tests/test_agent_langfuse_binding.py` 覆盖一次绑定、多版本共用、拒绝版本覆盖、
重新绑定与历史保护、权限、环境、并发变更和 YAML 层级。
`tests/test_langfuse_connections.py` 覆盖管理授权、环境和 origin、冻结 Dataset、
凭据轮换 / 停用、PREPARED、项目与认证失败、全局 SDK 隔离、OTLP 故障、
同步入口与 Managed 恢复。Console 的真实 API 测试覆盖绑定、来源展示、
引用删除保护、键盘与窄屏。既有取消、迟到评分、冻结报告和 Demo 基线沿用回归门禁。

两个真实自托管项目的隔离验收入口：

```bash
.venv/bin/python -m pytest -q tests/test_langfuse_connection_integration.py -m trajectory_local
```

该测试使用一次性本地 Langfuse、随机项目凭据、同名不同内容 Dataset、真实 HTTP
Agent 和 OTLP，交叉查询 Observation / Score。Builtin 的 `source=API` Score
只验证项目路由，不作为原生 Rule / `source=EVAL` 的验收证据。

尚未承诺项目数量上限、跨区域 Base URL 兼容矩阵、持续并发 / 内存容量或绑定增长后
导出延迟；16 个远端槽位是实现限制，不是吞吐或租户容量保证。Cloud、多项目原生
Evaluator / Rule 联调需在有授权的真实部署中单独验收。
