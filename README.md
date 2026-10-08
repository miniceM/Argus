# Argus

> **企业级 AI Agent 低侵入评测与质量门禁平台**
>
> 把 Agent 评测从"每个应用仓库里的测试脚本"升级为统一、可复现、可治理的平台能力。

[![CI](https://github.com/miniceM/Argus/actions/workflows/ci.yml/badge.svg)](https://github.com/miniceM/Argus/actions/workflows/ci.yml)
[![Langfuse i18n](https://github.com/miniceM/Argus/actions/workflows/langfuse-i18n.yml/badge.svg)](https://github.com/miniceM/Argus/actions/workflows/langfuse-i18n.yml)

Argus 在 [Langfuse](https://langfuse.com/) 已有的 Dataset / Trace / Observation / Experiment / Score 之上，补齐 **Agent Registry、版本化评测、远程执行与发布门禁** 这层执行控制面。业务 Agent 保持正常业务 API，不引入任何评测 SDK。

```text
Langfuse = Dataset / Trace / Observation / Experiment / Score 的 System of Record
Argus    = Agent Registry / Versioned Launch / Remote Execution / Evaluation Orchestration / Release Gate
Agent    = 正常业务应用，不感知 Evaluation Framework
```

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

Langfuse 演示账号：`admin@example.com` / `Poc-Admin-2026!`（仅本地 PoC 环境，凭据见 `.env.poc`）。

```bash
make down      # 停止
make clean     # 停止并删除本地 Volume
```

---

## 为什么需要 Argus

把评测放进每个 Agent 仓库，随着团队和 Agent 数量增长会遇到：

- 评测 SDK 与业务代码强耦合；
- timeout、retry、并发、rate limit 等执行逻辑重复建设；
- Dataset、Evaluator、Baseline、Score 缺乏统一版本治理；
- 测试调用路径可能偏离真实生产 API；
- Java、Python、Node 及不同 Agent Framework 需要分别适配；
- 很难形成统一、可审计的发布质量门禁。

Argus 把这些能力上移到统一控制面：

```text
Dataset
   │
   ▼
Experiment Launch ── freeze Dataset / Agent / Evaluator / Runner Version
   │
   ▼
Remote Agent Runner ── W3C Trace Context ──► 业务 Agent
   │
   ├─ Retry / Timeout / Rate Limit
   ├─ Item Execution / Attempt
   ├─ Evaluation
   └─ Langfuse Trace / Score / Experiment
```

---

## 核心概念

| 对象 | 一句话 |
|---|---|
| `AgentDefinition` | 一个可被评测的 Agent 身份 |
| `AgentVersion` | 该 Agent 的不可变可执行快照：Endpoint、协议、Request Mapping、执行策略、Credential 引用 |
| `ExperimentLaunch` | 一次可审计的评测请求，冻结 Dataset / Agent / Evaluator / Runner 四个版本 |
| `ExperimentItemExecution` / `ExecutionAttempt` | 单个 Dataset Item 的逻辑执行与技术重试分离，重试不会被算成多个样本 |
| `Baseline` | 一次完成结果，可绑定为后续 Candidate 的比较基准 |

字段、状态机与 API 语义见 [TECHNICAL_DESIGN.md](./TECHNICAL_DESIGN.md)。

### 接入自己的 Agent

1. 用 `POST /api/v1/agents` 注册 Agent，再用 `POST /api/v1/agent-versions` 登记一个不可变版本（Endpoint、协议、Request Mapping、执行策略、Credential 引用）；
2. 准备 Langfuse Dataset，创建 `POST /api/v1/experiment-launches`（可选 `POST /api/v1/experiment-launches/run` 立即异步执行）；
3. 用 `GET /api/v1/experiment-launches/{id}/summary` 与 `/comparison` 查看结果，并在 Console 或 Langfuse 中分析 Trace 与 Score。

完整请求 / 响应结构以 [docs/openapi.json](./docs/openapi.json) 与运行时 `/docs` 为准；`services/demo-agent/` 是最小可运行参考实现。

### 四条设计边界

1. **业务 Agent 零 Evaluation SDK 侵入**：Agent 只需提供正常业务 API，不读取 Dataset、不自行计算 Score、不为"正在评测"加分支。
2. **Langfuse 与 Argus 职责分离**：Argus 不重复实现 Langfuse 已成熟的数据模型与分析 UI。
3. **评测必须可复现**：正式 Experiment 必须能确定 Dataset / Agent / Evaluator / Runner 四个版本，禁止只记录 `latest`。
4. **评测上下文不污染业务 DTO**：通过标准 W3C `traceparent` 与 `X-Eval-*` Header 传递；Agent 已有 OpenTelemetry 时可继续向下游关联。

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
| 界面语言 | 官方 Langfuse 的独立 `zh-CN` Patch Layer 镜像 |

### Roadmap

| 阶段 | 主题 | 状态 |
|---|---|---|
| S1 | Agent Registry 与版本化评测领域模型 | ✅ 已完成 |
| S1.5 | 官方 Langfuse + 独立 i18n Patch Layer | ✅ 已完成 |
| S2 | 异步 Orchestrator、Queue / Worker、可靠执行状态机 | ✅ 已完成 |
| S3 | 版本化评测、Baseline Comparison、Run-level Score | ✅ 已完成 |
| S4 | Standard Agent Trajectory、Trace Assembler、深度轨迹评测 | 🚧 规划中 |
| S5 | Vault、RBAC、SSO、Audit、数据脱敏 | 🚧 规划中 |
| S6 | ReleasePolicy、Release Gate、CLI、CI/CD 集成 | 🚧 规划中 |

当前版本仍不是完整的 v1.0 企业平台；尚未覆盖 LLM-as-a-Judge 版本治理、Agent Trajectory、Worker 独立扩缩容、SSO / RBAC、Release Gate 等能力。路线图见 [Roadmap Issue #1](https://github.com/miniceM/Argus/issues/1)。

---

## 架构一览

```text
+----------------------------------------------------------------------------+
| 业务 Agent Runtime   正常业务应用，不感知评测框架                          |
+----------------------------------------------------------------------------+
| 只暴露正常业务 API，例如 POST /invoke                                      |
| 不引入 Langfuse / Argus SDK，不读 Dataset，不自行评分                      |
+----------------------------------------------------------------------------+
                            HTTP + W3C traceparent
                                      ^
                                      |
+----------------------------------------------------------------------------+
| Argus 控制面   services/eval-runner                                        |
+----------------------------------------------------------------------------+
| Agent Registry         Agent / 不可变 AgentVersion                         |
| Experiment Launch      冻结 Dataset / Agent / Evaluator / Runner           |
| Queue / Worker         状态机 / retry / cancel / resume                    |
| Evaluator + Baseline   逐项评分 / 候选对比                                 |
+----------------------------------------------------------------------------+
                  写入 Dataset / Trace / Experiment / Score
                                      |
                                      v
+----------------------------------------------------------------------------+
| Langfuse   评测数据 System of Record                                       |
+----------------------------------------------------------------------------+
| Dataset / Trace / Observation / Experiment / Score                         |
| 提供专业分析 UI；Argus 不重复实现这些模型                                  |
+----------------------------------------------------------------------------+
```

---

## 文档导航

本文只保留上手必需的信息。深入细节按下表分流：

| 你想知道 | 去看 |
|---|---|
| 领域模型、状态机、API 设计细节 | [TECHNICAL_DESIGN.md](./TECHNICAL_DESIGN.md) |
| 完整 API 合约 | [docs/openapi.json](./docs/openapi.json) 或运行时的 `/docs` |
| Agent 鉴权与 Vault 接入 | [凭据 Provider 与部署边界](./docs/secret-providers.md) |
| 端到端演示流程 | [walkthrough.md](./walkthrough.md) |
| Langfuse `zh-CN` 镜像构建与发布 | [deploy/langfuse/README.md](./deploy/langfuse/README.md) |
| 最近一次验证记录 | [VALIDATION_REPORT.md](./VALIDATION_REPORT.md) |
| 参与开发、代码规范、质量门禁 | [AGENTS.md](./AGENTS.md) |
| 提交 PR 前要做什么 | [CONTRIBUTING.md](./CONTRIBUTING.md) |
| 报告安全漏洞 | [SECURITY.md](./SECURITY.md) |
| 阶段规划与里程碑 | [Roadmap Issue #1](https://github.com/miniceM/Argus/issues/1) |

---

## 部署要点

- **本地环境**：`make up` 使用 `.env.poc` 中的演示凭据启动全栈，仅适用于开发与回归验证。
- **构建身份**：`make up` 会把当前 Git commit SHA 作为 `ARGUS_BUILD_ID` 注入 Runner 镜像；自建镜像部署时必须设置不可变、可定位的 commit SHA 或镜像摘要，空值与 `dev` / `latest` 不会被接受为正式 Launch。
- **Langfuse Cloud**：在 `.env.cloud` 中同时配置 API 地址（`LANGFUSE_BASE_URL`）与浏览器地址（`ARGUS_LANGFUSE_DASHBOARD_URL`），示例见 [`.env.cloud.example`](./.env.cloud.example)。
- **生产部署**：必须替换演示凭据，并接入 Secret Manager、网络隔离、身份认证与审计能力。仓库内的 Demo Agent、示例 Dataset 与本地 Compose 不代表产品边界。

---

## 质量结论如何判定

新建 Launch 默认采用**逐项诊断**：分别记录意图、工具调用、敏感信息与升级处理四项评分。也可切换为**复合结论**，只记录一个 `overall_pass`。

**执行成功不等于质量通过**——Runner 跑完只是执行完成，是否放行由质量结论决定。未选择或被取消的诊断指标不参与本次判定；没有可用 Evaluator 时结论为 unknown，不会默认判通过。

仓库内置的回归基线与判定细节见 [AGENTS.md](./AGENTS.md)。
