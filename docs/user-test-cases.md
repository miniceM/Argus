# Argus 平台用户测试案例设计手册 (User Acceptance Test Cases)

> **版本**：v1.0  
> **适用平台**：Argus 企业级 AI Agent 评测与质量门禁平台  
> **基准版本**：`codex/s4-0-observation-link` (基于 main 最新基线)  
> **测试环境**：
> - Console 前端：`http://localhost:18083`
> - Eval Runner 控制面：`http://localhost:18080`
> - Demo Agent v1：`http://localhost:18081`（缺陷版本，基准通过率 2/6）
> - Demo Agent v2：`http://localhost:18082`（修复版本，基准通过率 6/6）
> - 预置评测数据集：`banking-agent-regression`（6 条典型用例）

---

## 1. 测试设计总览与架构原则

Argus 平台遵循“**业务 Agent 零 Evaluation SDK 侵入**”与“**四维版本严格复现**”两大核心原则：

```text
[Dataset Version] ──┐
[Agent Version]   ──┼──> [4D Manifest 冻结] ──> [异步执行调度] ──> [用例与链路诊断] ──> [Release Gate 门禁]
[Evaluator Vers]  ──┤
[Runner Build]    ──┘
```

本测试案例手册围绕终端用户（评测工程师、Prompt 工程师、质量保障人员、发布负责人）的核心使用场景，划分为 6 大测试模块、共 18 个测试用例：

| 模块编号 | 模块名称 | 用例数量 | 核心验证价值 |
|---|---|:---:|---|
| **TC-01** | **Agent 资产与版本治理** | 3 | 验证 Agent 元数据可见性、版本配置不可变性、凭据零明文合规 |
| **TC-02** | **评测任务创建与 4D 快照固化** | 3 | 验证四维版本选择、自定义质量策略、Manifest 与 Digest 防篡改 |
| **TC-03** | **任务调度与状态机控制闭环** | 4 | 验证异步并发排队、中断取消、断点恢复、失败定向重跑等状态机完整性 |
| **TC-04** | **用例级诊断与可观测链路** | 3 | 验证多维 Evaluator 诊断、Attempt 重试抽屉耗时审计、W3C 链路贯通 |
| **TC-05** | **版本对比与 Release Gate 门禁** | 3 | 验证 Baseline 绑定、候选版本三层对比（指标/用例/门禁）、放行与阻断判定 |
| **TC-06** | **异常容错与高可用边界** | 2 | 验证非幂等防重放、并发原子锁防重跑、容器重启后台积压自愈 |

---

## 2. 测试用例详情

### TC-01: Agent 资产与版本治理

#### TC-01-01: 浏览已注册 Agent 列表与基础元数据核验
- **测试目的**：验证控制台能准确拉取并展示通过配置或 API 注册的 Agent 资产元数据。
- **前置条件**：系统正常启动，已预置 `banking-agent`。
- **操作步骤**：
  1. 打开浏览器访问 `http://localhost:18083/agents`。
  2. 观察 Agent 列表页面中卡片或表格信息。
- **预期结果**：
  - 列表中展示 `banking-agent`（名称：“银行客服 Agent”）；
  - 描述清晰显示“演示用银行客服 Agent；v1 包含故意保留的缺陷，v2 修复这些缺陷”；
  - 版本数显示为 2，最新版本为 `v2`，状态为 `ACTIVE`；
  - 页面提供“发起评测”或进入版本详情的入口。
- **API 辅助验证**：
  ```bash
  curl -s http://localhost:18080/api/v1/agents
  ```

---

#### TC-01-02: 查看 Agent 不可变版本配置与请求映射
- **测试目的**：验证 AgentVersion 作为不可变配置快照，能够完整展示 Endpoint、超时、重试、限流及 Request Mapping。
- **前置条件**：在 Agent 列表点击 `banking-agent` 进入详情页。
- **操作步骤**：
  1. 访问 `http://localhost:18083/agents/banking-agent`。
  2. 查看版本切换区，分别核验 `v1` 和 `v2`。
- **预期结果**：
  - `v1` 目标端点显示为 `http://demo-agent-v1:8080/invoke`；
  - `v2` 目标端点显示为 `http://demo-agent-v2:8080/invoke`；
  - 包含执行策略：超时时间 `10s`，最大重试次数 `2`，频控 `600/min`；
  - 包含请求映射（Request Mapping）：
    - `messages: input.messages`
    - `customer_id: input.customer_id`
  - 每个版本生成唯一的配置指纹 `spec_digest`（不可变更）。

---

#### TC-01-03: Agent 凭据引用治理与敏感信息脱敏（零明文 Secret）
- **测试目的**：验证系统不落盘或暴露任何明文密码/API Key，仅使用安全的 `credentialRef`。
- **前置条件**：访问 `http://localhost:18083/credentials` 或查看 Agent 详情。
- **操作步骤**：
  1. 检查 Agent 版本详情页中的凭据字段；
  2. 检查网络请求中返回的 Agent JSON 数据。
- **预期结果**：
  - 凭据显示为引用标识（如 `env://OPENAI_API_KEY` 或留空），不展示任何明文密钥；
  - API 响应中仅包含 `credential_ref`，不含密码字样；
  - 尝试提交包含明文 URL 参数（如 `http://user:pass@host/api`）的非法注册会被后端强制拦截拒绝。

---

### TC-02: 评测任务创建与 4D 快照固化

#### TC-02-01: 创建标准评测任务并固化四维快照
- **测试目的**：验证在控制台能够完整选择四维版本要素并创建任务，生成唯一的冻结快照。
- **前置条件**：服务正常运行。
- **操作步骤**：
  1. 访问 `http://localhost:18083/launches/new`。
  2. 填写任务名称：`UAT-测试任务-01`。
  3. 选择 Agent：`banking-agent`，选择版本：`v2`。
  4. 选择评测集：`banking-agent-regression`。
  5. 评测指标多选：勾选 `intent_match`、`required_tool_match`、`pii_safe`、`escalation_match`，版本确认均为 `1.0.0`。
  6. 并发数保持默认 `4`，点击“创建评测任务”按钮。
- **预期结果**：
  - 提交成功并自动跳转至任务详情页 `http://localhost:18083/launches/{launch_id}`；
  - 任务状态初始为 `PENDING`，进度显示为 `0/6 (0%)`；
  - 切换至“清单与审计 (ManifestAuditTab)”标签页，可查验四维冻结版本：
    - Dataset Name & Version Hash
    - Agent ID & Version & `spec_digest`
    - Evaluator IDs & Versions & `binding_digest`
    - Runner Version & `build_id`

---

#### TC-02-02: 配置自定义质量判定规则 (Quality Policy)
- **测试目的**：验证创建任务时能够自定义规则（阈值、方向、是否必须），不合法规则在前端与后端双重拦截。
- **前置条件**：处于新建评测页 `http://localhost:18083/launches/new`。
- **操作步骤**：
  1. 展开“质量判定规则 (Quality Policy)”高级配置区域；
  2. 调整规则：将 `intent_match` 阈值设定为 `>= 1.0`，`required=true`；
  3. 尝试输入非法配置（如空 Evaluator 或越界阈值）并观察校验；
  4. 恢复合法设置后提交创建。
- **预期结果**：
  - 非法配置被前端表单即时阻断，提示“质量判定规则不合法”；
  - 合法配置提交后，在详情页 Manifest 中生成固化的 `policy_digest`，规则作为不可变契约执行。

---

#### TC-02-03: 幂等性防护测试 (Idempotency Key)
- **测试目的**：验证使用相同 `Idempotency-Key` 请求时，系统保证不产生重复创建。
- **前置条件**：使用终端执行 API 调用。
- **操作步骤**：
  1. 执行以下 curl 命令（带特定 Idempotency-Key）：
     ```bash
     curl -i -X POST http://localhost:18080/api/v1/experiment-launches \
       -H "Content-Type: application/json" \
       -H "Idempotency-Key: uat-idem-key-001" \
       -d '{
         "agent_id": "banking-agent",
         "agent_version": "v2",
         "dataset_name": "banking-agent-regression"
       }'
     ```
  2. 立即再次执行完全相同的命令；
  3. 修改请求体中的参数（例如改 agent_version 为 "v1"），保持相同的 Idempotency-Key 再次执行。
- **预期结果**：
  - 第 1 次请求：返回 `HTTP 201 Created`，返回新 Launch ID；
  - 第 2 次请求：返回 `HTTP 200 OK` 或 `201`，返回同一个 Launch ID（幂等命中，不重复建单）；
  - 第 3 次请求（修改了 Payload）：返回 `HTTP 409 Conflict`，明确拒绝同 Key 异体请求。

---

### TC-03: 任务调度与状态机控制闭环

#### TC-03-01: 任务异步排队与调度执行流转
- **测试目的**：验证评测任务触发后，从 `PENDING` -> `QUEUED` -> `RUNNING` -> `COMPLETED` 的状态机流转。
- **前置条件**：已创建处于 `PENDING` 状态的评测任务。
- **操作步骤**：
  1. 在控制台任务详情页点击右上角操作按钮“启动评测 (Run)”；
  2. 观察任务首屏 KPI 状态徽标与用例进度条变化。
- **预期结果**：
  - 任务状态秒级变更为 `QUEUED`，随后转入 `RUNNING`；
  - 进度条动态递增（1/6 -> 3/6 -> 6/6）；
  - 约 2~4 秒后，任务进入终态 `COMPLETED`；
  - 成功用例数显示为 6，失败数为 0，通过率达到 100%。

---

#### TC-03-02: 运行中任务中断与取消 (Cancel 操作)
- **测试目的**：验证评测任务在并发执行过程中支持安全取消。
- **前置条件**：
  - 新建一个评测任务，可通过将并发限制设为 `1` 并设置较大超时，或通过命令行向任务投递执行。
- **操作步骤**：
  1. 触发任务运行；
  2. 在任务处于 `RUNNING` 期间迅速点击操作菜单中的“取消评测 (Cancel)”。
- **预期结果**：
  - 系统记录 `cancel_requested_at`；
  - 尚未被 Worker 消费的用例状态置为 `CANCELLED`；
  - 任务终态转为 `CANCELLED`，首屏徽标标红/标灰，停止后续排队。
- **API 验证**：
  ```bash
  curl -X POST http://localhost:18080/api/v1/experiment-launches/{launch_id}/cancel
  ```

---

#### TC-03-03: 中断任务断点恢复 (Resume 操作)
- **测试目的**：验证已取消（CANCELLED）的任务能够基于断点无损恢复，只执行未完成的用例。
- **前置条件**：存在一个部分用例已完成、部分用例被 CANCELLED 的任务。
- **操作步骤**：
  1. 访问已取消任务的详情页；
  2. 点击操作菜单中的“恢复执行 (Resume)”。
- **预期结果**：
  - 任务状态重新由 `CANCELLED` 变为 `RUNNING`；
  - 已完成的用例保持结果不变，不发生重复调用；
  - 处于 CANCELLED 的用例被重新放入就绪队列并执行完毕；
  - 最终任务整体流转至 `COMPLETED`。

---

#### TC-03-04: 针对失败用例的定向重跑 (Retry Failed)
- **测试目的**：验证只对失败（FAILED）或超时（TIMED_OUT）的用例进行定向重新调度，不浪费成功用例资源。
- **前置条件**：对 Agent v1 发起评测（已知会有 4 个失败用例）。
- **操作步骤**：
  1. 查看完成状态为 COMPLETED、但通过率为 2/6 的 Agent v1 评测任务；
  2. 点击操作菜单中的“重试失败用例 (Retry Failed)”。
- **预期结果**：
  - 系统仅筛选失败的 4 个用例重新排队进入 `QUEUED`；
  - 2 个成功的用例保持原结果不动；
  - 任务状态重新流转并更新结果快照 Revision（如 `revision: 2`）。

---

### TC-04: 用例级诊断与可观测链路

#### TC-04-01: 用例明细查看与多维 Evaluator 得分诊断
- **测试目的**：验证用例列表能够清晰展示输入、预期、实际输出以及每一项打分指标的判定。
- **前置条件**：打开任意已完成评测任务的详情页。
- **操作步骤**：
  1. 切换到“用例与链路 (CasesTraceTab)”标签页；
  2. 查看用例表格，点击第一条用例（`00000000-0000-4000-8000-000000000001`）展开详情。
- **预期结果**：
  - 列表清晰展示用例 ID、耗时、执行状态（`SUCCEEDED`）、综合通过状态；
  - 展开卡片中包含：
    - 输入消息：`[{"role":"user","content":"请帮我查询昨天账户中的异常交易"}]`
    - 预期输出：`intent: transaction_investigation, required_tool: transaction-query`
    - 实际输出：Agent 返回的真实 JSON 报文
  - Evaluator 得分矩阵：
    - `intent_match`: `1.0` (PASS)
    - `required_tool_match`: `1.0` (PASS)
    - `pii_safe`: `1.0` (PASS)
    - `escalation_match`: `1.0` (PASS)

---

#### TC-04-02: 执行尝试 (Attempt) 抽屉与重试耗时审计
- **测试目的**：验证每次与业务 Agent 的真实网络调用均记录有独立的 Execution Attempt 记录。
- **前置条件**：在用例表格中点击任意用例行的“查看尝试记录 (Attempts)”按钮。
- **操作步骤**：
  1. 打开 Attempt 抽屉面板；
  2. 观察尝试记录列表及报文信息。
- **预期结果**：
  - 抽屉展示该用例的调用序号（`Attempt #1`）；
  - 记录确切的调用开始时间、耗时（毫秒）、HTTP 状态码（`200`）；
  - 展示实际发送给 Demo Agent 的完整请求体；
  - 若发生重试，可查看不同 Attempt 的递增序号与错误分类（如 `READ_TIMEOUT`、`500_SERVER_ERROR`），证实“重试只增加 Attempt，不重复生成逻辑 Item”。

---

#### TC-04-03: W3C Trace Context 注入与 Langfuse 观测平台跳转
- **测试目的**：验证 Runner 在调用业务 Agent 时规范注入了 `traceparent`，并支持向 Langfuse 链路平台下钻。
- **前置条件**：打开用例详情或 Attempt 抽屉。
- **操作步骤**：
  1. 检查 Attempt 抽屉中的请求 Header 列表；
  2. 寻找 `traceparent` 字段；
  3. 观察右上角或表格中的“查看 Langfuse Trace”外部链接按钮。
- **预期结果**：
  - Header 中严格包含符合 W3C 标准的 `traceparent: 00-{trace_id}-{span_id}-01`；
  - 业务 Agent 无需引入评测 SDK 即可直接利用该 Header 串联链路；
  - 点击“查看 Langfuse Trace”可正确携带 Trace ID 跳转到对应可观测详情页。

---

### TC-05: 版本对比与 Release Gate 门禁

#### TC-05-01: 设定评测基线 (Set Baseline)
- **测试目的**：验证可将某个已完成的历史评测版本标记为当前 Agent 的黄金基准线（Baseline）。
- **前置条件**：已完成一轮针对 Agent v1 的评测（Launch A，通过率 2/6）。
- **操作步骤**：
  1. 访问 Launch A 详情页；
  2. 点击操作菜单中的“设为基线 (Set Baseline)”；
  3. 确认设为当前版本的 Baseline Snapshot。
- **预期结果**：
  - 操作成功，页面弹出成功反馈；
  - 该评测快照被标记为该 Agent 的基准线，供后续候选版本（Candidate）自动关联比对。

---

#### TC-05-02: 发起候选版本评测并进行基线对比 (Comparison Report)
- **测试目的**：验证候选版本（Candidate）能够与基线版本（Baseline）进行逐 Case 及聚合指标的三层对比。
- **前置条件**：已设定 Launch A 为基线；完成一轮针对 Agent v2 的评测（Launch B，通过率 6/6）。
- **操作步骤**：
  1. 访问 Launch B 详情页；
  2. 切换至“门禁与对比 (GateComparisonTab)”标签页；
  3. 确保当前比对的基准为 Launch A 的结果快照。
- **预期结果**：
  - 首屏展示质量门禁判定结果；
  - 对比概览展示：
    - 基线通过率：`33.3% (2/6)`
    - 候选版本通过率：`100.0% (6/6)`
    - 提升幅度（Delta）：`+66.7%`
  - 用例回归与改善状态清晰分组：
    - **改善用例 (Improved)**：`4` 条（原先在 v1 失败、但在 v2 成功的用例）
    - **退化用例 (Regressed)**：`0` 条
    - **保持一致 (Unchanged)**：`2` 条
  - 点击改善用例可打开 `ComparisonCaseDrawer`，双栏对比展示 Baseline 与 Candidate 的实际输出差异与指标跳变原因。

---

#### TC-05-03: Release Gate 发布门禁放行与阻断决策判定
- **测试目的**：验证 Release Gate 根据质量策略严格执行未知阻断、关键指标一票否决以及发布准入。
- **前置条件**：分别查看 Launch A 与 Launch B 的 Gate 判定。
- **操作步骤**：
  1. 查看 Launch A（Agent v1）的 Gate 决策卡片；
  2. 查看 Launch B（Agent v2）的 Gate 决策卡片。
- **预期结果**：
  - **Launch A (Agent v1)**：
    - 判定状态：`BLOCKED`（阻断发布）；
    - 原因说明：存在关键指标 `pii_safe` 未通过或综合通过率未达阈值（4 项规则失败）；
  - **Launch B (Agent v2)**：
    - 判定状态：`RELEASE_ELIGIBLE`（具备发布资格）；
    - 原因说明：所有必要规则全部通过，无退化用例，各项指标达到发布阈值。

---

### TC-06: 异常容错与高可用边界

#### TC-06-01: Agent 远程调用异常时的错误分类与状态解耦
- **测试目的**：验证当 Agent 发生网络超时或 5xx 错误时，系统正确解耦“执行失败”与“评测质量失败”。
- **前置条件**：临时停止 `demo-agent-v1` 容器或配置一个不可达的目标地址。
- **操作步骤**：
  1. 发起一次指向不可达端点的评测任务；
  2. 观察任务执行结果与用例状态。
- **预期结果**：
  - 用例状态标记为 `FAILED`，错误分类明确为 `CONNECT_ERROR` 或 `TIMEOUT`；
  - 评测判定显示为 `UNKNOWN` 或 `NOT_EVALUATED`，而不是错误地评定为“业务意图不匹配”；
  - 确认实现了“技术执行失败与业务质量评估分离”。

---

#### TC-06-02: 容器重启后台积压自愈 (Reconciler 巡检保障)
- **测试目的**：验证当评测服务异常重启导致内存队列丢失时，Reconciler 能够自动扫描超过 30 秒的积压任务并重新投递消费。
- **前置条件**：服务正常运行。
- **操作步骤**：
  1. 创建一个新的评测任务并触发执行；
  2. 在 Worker 刚开始执行时，重启 `eval-runner` 容器：
     ```bash
     docker compose -f docker-compose.cloud.yml restart eval-runner
     ```
  3. 观察容器启动后的日志及该任务的最终状态。
- **预期结果**：
  - 容器重启后，`_reconciler_loop` 巡检协程自动启动；
  - 巡检器检测到遗留的活跃任务，执行 `reconcile_backlog()` 将卡在 `QUEUED` 的条目重新排队；
  - 任务无需人工干预即可在数秒后自动恢复执行并达到终态 `COMPLETED`。

---

## 3. 测试执行指南与验收记录模板

### 3.1 快速执行回归测试基线 (Smoke Test)
在正式手工执行 UAT 之前，建议先运行平台的自动化一键回归：

```bash
# 1. 运行本地 8 步硬质量门禁
make validate

# 2. 验证容器服务健康状态
docker compose -f docker-compose.cloud.yml ps

# 3. 运行端到端 Demo 回归
python3 scripts/run_demo_regression.py  # 或通过 Console UI 操作
```

### 3.2 用户验收测试记录表 (UAT Sign-off Template)

| 用例编号 | 用例名称 | 执行人 | 执行结果 | 缺陷/备注 |
|---|---|---|:---:|---|
| TC-01-01 | 浏览已注册 Agent 列表与元数据核验 | | [ ] PASS / [ ] FAIL | |
| TC-01-02 | 查看 Agent 不可变版本配置与请求映射 | | [ ] PASS / [ ] FAIL | |
| TC-01-03 | Agent 凭据引用治理与脱敏 | | [ ] PASS / [ ] FAIL | |
| TC-02-01 | 创建标准评测任务并固化四维快照 | | [ ] PASS / [ ] FAIL | |
| TC-02-02 | 配置自定义质量判定规则 (Quality Policy) | | [ ] PASS / [ ] FAIL | |
| TC-02-03 | 幂等性防护测试 (Idempotency Key) | | [ ] PASS / [ ] FAIL | |
| TC-03-01 | 任务异步排队与调度执行流转 | | [ ] PASS / [ ] FAIL | |
| TC-03-02 | 运行中任务中断与取消 (Cancel) | | [ ] PASS / [ ] FAIL | |
| TC-03-03 | 中断任务断点恢复 (Resume) | | [ ] PASS / [ ] FAIL | |
| TC-03-04 | 针对失败用例的定向重跑 (Retry Failed) | | [ ] PASS / [ ] FAIL | |
| TC-04-01 | 用例明细查看与多维 Evaluator 得分诊断 | | [ ] PASS / [ ] FAIL | |
| TC-04-02 | 执行尝试 (Attempt) 抽屉与耗时审计 | | [ ] PASS / [ ] FAIL | |
| TC-04-03 | W3C Trace Context 注入与 Langfuse 跳转 | | [ ] PASS / [ ] FAIL | |
| TC-05-01 | 设定评测基线 (Set Baseline) | | [ ] PASS / [ ] FAIL | |
| TC-05-02 | 候选版本评测与基线对比 (Comparison Report) | | [ ] PASS / [ ] FAIL | |
| TC-05-03 | Release Gate 发布门禁放行与阻断判定 | | [ ] PASS / [ ] FAIL | |
| TC-06-01 | 远程调用异常时的错误分类与状态解耦 | | [ ] PASS / [ ] FAIL | |
| TC-06-02 | 容器重启后台积压自愈 (Reconciler 巡检) | | [ ] PASS / [ ] FAIL | |
