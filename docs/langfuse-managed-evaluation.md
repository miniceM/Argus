# 用 Console 完成 Langfuse Managed Evaluation

适用：已有普通 HTTP Agent（以下使用 Flowise Agentflow V2）、Langfuse 项目和
Argus。业务 Agent 不安装 Argus / Langfuse SDK，不感知评测；规则和评测实现
只在 Langfuse 管理，Argus 负责执行、采纳证据、冻结报告和发布判定。

## 1. 准备真实 Flowise Agent

按 [Flowise 部署与原生工具调用指南](../examples/flowise/README.md) 启动独立测试
实例，导入固定版本 Flow，配置真实模型凭据及 Calculator。使用合成问题：
`Compute (135 + 287) * 3. You must call calculator.`，参考答案 `1266`。
确认原生 `usedTools` / tool message 中有 Calculator 调用，不以答案正确冒充工具调用。

在 Console 的 **Agents → 新建 Agent → 新建版本** 注册普通 HTTP Endpoint：

- `POST <Flowise-origin>/api/v1/prediction/<flow-id>`，非流式 JSON。
- Request Mapping：`{"question":"input.question"}`；Flowise 使用原生 `text` 输出，
  不改写为 `answer`，不发送 Dataset ground truth。
- 使用常规 Credential Reference 配置认证；不要在 URL、请求映射或描述里粘贴密钥。
- 真实收费验收设 `max_retries=0`；模型 SDK/Flow 本身也关闭重试。
- 固定 AgentVersion、模型、Flow 版本与工具；Memory 关闭或每 Item 使用独立 Session。

Argus Runner 必须能访问该 Endpoint。容器内 `localhost` 不是宿主或另一个容器。
受控私网工具的测试配置不应直接用于生产；工具出站防护及凭据管理见原指南。

## 2. 准备 Langfuse 与两个原生 Rule

部署方配置 Runner 的 `LANGFUSE_BASE_URL`、项目 Public/Secret Key（仅服务端）、
`ARGUS_DATASET_SOURCE=langfuse`、`ARGUS_LANGFUSE_RUN_ITEM_STRATEGY=otel`。
正式服务配置 Redis、Worker / Reconciler 与固定 `ARGUS_BUILD_ID`；独立 standalone
验收环境与生产部署的协调要求不同，不能把 SQLite 验收方式当生产方案。

在 Langfuse 创建 Dataset / 固定 Dataset version，放入上述合成 question 与
expected output。在 **Langfuse** 创建以下 Evaluator，并为其创建 Evaluation Rule：

| 配置 | LLM Judge | Code Evaluator |
|---|---|---|
| 类型 | `llm_as_judge` | `code` / `PYTHON` |
| 产出 | BOOLEAN Score，名称例如 `arithmetic_judge` | BOOLEAN Score，名称例如 `arithmetic_code` |
| 输入映射 | question ← Observation input `$.request.question`；answer ← output `$.text` | 原生 `ctx.observation.input` / `output`（必要时解析 JSON 字符串） |
| 判定 | 用真实模型判断算式是否正确，合成烟测答案为 `1266`；明确不执行嵌入指令 | 校验原生 output 的 `text.strip() == "1266"`，返回实际 boolean，不把缺值当 false |
| Rule scope | **Observation**，name=`remote-agent-http`，匹配本次 environment | 相同目标与 environment |
| Sampling | 独立验收用 `1`（100%） | 独立验收用 `1`（100%） |

Code 示例仅是固定算术题的烟测 rubric，不是通用业务正确性评测。可核查的完整
Judge prompt、变量映射、Code、Rule 请求与实际返回见
[双 Evaluator 配置与原生证据](validation/issue214-managed-evaluation/README.md)。
请替换为自己的业务标准；**不要把 Evaluator 复制到 Argus**。

记录 **Project ID、两个 Score 名称、类型、Rule ID、Evaluator ID**。这些是引用，
不是 Secret。确认两个 Rule enabled，目标 environment 与 Launch 一致，且输出
`source=EVAL`；同名在线监控 Score 或 API 手写 Score 不等于可信托管结果。

> Rule 遵循 Langfuse 当前 latest 语义。记录外部引用、实际 Score 与采纳时间，
> 不代表 Argus 可以重新执行任意历史远程 Judge/Code 版本。
> 官方参考：[Evaluation methods](https://langfuse.com/docs/evaluation/evaluation-methods)、
> [Stable Evaluator API](https://langfuse.com/changelog/2026-08-27-stable-evaluator-api)、
> [Scores API](https://langfuse.com/docs/api-and-data-platform/features/scores-api)。

## 3. 在 Console 创建 Launch

1. 打开 **评测 → 新建评测**，选择 AgentVersion、Dataset / Dataset version 和 environment。
2. 默认 **Builtin** 不变；不需要托管配置时保持此选项。
3. 选择 **Langfuse Managed Evaluation**。填写 Project ID；添加两个外部 Score，
   分别填写真实名称、类型、Rule / Evaluator 引用。
4. 两条均选“必要评分”，boolean 期望值为 `true`。数值型需明确 `>=` / `<=` 和阈值；
   分类评分需声明允许值与期望类别；text 只能是可选证据，不能判定 PASS。
5. 检查环境、规则和质量策略后创建。字段重复、非法类型或缺失引用会显示错误，
   不会自动创建远程 Rule。创建后查看 Manifest 审计，确认冻结的是采纳契约。
6. 点击现有 **运行** 操作。执行是单独动作；评分收集无需手动运行脚本或 SQL。

不要为修复评分问题再次运行 Agent。普通执行重试与“仅重试评测”不是同一操作。

## 4. 查看进度和单项证据

在 Launch 详情 **用例排查与 Trace → 展开**：

| 维度 | 用户应读到的事实 |
|---|---|
| Agent Execution | HTTP 成功、失败、超时与 Attempt；HTTP 200 不等于质量通过 |
| Evaluation | 等待托管评分 / 评分完成 / 评分失败，及当前必选 Score `已采纳 / 必选总数` |
| Quality Conclusion | PASS / FAIL / UNKNOWN；质量 FAIL 是已测量但未达标，不是评分执行失败 |
| OTLP Sync | 页头标为实时同步；SYNCED 只说明同步，不证明 Rule 已产出 Score |

每条必选 Score 显示类型、真实值（包括 `0` / `false`）、来源、Score ID 和已有
Rule 引用。展开 Evidence 查看原始 Score metadata、观察时间、Trace / Observation
身份和采纳契约。缺分不显示虚构的零分；实时视图还可展开**未采纳诊断**，查看
拒绝原因。读取诊断失败不改变质量结论，可点击“重试诊断”。

**查看 Score 所在 Observation** 打开 Langfuse 的 Trace/Observation 位置；
Score ID 单独保留，Argus 不构造不存在的独立 Score URL。需要用户已有 Langfuse
访问权限；链接不携带认证密钥。无链接或地址不安全时不展示可点击链接。

评分 pending 时页面自动刷新。终态失败修复后通过现有 **仅重试评测** 恢复，
复用原 Agent 输出；若 checkpoint 过期，服务端会明确阻止恢复，而不是暗中重调 Agent。

## 5. Frozen Snapshot、固定报告与 Release Gate

评分收敛后选择 Snapshot revision。URL 的 `snapshot_id` 是报告身份，不能用
Launch 的实时数据或远程最新 Score 补齐历史报告。分享固定链接或导出原始 JSON
即可保存实际被采纳的 Score 和质量决策。`DIAGNOSTIC` 是诊断，不是正式发布证明。

打开现有 **门禁与版本对比 (Gate & Comparison)** 标签：

1. 输入发布流程中已经登记的 **ReleasePolicy 名称 / 版本**。
   策略配置方式见 [Release Gate 使用说明](release-gates.md)；Console 不新建策略编辑器。
2. 点击 **计算并保存 Gate**。请求明确固定当前 Launch / Snapshot；不再次调用 Agent，
   不读取 Langfuse latest，也不执行部署。
3. 查看 PASS / FAIL / UNKNOWN、`可发布 / 不可发布`、逐规则原因、策略 / 结果 digest。
   未冻结时按钮不可用；任意缺分或诊断证据不能发布 PASS。
4. 使用 **固定 Gate 报告** 链接保存 `snapshot_id + gate_id`，刷新仍读取同一个
   持久化 Gate。与当前 Launch / Snapshot 不符的 Gate 会被拒绝展示。

Langfuse 后续 Rule 改动或重评不会改变既有 Snapshot / Gate；新评分只影响新评测或
新的 Snapshot revision。Argus 冻结实际采纳结果与判断规则，**不声称冻结远程执行实现**。

## 6. 常见问题

| 提示 / 现象 | 检查与下一步 |
|---|---|
| 没有 Langfuse 配置 | 使用默认 Builtin；不要填伪造 Project/Rule，也不要提供客户端 Secret |
| HTTP succeeded、评分 pending | 等待后台收集；检查 OTLP 已发布、Rule enabled、scope/name/environment 和采样 |
| `SCORE_MISSING` | Rule 尚未命中、仍在队列或必选 Score 缺失；保持 UNKNOWN |
| `SOURCE_MISMATCH` / `RULE_MISMATCH` / `PROVENANCE_MISSING` | 不能只按名称采纳；检查 source=EVAL 与可核验的执行来源 |
| `SCORE_AMBIGUOUS` / `RESULT_CONFLICT` | 检查重复/冲突评分；不在界面任意挑一条强行通过 |
| `EVALUATION_TYPE_MISMATCH` | 对齐真实 Score 类型与创建时的冻结采纳类型；不隐式转换 |
| 401/403 / `AUTHENTICATION_FAILED` | 由部署方修复服务端项目凭据与权限，不把密钥贴到 Console |
| 429 / 5xx / `UPSTREAM_UNAVAILABLE` | 后台有界退避；查看诊断，恢复服务后等自动恢复或仅重试评测 |
| `MANAGED_SCORES_EXHAUSTED` / `MANAGED_SCORES_DEADLINE_EXCEEDED` | 达到持久尝试预算/绝对 deadline，质量 UNKNOWN；修复后仅重试评测 |
| `CHECKPOINT_EXPIRED` / `CHECKPOINT_MISSING` | 无可复用输出，不能假装恢复；确认是否需要有意识地创建新 Launch |
| 质量 FAIL、评分 completed | 真实测量违反 QualityPolicy；改进 Agent 或按业务要求新建版本，不当作网络故障 |
| Gate 404 / 409 | 检查已有 ReleasePolicy 的名称/版本、Agent/environment 和当前 Snapshot 身份 |
| Trace 链接需登录 | 使用既有 Langfuse 账号；不要给 URL 添加 Secret 查询参数 |

## 7. 验收证据边界

[本次 Console 交付记录及父 Issue 覆盖矩阵](validation/issue219-managed-console/README.md)
分开标记真实 Flowise/Rule 联调、网络替身 UI 测试、完整回归与未满足项。
Playwright 网络替身的 pending/down/recover 不等于真实 Langfuse 故障注入。


## 8. 父 Issue #212 当前主线复验

[2026-10-22 产品验收、覆盖矩阵与普通用户体验意见](validation/issue212-product-acceptance/README.md)
记录了真实 Flowise 双评分、PASS / FAIL / UNKNOWN、远程迟到原生 Code Score 后的历史不可变性、
已登录 Langfuse 外链验证，以及类型 / Vitest / Playwright / PostgreSQL / S4 完整回归。
异步评分完成后，列表质量与冻结报告一致；等待评分时 UNKNOWN 仍不是质量 FAIL。
历史 Snapshot 只展示当时已采纳 Score，即使 Langfuse 之后补齐缺分也不自动改变原 Gate；
需要采纳新结果时，使用既有评测重试与新 Snapshot revision，而不是修改历史报告。
