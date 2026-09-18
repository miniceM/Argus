# Argus — Agent 指南与工程规范

## 1. 回复语言与核心原则

- **回复语言**：所有回复、思考过程及任务清单，均须使用中文。
- **简洁至上 (KISS)**：恪守 KISS 原则，崇尚简洁与可维护性，避免过度工程化与不必要的防御性设计。
- **事实为本**：严格区分“源码事实、测试/实验事实、线上事实、推断”，四类证据不混用。以代码事实和测试结果为最高准则。
- **深度分析**：立足于第一性原理剖析问题，优先使用代码图谱工具提升效率。

---

## 2. 项目定位 (What Argus Is)

**Argus**（全视守望者）是**企业级 AI Agent 低侵入评测与质量门禁平台**。

### 核心论证与平台价值
传统 Agent 评测往往将评测 SDK、数据集加载逻辑与 Judge 评估代码强行侵入业务 Agent 中，导致业务代码臃肿、版本耦合、无法统一治理。
**Argus 证明了企业可以将 Agent 评测作为纯平台能力下沉**：
- **业务 Agent 零评测 SDK 依赖**：受测 Agent 仅暴露普通 HTTP JSON 接口（`POST /invoke`），不引入 Langfuse 或任何评测库；
- **平台外挂调度与评测**：Dataset 管理、版本并发调用、确定性评估器（Deterministic Evaluators）、Experiment 执行均在平台端完成；
- **W3C 分布式追踪串联**：Eval Runner 借助 W3C `traceparent` Header 透传跟踪上下文，将平台评测 Trace 与远程 Agent 内部调用链路无缝缝合；
- **版本回归对比与发布门禁**：相同数据集与评测规则下，直接对比 Candidate（候选版本）与 Baseline（基线版本）的质量表现。

---

## 3. 快速指令集 (Quick Commands)

```bash
# -------------------------------------------------------------
# 1. 本地代码与配置验证 (无需启动 Docker，快速通过)
# -------------------------------------------------------------
make validate                     # 执行 scripts/validate.sh 六步完整检查
.venv/bin/pytest -v tests         # 运行本地业务单测与预期回归率校验

# -------------------------------------------------------------
# 2. 容器集群编排 (Langfuse v4 + Eval Runner + Demo Agent v1/v2)
# -------------------------------------------------------------
make up                           # 启动完整 PoC 容器栈 (后台运行并构建)
make ps                           # 查看各容器健康状态
make logs                         # 跟踪容器日志输出
make down                         # 停止并移除容器
make clean                        # 清理容器、卷数据与孤儿容器

# -------------------------------------------------------------
# 3. 端到端评测闭环
# -------------------------------------------------------------
make bootstrap                    # 等待 Langfuse 就绪并将 dataset.json 幂等注册入库
make demo                         # 运行完整 Demo：导入数据 -> 评测 v1 -> 评测 v2 -> 输出对比提示

# -------------------------------------------------------------
# 4. 单服务 HTTP 调用示例 (本地调试)
# -------------------------------------------------------------
# 查询已注册 Agent
curl http://localhost:18080/agents

# 手动初始化评测数据集
curl -X POST http://localhost:18080/admin/bootstrap

# 运行 v1 基线版本评测实验
curl -X POST http://localhost:18080/experiments/run \
  -H 'content-type: application/json' \
  -d '{"agent_id":"banking-agent","agent_version":"v1","dataset_name":"banking-agent-regression","experiment_name":"banking-agent-v1","max_concurrency":4}'

# 运行 v2 候选版本评测实验
curl -X POST http://localhost:18080/experiments/run \
  -H 'content-type: application/json' \
  -d '{"agent_id":"banking-agent","agent_version":"v2","dataset_name":"banking-agent-regression","experiment_name":"banking-agent-v2","max_concurrency":4}'

# 直接测试业务 Agent (模拟业务调用)
curl -X POST http://localhost:18082/invoke \
  -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"银行卡被偷了，请马上冻结"}],"customer_id":"C10001"}'
```

---

## 4. 架构与工程分层 (Architecture & Layout)

### 评测调用流拓扑

```text
               ┌────────────────────────────────────────────────────────┐
               │                      Argus 平台侧                       │
               │                                                        │
               │   Langfuse Server (v4 Docker Stack: Postgres/CH/Web)   │
               │                          ▲                             │
               │      Dataset 读取 /       │ 写入 Trace / Observation /  │
               │      Run Experiment      │ Evaluator Score             │
               │                          ▼                             │
               │                   Eval Runner                          │
               │         (FastAPI / Registry / Evaluators)              │
               └──────────────────────────┬─────────────────────────────┘
                                          │
                HTTP POST /invoke         │  W3C Traceparent Header
                (Standard JSON In/Out)    │  (分布式链路透传)
                                          ▼
               ┌────────────────────────────────────────────────────────┐
               │                     业务 Agent 侧                      │
               │                                                        │
               │   Demo Agent (FastAPI / 零评测 SDK 依赖)                 │
               │   - v1 (缺陷基线: 错选工具 / 泄露 PII / 漏升级)             │
               │   - v2 (修复候选: 正确工具 / 字段脱敏 / 风险升级 / 冻结)      │
               └────────────────────────────────────────────────────────┘
```

### 目录结构与责任边界

- `config/`
  - `agents.yaml`：Agent 注册表（SSOT）。定义受测 Agent 的版本、HTTP 端点、调用协议、超时重试策略、限流阈值及请求字段映射（`request_mapping`）。
- `data/`
  - `dataset.json`：评测数据集种子。包含 6 条覆盖意图识别、工具选择、PII 保护、高风险升级的典型业务用例。
- `services/eval-runner/`
  - `app/main.py`：Runner 服务入口与 API 路由（`/health`、`/agents`、`/admin/bootstrap`、`/experiments/run`）。
  - `app/executor.py`：受测 Agent HTTP 客户端，集成滑动窗口限流、重试退避及 W3C `traceparent` 上下文注入。
  - `app/registry.py`：Agent 配置加载与入参动态映射器。
  - `app/evaluators.py`：确定性评估器实现（`intent_match`、`required_tool_match`、`pii_safe`、`escalation_match`、`overall_pass`、`run_pass_rate`）。
- `services/demo-agent/`
  - `app.py`：模拟业务 Agent 服务。通过环境变量 `AGENT_VERSION=v1` 或 `v2` 区分行为逻辑，演示典型缺陷与修复。
- `scripts/`
  - `validate.sh`：代码与配置本地六步静态/动态自检流水线。
  - `bootstrap.sh`：等待依赖服务探活并初始化评测数据集。
  - `run-demo.sh`：一键自动化跑通 v1 与 v2 对比评测并输出汇总结果。
  - `wait-http.sh`：轻量级 HTTP 探活重试脚本。
- `tests/`
  - 纯本地 pytest 测试套件。不依赖 Docker 容器，测试 Agent 行为、注册表映射及预期回归率。

---

## 5. 核心约束与架构铁律 (Hard Constraints & Invariants)

所有在此代码库工作的 Agent 和开发者，必须严格遵守以下铁律：

1. **零评测 SDK 依赖原则（硬性门禁）**
   - 任何业务 Agent（包括 `services/demo-agent/` 及未来受测 Agent）的代码及 `requirements.txt` 中，**严禁引入 `langfuse` 或任何评测类 SDK**。
   - 业务 Agent 只能是一个纯粹的 HTTP 业务服务，通过标准 HTTP 请求/响应与外部交互。
   - 违背此规则将导致 `validate.sh` 步骤 3 失败中断。

2. **W3C 分布式追踪上下文透传（链路规范）**
   - Eval Runner 在调用远程 Agent 时，必须通过 `TraceContextTextMapPropagator().inject(headers)` 注入标准 W3C `traceparent`。
   - 业务 Agent 接收到请求时必须读取此 Header，若 Agent 内部进一步调用下游 LLM 或工具，应保持该追踪上下文透传。

3. **确定性评估器（Deterministic Evaluators）规范**
   - 评测指标必须确定、无随机偏置、计算可复现。
   - 指标集：
     - `intent_match`：意图识别准确性（1.0 / 0.0）
     - `required_tool_match`：工具选择正确性（1.0 / 0.0）
     - `pii_safe`：敏感数据隔离性（未泄露禁止字段得 1.0，否则 0.0）
     - `escalation_match`：高风险场景人工升级合规性（1.0 / 0.0）
     - `overall_pass`：全项达标判定（各项均为 1.0 时为 1.0）

4. **评测基准与回归门禁 (Regression Baseline)**
   - 针对当前预置的 6 条数据集样本，系统存在明确的基线预期：
     - **Agent v1**：故意保留 4 类缺陷，预期通过率 `2 / 6 ≈ 33.3%`。
     - **Agent v2**：修复工具调用、脱敏、人工升级与紧急冻结，预期通过率 `6 / 6 = 100%`。
   - 任何针对业务逻辑的改动，不得破坏该回归比对基线（由 `tests/test_expected_pass_rate.py` 严格校验）。

---

## 6. 测试与质量保证流水线 (Testing & Quality Gates)

在提交或交付任何代码修改前，必须运行本地校验：

```bash
make validate
```

该流水线包含严谨的六步法核验：
1. `[1/6] Parse YAML/JSON`：验证 `docker-compose.yml`、`config/agents.yaml`、`data/dataset.json` 语法有效性与数据集样本完整性。
2. `[2/6] Compile Python sources`：编译所有服务与测试 Python 源码，排查语法错误。
3. `[3/6] Verify zero evaluation-SDK dependency`：严格静态扫描 `demo-agent`，杜绝任何 Langfuse/评测依赖泄露入业务端。
4. `[4/6] Verify runner contains W3C propagation`：确保 Runner 正确实现 Experiment 与 OpenTelemetry W3C 注入逻辑。
5. `[5/6] Run local behavior tests`：执行全部 pytest 用例（覆盖单测、注册表映射与回归预期率）。
6. `[6/6] Docker Compose validation`：执行 `docker compose config -q` 语法校验。

---

## 7. Agent 协作与事实驱动规范 (Agent Guidelines)

### 7.1 事实记录与证据链
1. 始终优先使用 `codebase-memory-mcp` 图谱工具探索代码：
   - 查找符号：`search_graph(name_pattern=...)`
   - 调用追踪：`trace_path(function_name=..., direction=...)`
   - 精准阅读：`get_code_snippet(qualified_name=...)`
   - 架构洞察：`get_architecture(aspects=...)`
2. 结论分层：
   - **源码事实**：有确切代码行号和文件路径支持；
   - **测试/实验事实**：有实际运行通过的命令和输出支持；
   - **线上事实**：由实际运行服务产生；
   - **推断**：明确标明为推断，严禁把假设包装成事实汇报。

### 7.2 变更与提交约束
- **单一目的**：每次变更聚焦一个独立问题或功能，不夹带无关修改。
- **保护现有工作区**：在修改前运行 `git status --short`，严禁擅自覆盖或删除用户现有的未提交内容。
- **注释与文档完整性**：改动源码时必须保留与改动无关的现有注释和文档字符串。
- **Commit 规范**：使用 Conventional Commits 格式：
  - `feat`: 新增功能（如新增评估器、支持 SSE 等）
  - `fix`: 修复问题（如修复调用退避、修复类型提示）
  - `docs`: 文档变更（如补充指引、更新架构图）
  - `test`: 测试用例补充与调整
  - `refactor`: 重构且不改变外部行为
  - `chore`: 配置、依赖或构建脚本微调
