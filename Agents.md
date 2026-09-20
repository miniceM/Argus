# Argus — Agent 工作指导规范

> 本文件定义在 Argus 仓库中工作的 AI Agent / Coding Agent 的统一工程行为规范。
> 目标是以 **可验证、可复现、可审查、可维护** 的方式持续演进 Argus。

## 0. 规范级别与适用范围

- **MUST / 必须**：硬性约束，除非任务本身明确修改该约束，否则不得违反。
- **SHOULD / 应该**：默认遵守；若不遵守，交付说明中必须给出原因。
- **MAY / 可以**：按任务需要选择。
- 本规范适用于需求分析、代码修改、测试、重构、文档、配置、Issue/PR 处理和交付说明。
- 所有对用户可见的说明、任务清单、代码注释和仓库文档默认使用中文；代码标识符、协议字段和业界标准术语保持英文。
- 不要求输出内部推理过程。对外只提供结论、证据、实现方案、验证结果和必要的权衡说明。

---

## 1. 项目定位与最高优先级约束

**Argus** 是企业级 AI Agent 低侵入评测与质量门禁平台。

```text
Langfuse = Dataset / Trace / Observation / Experiment / Score 的 System of Record
Argus    = Agent Registry / Remote Execution / Trajectory Evaluation / Release Gate
Agent    = 普通业务应用，不感知评测框架
```

任何实现都必须维护以下架构边界：

1. **业务 Agent 零 Evaluation SDK 依赖**
   - `services/demo-agent/` 以及未来被测 Agent MUST NOT 引入 `langfuse`、Argus Eval SDK 或其他评测 SDK。
   - Agent 仅暴露正常业务 API，例如 `POST /invoke`。
   - 评测元数据优先通过 HTTP Header / W3C Trace Context 传递，不污染 Agent 业务 DTO。

2. **Langfuse 与 Argus 职责分离**
   - Langfuse 负责 Dataset、Trace、Observation、Experiment、Score 及专业分析 UI。
   - Argus 负责执行控制面和企业治理能力。
   - MUST NOT 在 Argus 中重复实现 Langfuse 已成熟的数据模型和 UI。

3. **W3C Trace Context 是跨系统链路标准**
   - Eval Runner 调用 Agent 时 MUST 传播标准 `traceparent`。
   - Agent 内部若已有 OpenTelemetry，应继续向下游 LLM / Tool 传播上下文。
   - OpenTelemetry 是深度可观测增强，不应成为运行基础评测的强制依赖。

4. **评测结果必须可复现**
   - 正式 Experiment 必须能够确定 Dataset Version、Agent Version、Evaluator Version 和 Runner Version。
   - 禁止仅记录 `latest` 而无法恢复真实运行版本。

---

## 2. Agent 标准工作流

处理代码类任务时，按以下顺序执行：

```text
读取 Issue / 设计评论 / 本规范
  ↓
检查现有代码与测试
  ↓
明确最小变更范围
  ↓
RED：先写/调整失败测试
  ↓
GREEN：最小实现
  ↓
REFACTOR：必要时整理
  ↓
运行目标测试
  ↓
运行 make validate
  ↓
检查 diff / 架构约束
  ↓
提交验证结果
```

开始编码前 MUST：

- 阅读相关 Issue；Issue 评论中的设计方案也属于当前设计上下文。
- 将验收标准转换为测试或明确验证项。
- 阅读相关实现和已有测试，不凭文件名或猜测直接修改。
- 确认要改什么、不改什么，以及 API / 数据模型 / 配置兼容性影响。
- 不覆盖与任务无关的现有修改。
- 优先复用现有抽象，避免无必要的新层次。

实现过程中 MUST：

- 每次变更聚焦一个明确目标。
- 不夹带无关重构。
- 第三方 SDK/API 行为不确定时，先检查当前实现或官方文档。
- 发现实现需要偏离 Issue / 设计时，明确说明，不静默改变需求。

---

## 3. TDD 执行要求

TDD 是 Argus 功能开发、Bug 修复和重要重构的默认工作方式。这里只保留执行约束，不重复通用理论。

### 3.1 必须遵守

- **先测试后实现**：功能代码修改前，先增加或调整能表达需求的测试。
- **确认 RED**：实际运行测试，并确认它因目标能力缺失而失败。
- **最小 GREEN**：只实现让目标测试通过所需的最小改动。
- **再 REFACTOR**：测试全绿后再整理结构。
- **Bug 必须有回归测试**：先复现，再修复。
- **不得伪造绿色**：禁止删除断言、放宽正确验收条件、跳过测试或为了测试暴露不必要的生产 API。
- **优先行为测试**：测试业务行为、契约和边界，不绑定无必要的实现细节。
- **覆盖关键失败路径**：涉及网络、并发、状态机时，至少考虑 timeout、retry、429/5xx、幂等、取消/恢复等与任务直接相关的路径。

### 3.2 测试选择

按任务选择最低成本且足够证明正确性的测试：

- 纯业务规则：Unit Test
- Registry / Mapping / 状态机：Domain / Contract Test
- DB / Queue / HTTP / Langfuse 边界：Integration Test
- 完整 `Dataset → Runner → Agent → Langfuse`：E2E Test

不要用 E2E 替代本应由单元或契约测试覆盖的边界条件。

### 3.3 允许例外

纯文档、注释、格式化、仓库元数据等无法合理先写失败测试的修改，可以不机械执行 RED，但仍必须做相应验证。

功能代码若确实不能先写测试，交付说明中必须说明原因和替代验证。

---

## 4. 当前测试基线与质量门禁

本仓库统一质量入口：

```bash
make validate
```

当前包含：

1. 解析 `docker-compose.yml`、`config/agents.yaml`、`data/dataset.json`。
2. 编译 Python 源码。
3. 验证 Demo Agent 不包含 Langfuse / Evaluation SDK 依赖。
4. 验证 Runner 包含 Remote Experiment 与 W3C propagation。
5. 执行全部 `pytest`。
6. 环境具备 Docker 时执行 `docker compose config -q`。

常用命令：

```bash
# TDD 循环：优先运行目标测试
.venv/bin/python -m pytest -q tests/test_registry.py

# 本地完整测试
.venv/bin/python -m pytest -q tests

# 最终质量门禁
make validate

# 完整 PoC
make up
make demo
make down
```

### 当前演示回归基线

- Agent v1：预期 `2 / 6 ≈ 33.3%`。
- Agent v2：预期 `6 / 6 = 100%`。
- 由 `tests/test_expected_pass_rate.py` 保护。
- 若业务预期确需改变，必须说明原因；禁止仅为通过测试修改基线。

---

## 5. 代码与架构修改指导

### 5.1 Eval Runner

`services/eval-runner/` 是 Argus 执行控制面的核心。

修改时必须关注：

- Request Mapping 与 Agent 业务协议解耦。
- Retry / Timeout / Rate Limit 行为可测试。
- W3C `traceparent` 正确传播。
- Execution Failure 与 Evaluation Failure 分离。
- 同一个 DatasetItem 的 Retry 不创建多个逻辑 Experiment Item。
- 幂等、恢复、取消等行为通过明确状态机表达。

### 5.2 Agent Registry

`config/agents.yaml` 当前是 PoC SSOT，后续会演进到持久化 Registry。

- Endpoint、Request Mapping、执行策略属于 AgentVersion，不属于 Dataset。
- 生产设计只保存 `credentialRef`，不保存明文 Secret。
- AgentVersion SHOULD 视为不可变快照；配置变化创建新 version/revision。

### 5.3 Evaluator

`services/eval-runner/app/evaluators.py` 当前以确定性 Evaluator 为主。

- 能用确定性规则解决的问题，不优先引入 LLM Judge。
- Evaluator 输出必须可解释。
- 输入、输出和阈值显式定义。
- LLM Judge 必须固定 Prompt Version 与 Model Version。
- Tool/Trajectory 评测应面向标准 Trajectory，不直接耦合具体 Agent Framework。

### 5.4 Demo Agent

`services/demo-agent/` 是被测业务样例，不是评测平台的一部分。

MUST NOT：

- 引入 Langfuse SDK。
- 引入 Argus Evaluation SDK。
- 读取 Dataset。
- 自己计算 Score。
- 根据“当前正在评测”改变正常业务行为。

MAY：

- 接收标准 W3C `traceparent`。
- 使用正常业务 OpenTelemetry instrumentation。

---

## 6. 事实与证据规范

结论区分以下四类：

- **源码事实**：当前代码、配置、数据模型直接证明。
- **测试事实**：实际执行测试命令得到。
- **运行事实**：真实服务、容器、Langfuse UI、HTTP 调用得到。
- **推断/设计判断**：必须明确标注为判断。

禁止：

- 没运行测试却说“测试已通过”。
- 没启动 Docker 却说“端到端已验证”。
- 只看函数名就推断行为。
- 用 README 描述替代对当前代码的验证。
- 把 Roadmap 能力描述为当前已具备能力。

---

## 7. Git / Issue / PR 工作规范

- Issue 正文中的验收标准是实现与测试输入。
- Issue 评论中的设计方案是当前设计上下文；偏离时要说明原因。
- Commit SHOULD 保持单一目的。
- 使用 Conventional Commits：
  - `feat:` 新功能
  - `fix:` Bug 修复
  - `test:` 测试
  - `refactor:` 重构
  - `docs:` 文档
  - `chore:` 构建、依赖、工具配置
- 不修改与任务无关的文件。
- 不覆盖用户已有未提交修改。
- PR 描述 SHOULD 包含：问题、方案、测试证据、兼容性影响、风险。

---

## 8. 安全与敏感数据规范

- 禁止提交真实 Token、密码、Cookie、API Key、数据库凭证。
- `.env.poc` 仅允许演示凭据。
- 日志、Trace、测试 Fixture 中不得无意保存真实 PII。
- Authorization、Cookie 等敏感 Header 默认不得写入 Langfuse Trace。
- 新增外部调用时，应考虑 timeout、重试边界、SSRF、证书校验和 Secret 获取方式。
- 生产 Agent Endpoint 优先内部网络、mTLS、Network Policy 和 Vault/Secret Manager。

---

## 9. Definition of Done

- [ ] 已阅读相关 Issue、设计评论和现有实现。
- [ ] 验收标准已转换为测试或明确验证项。
- [ ] 功能/修复遵循 RED → GREEN → REFACTOR；例外已说明。
- [ ] 新行为有自动化测试；Bug 有回归测试。
- [ ] 目标测试通过。
- [ ] `make validate` 通过；跳过项已说明原因。
- [ ] 未破坏零 Evaluation SDK、W3C Trace、版本可复现等架构约束。
- [ ] 未引入明文 Secret 或不必要的敏感数据。
- [ ] Diff 无任务外修改。
- [ ] 文档/API/配置在需要时同步更新。
- [ ] 交付说明包含实际验证命令和结果。

---

## 10. 最终交付格式

保持简洁，只报告对用户有用的信息：

```text
完成内容
- ...

测试 / 验证
- RED：...
- GREEN：...
- make validate：PASS / 未执行（原因）

影响与风险
- ...
```
