# Argus — Agent 工作指导规范

> 本文件定义在 Argus 仓库中工作的 AI Agent / Coding Agent 的统一工程行为规范。
> 目标不是“尽快写出代码”，而是以 **可验证、可复现、可审查、可维护** 的方式持续演进 Argus。

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

平台核心理念：

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
   - MUST NOT 为了方便在 Argus 中重新实现一套 Langfuse 已成熟的数据模型和 UI。

3. **W3C Trace Context 是跨系统链路标准**
   - Eval Runner 调用 Agent 时 MUST 传播标准 `traceparent`。
   - Agent 内部若已有 OpenTelemetry，应继续向下游 LLM / Tool 传播上下文。
   - OpenTelemetry 是深度可观测增强，不应成为运行基础评测的强制依赖。

4. **评测结果必须可复现**
   - 正式 Experiment 必须能够确定 Dataset Version、Agent Version、Evaluator Version 和 Runner Version。
   - 禁止仅记录 `latest` 而无法恢复真实运行版本。

---

## 2. Agent 的标准工作流程

Agent 接到任何非纯文本修改任务时，MUST 按以下顺序工作：

```text
理解需求
  ↓
读取 Issue / 设计评论 / AGENTS 规范
  ↓
检查现有代码与测试
  ↓
形成最小变更计划
  ↓
RED：先写失败测试
  ↓
GREEN：最小实现使测试通过
  ↓
REFACTOR：在测试保护下整理设计
  ↓
运行局部测试
  ↓
运行 make validate
  ↓
检查 diff / 回归 / 架构约束
  ↓
提交结果与验证证据
```

### 2.1 开始编码前 MUST 完成

- 阅读当前任务对应的 GitHub Issue；如果 Issue 评论中存在设计方案，也必须阅读。
- 把 Issue 的验收标准转换成可执行测试或明确的验证项。
- 阅读相关实现和已有测试，禁止仅根据文件名或猜测直接改代码。
- 确认本次任务的边界：要改什么、不改什么、是否涉及公开 API / 数据模型 /配置兼容性。
- 检查当前工作区或分支状态；禁止覆盖与任务无关的用户改动。
- 优先复用现有抽象；新增抽象前先证明现有抽象不能满足需求。

### 2.2 实现过程中 MUST 保持

- 一个变更只解决一个明确问题，避免顺手大规模重构。
- 代码事实优先于猜测；测试结果优先于口头判断。
- 发现设计与 Issue 不一致时，不得静默改变需求，应在交付说明中指出差异。
- 对外部系统行为、第三方 SDK/API 行为不确定时，先查当前实现或官方文档，不凭记忆编造接口。
- 不以“测试最终会过”为理由跳过 RED 阶段。

---

## 3. TDD 是默认且强制的工作范式

Argus 的功能开发、Bug 修复和重要重构默认采用 **Test-Driven Development（TDD）**。

核心循环只有三步：

```text
RED → GREEN → REFACTOR
```

### 3.1 RED：先证明需求当前没有被满足

在修改生产代码之前，MUST 先增加或修改测试，使其因为目标能力尚未实现而失败。

RED 阶段要求：

- 测试必须表达业务行为或契约，而不是绑定某个实现细节。
- 必须实际运行新增测试，并确认它 **因预期原因失败**。
- 如果测试意外通过，说明测试没有覆盖新需求；应重新设计测试，而不是直接进入编码。
- Bug 修复 MUST 先写能够稳定复现 Bug 的回归测试。
- 新 API / 新数据模型 SHOULD 先从契约测试或领域行为测试开始。

示例：

```text
Issue：429 应按照 Retry-After 重试

错误做法：
先修改 executor.py → 再补一个会通过的测试

正确做法：
1. 写 test_retry_respects_retry_after()
2. 运行测试，确认当前实现失败
3. 再修改 executor.py
```

### 3.2 GREEN：只做让测试通过所需的最小实现

GREEN 阶段的目标不是“设计最完美”，而是用最小变更满足刚刚失败的测试。

MUST：

- 优先修改最小范围代码。
- 不夹带与当前测试无关的重构。
- 不通过删除断言、放宽测试条件或跳过测试来制造绿色结果。
- 不为了测试方便把生产代码暴露出不必要的 API。
- 测试通过后再考虑结构优化。

### 3.3 REFACTOR：在全绿状态下改善设计

只有在目标测试已经 GREEN 后，才能进行重构。

REFACTOR 阶段可以：

- 消除重复。
- 改善命名。
- 提取清晰的领域对象或函数。
- 简化控制流。
- 优化职责边界。
- 删除已经没有价值的兼容代码。

但必须满足：

- 外部可观察行为不变。
- 每一小步重构后重新运行相关测试。
- 如果重构导致测试失败，优先恢复绿色状态再继续。

### 3.4 TDD 的例外

以下变更可以不机械执行“先写失败单测”，但仍必须有验证：

- 纯文档、注释、拼写修正。
- 纯格式化且不改变语义。
- 无法通过自动化测试表达的仓库元数据变更。

即使属于例外，也 SHOULD 使用最接近的自动验证，例如 Markdown lint、YAML/JSON parse、`docker compose config -q` 等。

如果功能代码确实无法先写测试，交付说明中 MUST 明确：

1. 为什么不能先写失败测试；
2. 使用了什么替代验证；
3. 后续是否需要补自动化测试。

“时间不够”“改动很小”“我认为不会出错”不是有效例外理由。

### 3.5 测试分层策略

按照从便宜到昂贵的顺序选择测试：

```text
Unit Test
   ↓
Domain / Contract Test
   ↓
Integration Test
   ↓
End-to-End Test
```

原则：

- 纯业务规则优先 Unit Test。
- Agent Registry、Request Mapping、状态机等优先 Domain/Contract Test。
- Langfuse、DB、Queue、HTTP 边界使用 Integration Test。
- 完整 `Dataset → Runner → Remote Agent → Langfuse` 链路使用 E2E Test。
- 不应依赖 E2E 测试覆盖所有边界条件；E2E 用于证明系统组合正确。

### 3.6 Bug 修复的固定模板

每个 Bug 修复 SHOULD 遵循：

```text
1. Reproduce：复现问题
2. RED：新增回归测试，确认失败
3. Fix：最小修复
4. GREEN：确认回归测试通过
5. Regression：运行受影响测试集
6. Refactor：必要时整理代码
7. Validate：运行 make validate
```

### 3.7 不允许的“伪 TDD”

- 先写完实现，再补测试证明实现正确。
- 为了让实现通过而修改原本正确的验收条件。
- 测试只断言函数被调用，而不验证核心业务行为。
- 使用过度 Mock 导致真实契约根本未被验证。
- 只测试 happy path，不覆盖失败、重试、超时、幂等和边界条件。
- Bug 修复没有回归测试。

---

## 4. Argus 当前测试基线与质量门禁

本仓库当前本地质量入口：

```bash
make validate
```

`scripts/validate.sh` 当前执行六类验证：

1. 解析 `docker-compose.yml`、`config/agents.yaml`、`data/dataset.json`。
2. 编译 Python 源码。
3. 验证 Demo Agent 不包含 Langfuse / Evaluation SDK 依赖。
4. 验证 Runner 包含 Remote Experiment 与 W3C propagation。
5. 执行全部 `pytest`。
6. 环境具备 Docker 时执行 `docker compose config -q`。

常用命令：

```bash
# 快速本地回归
.venv/bin/python -m pytest -q tests

# 只跑目标测试（TDD 循环时优先）
.venv/bin/python -m pytest -q tests/test_registry.py

# 最终完整质量门禁
make validate

# 完整 PoC
make up
make demo
make down
```

### 当前演示回归基线

- Agent v1：故意保留缺陷，预期 `2 / 6 ≈ 33.3%`。
- Agent v2：修复对应问题，预期 `6 / 6 = 100%`。
- 该基线由 `tests/test_expected_pass_rate.py` 保护。
- 修改 Demo 行为时，如果确实需要改变基线，必须同时说明为什么业务预期发生变化；禁止为了通过测试直接修改数字。

---

## 5. 代码与架构修改指导

### 5.1 Eval Runner

`services/eval-runner/` 是 Argus 执行控制面的核心。

修改时优先保证：

- 请求映射与 Agent 业务协议解耦。
- Retry / Timeout / Rate Limit 行为可测试。
- W3C `traceparent` 始终正确传播。
- Execution Failure 与 Evaluation Failure 分离。
- 同一个 DatasetItem 的 Retry 不得创建多个逻辑 Experiment Item。
- 幂等、恢复、取消等状态必须具有明确状态机，而不是散落布尔字段。

### 5.2 Agent Registry

`config/agents.yaml` 当前是 PoC SSOT，后续会演进到持久化 Registry。

规则：

- Endpoint、Request Mapping、执行策略属于 AgentVersion，而不是 Dataset。
- Secret 不得直接进入仓库配置；生产设计只保存 `credentialRef`。
- AgentVersion SHOULD 视为不可变快照；配置变化应创建新 version/revision。

### 5.3 Evaluator

`services/eval-runner/app/evaluators.py` 当前以确定性 Evaluator 为主。

规则：

- 能用确定性规则解决的问题，不优先引入 LLM Judge。
- Evaluator 输出必须可解释。
- Evaluator 的输入、输出和阈值应显式定义。
- 后续 LLM Judge 必须固定 Prompt Version 与 Model Version。
- Tool/Trajectory 评测应基于标准 Trajectory，而不是直接耦合某个 Agent Framework。

### 5.4 Demo Agent

`services/demo-agent/` 是被测业务系统样例，不是评测平台的一部分。

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

## 6. 事实、证据与问题分析规范

结论必须区分以下四类证据：

- **源码事实**：当前代码、配置、数据模型直接证明。
- **测试事实**：由实际执行测试命令得到。
- **运行事实**：由真实服务、容器、Langfuse UI、HTTP 调用得到。
- **推断/设计判断**：基于证据提出的方案或推测，必须明确是判断而非事实。

禁止：

- 没运行测试却说“测试已通过”。
- 没启动 Docker 却说“端到端已验证”。
- 看到函数名就推断行为，不阅读实现。
- 用 README 描述替代对当前代码的验证。
- 把未来 Roadmap 能力描述为当前已具备能力。

---

## 7. Git / Issue / PR 工作规范

- 如果任务来自 GitHub Issue，Issue 正文中的验收标准是实现和测试的输入。
- Issue 评论中的设计方案视为当前设计上下文；实现若需要偏离，应解释原因。
- 每个 Commit SHOULD 保持单一目的。
- Commit 使用 Conventional Commits：
  - `feat:` 新功能
  - `fix:` Bug 修复
  - `test:` 测试新增或调整
  - `refactor:` 不改变外部行为的重构
  - `docs:` 文档
  - `chore:` 构建、依赖、工具配置
- 禁止为了“干净”而修改与任务无关的文件。
- 禁止覆盖用户已有未提交修改。
- PR 描述 SHOULD 包含：问题、方案、测试证据、兼容性影响、风险。

---

## 8. 安全与敏感数据规范

- 禁止提交真实 Token、密码、Cookie、API Key、数据库凭证。
- `.env.poc` 仅允许演示凭据，任何生产凭证不得写入仓库。
- 日志、Trace、测试 Fixture 中不得无意保存真实 PII。
- Authorization、Cookie 等敏感 Header 默认不得写入 Langfuse Trace。
- 新增外部调用时，应考虑 timeout、重试边界、SSRF、证书校验和 Secret 获取方式。
- 涉及生产 Agent Endpoint 的设计，优先内部网络、mTLS、Network Policy 和 Vault/Secret Manager。

---

## 9. Definition of Done

一个功能只有同时满足以下条件才算完成：

- [ ] 已阅读相关 Issue、设计评论和现有实现。
- [ ] 已把验收标准转换为测试或明确验证项。
- [ ] 功能/修复遵循 RED → GREEN → REFACTOR；若例外已说明原因。
- [ ] 新增行为有自动化测试保护。
- [ ] Bug 修复有回归测试。
- [ ] 目标测试通过。
- [ ] `make validate` 通过；若某一步因环境不可用被跳过，已明确说明。
- [ ] 未破坏零 Evaluation SDK、W3C Trace、版本可复现等架构铁律。
- [ ] 未引入明文 Secret 或不必要的敏感数据。
- [ ] Diff 中没有与任务无关的修改。
- [ ] 文档/API/配置在需要时同步更新。
- [ ] 交付说明包含实际验证命令和结果，而不是笼统写“已测试”。

---

## 10. Agent 最终交付模板

完成任务后，建议按下面格式汇报：

```text
完成内容
- ...

TDD / 测试
- RED：新增 xxx 测试，修改前因 xxx 失败
- GREEN：实现 xxx 后通过
- REFACTOR：整理 xxx，行为未改变

验证
- pytest ... -> PASS
- make validate -> PASS
- E2E -> PASS / 未执行（原因）

影响与风险
- ...

后续建议
- ...
```

最终目标不是“让代码看起来正确”，而是让每一次变更都有 **失败用例作为起点、自动化测试作为证据、清晰架构边界作为约束、可复现验证作为终点**。
