# Issue #215：Launch 级 Langfuse Managed Evaluation

## 1. 交付范围与证据边界

- Builtin 默认行为保留。只有新建 Launch 显式选中 Managed 才增加外部 Score 采纳契约。
- 本项提供**创建、严格校验、冻结、查询和展示**；不创建或编辑 Langfuse Rule/Evaluator，不执行 Judge/Code，不构建外部 Evaluator Catalog。
- Rule 始终使用 Langfuse 当前 latest Evaluator。冻结的是 Argus 的**采纳条件**，不是远程执行实现。
- **没有 Score 自动回收、异步 Worker/lease、外部 Score 入库/Gate 采纳或全链路评分 UI**：这些属于 #216～#219。
- 本文 UI/创建验收使用真实本地 Runner API 和隔离 SQLite，外部 project/Rule/Evaluator ID 是显式标明的测试引用，**不是新一轮真实 Langfuse 评分证据**；Worker 集成测试的 HTTP 响应是 mock。
- 前置真实链路见 [#214 的独立实测报告](../issue214-managed-evaluation/README.md)：已验证 `remote-agent-http` TOOL 与托管 Judge/Code Score。本项没有重做付费模型调用，也不把当前本地测试冒充该实测。

## 2. API Contract 与历史兼容

类型的唯一事实来源是 [OpenAPI](../../openapi.json)，前端类型由其生成。
入口仍为 `POST /api/v1/experiment-launches`，详情仍为
`GET /api/v1/experiment-launches/{launch_id}`；未新增平行 API 或数据库表。

可操作实例（来自本次 Playwright 的真实本地 API）：

- [创建请求](evidence/create-request.json)：显式 Managed；boolean `correctness` 与 numeric `accuracy`；两者分别配置期望 true 和阈值 0.8。
- [Frozen Manifest](evidence/frozen-manifest.json)：原有 schema 1.2 的可选扩展；`evaluators=[]`、外部引用、目标/source/latest 语义、采纳摘要与完整 QualityPolicy 分开保存。

### 服务器约束

1. 省略/显式 `builtin` 走原有路径，**不增加 Manifest 字段、改变默认 Evaluator 或幂等摘要**。历史 Manifest 缺少来源时仅在读取边界解释为 Builtin，不回填历史记录。
2. Managed 必须显式提供非空 project/environment、1～50 项 Score 和 QualityPolicy；不能混入 `evaluator_ids` 或 `evaluator_selections`，包括空列表。
3. Score 名称必须唯一、非空、无首尾空白且最长 128 字符。接受 numeric/boolean/categorical/text；text 仅可选证据，不能成为 QualityPolicy 判定规则。
4. 每项 Score 必须引用用户选择的已有 Rule ID；Evaluator ID 可选。分类取值必须非空、唯一，期望值必须属于该冻结枚举。
5. 目标限定为 #214 已实测的 `remote-agent-http / TOOL`，固定 source=EVAL，禁止 replay span 或其它未支持的目标。
6. 契约 environment 必须等于规范化后的 Launch environment。设置了 `LANGFUSE_TRACING_ENVIRONMENT` 时还必须与其匹配；设置了 `LANGFUSE_PROJECT_ID` 时，project 必须与该配置匹配。单项 Score 声明 project 时也必须匹配契约。
7. **这里校验引用格式和一致性，不发起外部 API 请求确认引用当前存在、Rule 已启用或远程实际版本**；项目的真实权限/存在性和未来 Score 归属不能由用户填入 ID 证明。未配置服务端 project/environment 时，不凭空推导其真实身份。界面/契约不声称已经得到或验证远程 Score。
8. 必选 Score 必须具有对应规则，规则类型和 required 与 Score 一致；未选指标、错误运算符、非有限数值、非法期望值、重复规则、无必要规则等由既有 QualityPolicy 验证拒绝。
9. 采纳条件深拷贝后写入 Manifest；幂等摘要包含外部契约。同 key 不同 Rule/条件返回冲突，前端编辑或后续 Langfuse 配置变化不会改写已保存 Manifest。

### 错误契约

语义错误为 HTTP 400 的 `detail={code,message,recovery}`；既有 QualityPolicy 错误保留其原有结构。
未知字段、缺失结构字段和非法 JSON 字段类型为 FastAPI/Pydantic 的 HTTP 422 字段错误。
界面复用既有错误格式化逻辑，显示对应可读原因，不静默回退 Builtin。

| 稳定错误码 | 条件 |
|---|---|
| `EVALUATION_SOURCE_INVALID` | 不支持的来源，包括显式空字符串 |
| `EVALUATION_SOURCE_CONFLICT` | Builtin 带外部配置，或 Managed 混入 Builtin 选择 |
| `MANAGED_CONTRACT_REQUIRED` | 缺少 Managed 契约 |
| `MANAGED_IDENTITY_REQUIRED` | project 为空或含首尾空白 |
| `MANAGED_PROJECT_MISMATCH` | Score/project/已配置的服务端 project 不一致 |
| `MANAGED_ENVIRONMENT_MISMATCH` | Launch/契约/已配置 tracing 环境不一致 |
| `MANAGED_TARGET_UNSUPPORTED` | 未支持的 Observation 目标 |
| `MANAGED_SCORE_NAME_INVALID` / `MANAGED_SCORE_DUPLICATE` | 空白/过长名称或重名 |
| `MANAGED_SCORE_TYPE_UNSUPPORTED` / `MANAGED_REQUIRED_TYPE_UNSUPPORTED` | 无效类型，或必选 text |
| `MANAGED_RULE_REFERENCE_REQUIRED` / `MANAGED_REFERENCE_INVALID` | 无效 Rule/Evaluator 引用 |
| `MANAGED_CATEGORIES_INVALID` | 分类枚举缺失/空白/重复 |
| `MANAGED_QUALITY_POLICY_REQUIRED` / `MANAGED_POLICY_MISMATCH` | 缺少策略或与 Score 契约不符 |
| `QUALITY_POLICY_*` | 复用既有策略规则错误码 |

## 3. 缺分时的安全边界

- 不改 Agent HTTP 执行、traceparent、重试或 Checkpoint 协议。
- 所有正常评分调用仍经过既有 `evaluate_frozen_item`；Managed 提前返回空 Score/typed 结果，并按冻结 QualityPolicy 得到 UNKNOWN。
- 汇总入口也拒绝将调用方提供的本地 typed 结果当作远程采纳证据。
- 暂时沿用现有 `eval_status=failed` 加诊断 `LANGFUSE_MANAGED_SCORES_PENDING`，**不增加 #217 的评测状态机**。HTTP `execution_status=succeeded` 与质量 UNKNOWN 独立；没有结果时绝不制造 0、PASS 或可发布结论。
- 测量摘要按已冻结采纳条件重算；QualityPolicy 阈值/运算符仍有独立摘要，不把它们解释为远程实现版本。

## 4. Console 可操作演示

1. 进入 `/launches/new`，确认来源默认 Builtin，原有四项诊断指标和 QualityPolicy 编辑器不变。
2. 切换为 **Langfuse Managed Evaluation**；界面显示“待收集评分，暂不可用于正式发布”与 latest 语义说明。
3. 填写既有 Langfuse Project ID、Score 名称、类型、Rule ID、可选 Evaluator ID。目标固定显示 `remote-agent-http / TOOL`，environment 来自上方 Launch 环境。
4. 添加另一项 Score，选择 numeric 并输入阈值；categorical 可输入允许枚举与期望值；text 自动成为可选证据。可增删 Score 或配置必要性。
5. 填入重名 Score，点击创建：服务器拒绝，界面显示具体原因，当前来源不切换、不丢弃输入。修正后重新提交。
6. 创建成功跳转详情，在“不可变快照与审计”读回采纳契约及 QualityPolicy；刷新后与创建响应一致。质量保持 UNKNOWN，不启动远程评分。

证据：

- [桌面来源选择与语义说明](evidence/creation-source-desktop.png)
- [桌面两种类型配置](evidence/creation-desktop.png)
- [服务端重名拒绝](evidence/creation-error-desktop.png)
- [390px 来源配置](evidence/creation-mobile.png)
- [Manifest 读回与 UNKNOWN](evidence/manifest-desktop.png)

Playwright 在 390px 验证 Managed 区**每个** input/select/button 的横向几何范围，不能仅靠页面无横向滚动认定可用；桌面按 1280×900 验证。

## 5. 测试报告

完整日志：[make validate](evidence/make-validate.log)、[Playwright 全量](evidence/playwright-all.log)、[后端定向](evidence/pytest-targeted.log)。

测试命令均从仓库根目录执行。环境：Python 3.12；Node 22.23.3；pnpm 10.5.2；Chrome。
本机 Homebrew Node 22 因缺少动态库不能启动，测试使用 npx 隔离安装的 Node 22，不改项目依赖或系统库。

```bash
.venv/bin/python -m pytest -q tests/test_issue_215_managed_evaluation.py tests/test_launch_manifest_freeze.py
pnpm --dir services/console lint:tokens
pnpm --dir services/console typecheck
pnpm --dir services/console test
pnpm --dir services/console build
ARGUS_E2E_API_PORT=29580 ARGUS_E2E_CONSOLE_PORT=29583 pnpm --dir services/console exec playwright test
make validate
```

| 验证项 | 结果与证据 |
|---|---|
| 后端 RED | 新增首批 18 项契约测试全部因缺少新能力失败；补充 API/摘要测试后发现显式空来源回退和摘要重算两个真实缺口；最终审查补充的 NaN/Infinity 三例确认原错误载荷不能序列化，修复后均返回 JSON-safe HTTP 400 |
| 前端 RED | 新增两个创建场景因缺少来源入口失败；实现后创建页 24 项通过；新增跨类型临时重名/修正用例先 RED，改用行位置维护规则后 GREEN |
| 后端目标/Worker | PASS：40 项（含历史 Manifest 回归）；Worker 的 mock HTTP 200 只调用 Agent 一次，执行成功但 scores 为空、质量 UNKNOWN、releasable=false |
| Console 定向 | PASS；默认 Builtin、Managed 请求不带本地 Evaluator、服务端错误可读、类型切换/增删/required、Manifest 展示 |
| Console 完整门禁 | PASS：54 文件、868 项 Vitest；Token lint、typecheck、build 与生成契约同步通过 |
| Playwright 全量 | PASS：131 项，包含本项真实本地 API/数据库创建→拒绝→修正→读回→刷新，以及历史/Builtin 回归 |
| `make validate` | PASS：8 步通过；Python 1455 passed、7 skipped（缺少 TEST_POSTGRES_URL 的既有 PostgreSQL 专项）、31 deselected（trajectory_local）；Compose 静态校验通过 |
| 真实远程 Langfuse/模型 | NOT RUN（本项配置不触发外部网络；前置 #214 证据独立保留） |
| S4 真实摄取专项 | NOT RUN（没有修改 OTLP/Trajectory 摄取链路） |

首次全量 Vitest 在并发运行时有两项既有 Pr126 用例未及时观察到输出；未修改其断言或实现，单独重跑原 84 项通过。最终全量结果以下方记录为准，不将失败首次运行抹去。

## 6. 兼容性与非目标核对

- 无迁移、无新外部网络调用、无 Secret 写入、无 Agent SDK 或业务 DTO 改动。
- Builtin 请求载荷、默认指标、Manifest、幂等、Worker 评分与历史报告回归保留。
- 新增字段均为创建请求可选扩展；仅 Managed Manifest 带新字段；既有查询 API 原样返回已保存 JSON。
- 未修改 Evaluator Catalog、远程 Prompt/Code、Collector、lease/恢复、Release Gate 或完整评分结果页面。
- 当前配置能力不等于托管评分闭环：#216～#219 未完成之前，Managed 任务不能正式发布 PASS。

## 7. 官方语义核对（2026-10-20）

通过 Context7 官方 `/langfuse/langfuse-docs` 核对当前 Code Evaluator Score 类型为 NUMERIC、BOOLEAN、CATEGORICAL、TEXT；本契约使用对应的小写类型名，text 不进入质量判定。

- [Code Evaluators 当前文档](https://langfuse.com/docs/evaluation/evaluation-methods/code-evaluators)
- [Score 类型](https://langfuse.com/docs/evaluation/scores/overview#score-types)
- [Stable Evaluator/Rule API 公告](https://langfuse.com/changelog/2026-08-27-stable-evaluator-api)：ID 是稳定引用，Rule 始终使用其 Evaluator 的最新版本。

没有基于旧 SDK 记忆新增外部 API 调用，也没有以引用 ID 伪造远程版本锁。
