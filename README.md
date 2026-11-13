# Argus

> **Agent 评测桥接、持久化执行与发布质量证据/门禁平台**
>
> 连接已有业务服务与 Langfuse，把分散的接入、执行恢复和发布质量约定收敛为可审计流程；不接管业务 Runtime 或生产发布。

[![CI](https://github.com/miniceM/Argus/actions/workflows/ci.yml/badge.svg)](https://github.com/miniceM/Argus/actions/workflows/ci.yml)
[![Langfuse i18n](https://github.com/miniceM/Argus/actions/workflows/langfuse-i18n.yml/badge.svg)](https://github.com/miniceM/Argus/actions/workflows/langfuse-i18n.yml)

Argus 在 [Langfuse](https://langfuse.com/) 之上提供统一的服务接入、持久化评测执行、冻结结果与质量门禁。对已支持 HTTP 契约的业务 Agent，不要求内嵌 Evaluation SDK；被测服务不受评测程序语言限制。

Langfuse SDK + 外置脚本同样可以跨语言调用服务，并提供实验/比较与 CI 评测能力。Argus 的价值在于统一多团队的适配、恢复、证据与门禁约束，而不是独占“零业务 SDK”或“可以阻断回归”。

```text
Langfuse = 日常 Dataset/Prompt、Trace/Observation、实验探索与在线评估
Argus    = 接入 / 持久化执行 / 冻结证据 / 批准基准 / 发布质量 Gate
Agent    = 正常业务 API 与业务 Runtime；内部 OTel 可选
CI/CD    = 构建、部署、审批、灰度、回滚与最终发布授权
```

完整职责与非目标见 [产品边界与需求准入](docs/product-boundaries.md)。下文“已实现”与 Roadmap 目标分开，文档定位不代表所有企业级保护均已交付。

> 本文面向**使用 Argus 的读者**：只想跑起来、评估它是否适合自己的团队，先看下面两节即可。
> 需要读代码、改代码或参与开发，请直接看 [AGENTS.md](./AGENTS.md) 与 [TECHNICAL_DESIGN.md](./TECHNICAL_DESIGN.md)。

---

## 快速开始

### 环境要求

| 依赖 | 版本 | 用途 |
|---|---|---|
| Docker Engine / Docker Desktop + Compose v2 | 建议 ≥ 8 GB 可用内存 | 本地 Langfuse、PostgreSQL、Redis、Runner、Console |
| Python | 3.12 | 源码方式运行 Eval Runner / 本地质量门禁 |
| Node.js + pnpm | Node 22（CI 固定 pnpm 10.5.2） | Argus Console |
| `make`、`curl` | — | 环境编排与演示脚本 |

### 五分钟跑通一次完整评测

```bash
git clone https://github.com/miniceM/Argus.git
cd Argus

make up        # 构建并启动 Langfuse + Argus Runner + Console + Demo Agent
make demo      # 等待就绪 → 导入 Demo Registry → 跑 v1 / v2 回归
```

`make demo` 会输出两个 Experiment 的结果 JSON，并把 Trace / Score 写入 Langfuse。完整演示流程见 [walkthrough.md](./walkthrough.md)。

| 服务 | 地址 |
|---|---|
| Argus Console | http://localhost:18083 |
| Eval Runner API | http://localhost:18080 |
| FastAPI / OpenAPI Docs | http://localhost:18080/docs |
| Langfuse | http://localhost:3000 |
| Demo Agent v1 / v2 | http://localhost:18081 / http://localhost:18082 |
| Real LLM Agent（可选，需三项模型配置，见 [services/real-llm-agent/README.md](./services/real-llm-agent/README.md)） | http://127.0.0.1:18084 |

Langfuse 演示账号：`admin@example.com` / `Poc-Admin-2026!`（仅本地 PoC 环境，凭据见 `.env.poc`）。

```bash
make down      # 停止
make clean     # 停止并删除本地 Volume
```

---

## 为什么需要 Argus

如果一个团队用少量 Langfuse 脚本已能满足需求，不必为了“平台化”再部署 Argus。以下需求同时存在时，再评估统一控制面的收益：

- 多个服务的协议、鉴权、输入输出映射反复维护；
- 长任务需要持久化进展、故障恢复、取消和尝试隔离；
- 外部评分需要来源验证，不能把缺失/错误当作通过；
- 发布需要固定候选、环境、策略、批准基准和不可变证据；
- 希望 CI 消费统一、可解释的质量门禁，而不是各团队各写一套约定。

```text
Langfuse Dataset / 固定评分契约
             ↓
Argus Launch → Queue / Worker → HTTP 业务 Agent（任意语言）
             ↓
可信结果 → 不可变 Snapshot → 批准基准比较 → Quality Gate → CI/CD
             └─ Trace / Score 投影与详细分析 → Langfuse
```

基础最终输出评测不依赖内部埋点。深入工具/轨迹判断需要相应可信证据；没有证据时不会凭空补造。超时重试也不能替代远端业务幂等。

---

## 核心概念

| 对象 | 一句话 |
|---|---|
| `AgentDefinition` | 一个可被评测的 Agent 身份 |
| `AgentVersion` | 该 Agent 的不可变调用配置：Endpoint、协议、Request Mapping、执行策略、Credential 引用；不自动证明远端制品身份 |
| `ExperimentLaunch` | 一次可审计的评测请求，冻结 Dataset / Agent / Evaluator / Runner 四个版本 |
| `ExperimentItemExecution` / `ExecutionAttempt` | 单个 Dataset Item 的逻辑执行与技术重试分离，重试不会被算成多个样本 |
| `Baseline` | 符合资格的不可变结果快照，批准后精确绑定到 Candidate，非“最新一次运行” |

字段、状态机与 API 语义见 [TECHNICAL_DESIGN.md](./TECHNICAL_DESIGN.md)。

### 接入自己的 Agent

1. 用 `POST /api/v1/agents` 注册 Agent，再用 `POST /api/v1/agent-versions` 登记一个不可变版本（Endpoint、协议、Request Mapping、执行策略、Credential 引用）；
2. 准备 Langfuse Dataset，创建 `POST /api/v1/experiment-launches`（可选 `POST /api/v1/experiment-launches/run` 立即异步执行）；
3. 用 `GET /api/v1/experiment-launches/{id}/summary` 与 `/comparison` 查看结果，并在 Console 或 Langfuse 中分析 Trace 与 Score。

完整请求 / 响应结构以 [docs/openapi.json](./docs/openapi.json) 与运行时 `/docs` 为准；`services/demo-agent/` 是最小可运行参考实现。

### 设计边界

受认证保护的同步 HTTP 服务，参见 [HTTP Agent 认证、Headers 与回答提取](docs/http-agent-integration.md)，包含 Console 配置流程与 AgentScope Java DataAgent 的部署前置和验收限制。

### 设计边界

1. **业务服务不必集成评测 SDK**：保证范围限于已支持接口；已有可观测性不要求移除。
2. **一个对象一个权威方**：Argus 保存发布必需的冻结证据，不重建日常 Dataset/Prompt/Trace 分析平台。
3. **冻结不夸大保证**：版本声明不等于远端制品已核验；随机模型和外部依赖不承诺完全确定性。
4. **门禁不等于发布授权**：PASS 只说明满足指定质量策略，最终部署由 CI/CD 和审批系统负责。
5. **历史与缺失证据诚实呈现**：重评新修订，来源不明/缺分保持 UNKNOWN，不能以均分洗绿。

---

## 能力概览

### 已实现

| 能力域 | 内容 |
|---|---|
| Agent Registry | PostgreSQL 持久化 `AgentDefinition` / `AgentVersion`；不可变版本；选择稳定 Credential ID，Token 加密存储、运行时解析（[接入说明](docs/secret-providers.md)） |
| Versioned Launch | 四维冻结 Manifest、Dataset 版本与内容校验、Idempotency-Key 与冲突检测、Launch 生命周期持久化 |
| Remote Runner | `SYNC_HTTP` 调用、Request Mapping、timeout / retry / rate limit、W3C Trace Context 注入、逐次 Attempt 记录 |
| 异步执行 | Redis Streams 队列 + Worker、可靠执行状态机、`cancel` / `resume` / `retry-failed`、分布式限流 |
| 评测与对比 | 确定性 item-level Evaluator、Run-level 汇总与 Score、Baseline 绑定、Candidate 对比（含单用例） |
| Console | React + TypeScript 控制台：Agent、Launch、Evaluator、对比视图 |
| 发布门禁 | 不可变策略、固定 Snapshot 的绝对/相对规则、critical veto、Gate API；见 [发布门禁](./docs/release-gates.md) 与 [CLI/CI 集成](./docs/release-cli.md) |
| 界面语言 | 官方 Langfuse 的独立 `zh-CN` Patch Layer 镜像 |

### Roadmap（2026-10-27 收敛）

| 主题 | 已有基础与后续边界 |
|---|---|
| 接入与持久化执行 | 复用 S1/S2；新协议按真实需求准入，不建设通用工作流引擎 |
| 冻结评测与外部评分 | #49、#212–#219 已关闭；继续复用既有契约，不据此宣称新发布身份保护已验收 |
| 最小安全/审计接入（#6、#31） | 补齐受控部署所需的身份/资源授权、Secret、目标访问和数据审计；不自建 IdP/Vault |
| 发布质量证据与 CI（#7） | Gate/API/CLI 已有；收尾聚焦被测制品身份、CI 适用范围/失效与安全验收，不接管部署 |
| 轨迹增强（#5、#127–#132） | 已有实现/历史证据继续维护；按需验证，不是普通发布闭环前置；#131 自建 Judge 默认延期 |
| i18n 补丁 | 兼容维护已有成果，不作为平台核心差异或新增企业能力的前置 |

当前不是已经验收的完整企业发布平台。受信远端身份核验、判定有效期/CI 防错配和完整授权审计仍须按 [Roadmap #1](https://github.com/miniceM/Argus/issues/1) 取得证据。深度轨迹、自建 Judge、全部 SSO/Vault/CI Provider 组合不再作为 v1.0 必选清单。

---

## 架构一览

```text
业务 Agent（任意语言 / 正常 API / 内部观测可选）
                      ↑ HTTP + traceparent
Argus：Registry → 持久化 Launch/Attempt → 评分采纳 → Snapshot/基准 → Gate
                      ↕                         ↓
Langfuse：Dataset / Trace / 托管评分 / 实验分析    CI/CD：核验质量证据后决定发布
```

这是职责图，不表示远端身份核验、授权和 CI 防错配等目标已全部实现。模块复用既有部署，不要求拆成四套微服务。

---

## 文档导航

本文只保留上手必需的信息。深入细节按下表分流：

| 你想知道 | 去看 |
|---|---|
| 产品职责、非目标、需求准入与近期验收 | [产品边界与需求准入](docs/product-boundaries.md) |
| 领域模型、状态机、API 设计细节 | [TECHNICAL_DESIGN.md](./TECHNICAL_DESIGN.md) |
| 完整 API 合约 | [docs/openapi.json](./docs/openapi.json) 或运行时的 `/docs` |
| Agent 凭据管理 | [凭据 Provider 与部署边界](./docs/secret-providers.md) |
| 多团队 Langfuse 项目接入 | [项目绑定、凭据与冻结来源](./docs/langfuse-project-connections.md) |
| 端到端演示流程 | [walkthrough.md](./walkthrough.md) |
| Langfuse `zh-CN` 镜像构建与发布 | [deploy/langfuse/README.md](./deploy/langfuse/README.md) |
| Console 托管评分、冻结报告与 Release Gate | [Flowise → Argus → Langfuse 用户指南](./docs/langfuse-managed-evaluation.md) |
| Langfuse 原生托管双 Evaluator 独立实测（#214 范围） | [Issue #214 验收与复现](./docs/validation/issue214-managed-evaluation/README.md) |
| Flowise 原生 HTTP 工具 Agent（#213） | [部署与联调](./examples/flowise/README.md) · [真实验收记录](./docs/validation/issue-213/README.md) |
| 最近一次验证记录 | [VALIDATION_REPORT.md](./VALIDATION_REPORT.md) |
| 参与开发、代码规范、质量门禁 | [AGENTS.md](./AGENTS.md) |
| 提交 PR 前要做什么 | [CONTRIBUTING.md](./CONTRIBUTING.md) |
| 报告安全漏洞 | [SECURITY.md](./SECURITY.md) |
| 阶段规划与里程碑 | [Roadmap Issue #1](https://github.com/miniceM/Argus/issues/1) |

---

## 部署要点

- **ARM64 内网离线更新**：[交付与操作指南](deploy/airgap/README.md)，包含站点密钥、仅 Argus 升级、备份恢复及验收边界。
- **本地环境**：`make up` 使用 `.env.poc` 中的演示凭据启动全栈，仅适用于开发与回归验证。
- **构建身份**：`make up` 会把当前 Git commit SHA 作为 `ARGUS_BUILD_ID` 注入 Runner 镜像；自建镜像部署时必须设置不可变、可定位的 commit SHA 或镜像摘要，空值与 `dev` / `latest` 不会被接受为正式 Launch。
- **Langfuse Cloud**：在 `.env.cloud` 中同时配置 API 地址（`LANGFUSE_BASE_URL`）与浏览器地址（`ARGUS_LANGFUSE_DASHBOARD_URL`），示例见 [`.env.cloud.example`](./.env.cloud.example)。
- **生产部署**：必须替换演示凭据，并接入 Secret Manager、网络隔离、身份认证与审计能力。仓库内的 Demo Agent、示例 Dataset 与本地 Compose 不代表产品边界。

---

## 质量结论如何判定

新建 Launch 默认采用**逐项诊断**：分别记录意图、工具调用、敏感信息与升级处理四项评分。也可切换为**复合结论**，只记录一个 `overall_pass`。

**执行成功不等于质量通过**——Runner 跑完只是执行完成，是否放行由质量结论决定。未选择或被取消的诊断指标不参与本次判定；没有可用 Evaluator 时结论为 unknown，不会默认判通过。

仓库内置的回归基线与判定细节见 [AGENTS.md](./AGENTS.md)。
