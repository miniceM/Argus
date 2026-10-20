# Issue #216：Scores v3 定向读取、可信关联及不可变采纳

## 1. 结论与证据边界

2026-10-20 完成真实验证：**当前 Managed Launch 的两条 Langfuse 原生 EVAL Score → 同一 Argus Item → 既有 TypedEvaluationResult / evaluation_results**。重复收集保持同一结果及审计身份，不调用 Agent、不回写 Score。

本项只交付单次定向收集及只读诊断，不实现 #217 的后台轮询/恢复、#218 的正式质量决策或 #219 的完整 Console 体验。所有收集响应固定 `quality_conclusion=unknown`、`releasable=false`；既有 Item 状态、数值投影、质量结论和历史 Snapshot 不回写。外部结果 provenance 为 `MANAGED_DIAGNOSTIC_ONLY`，不冒充 Builtin 的 `FROZEN_VERIFIED`。

| 验证层 | 结果 / 证据 |
|---|---|
| #214 历史真实双 Score 重新读取 | PASS：[只读验收](evidence/issue214-fresh-readback/verification.json)，两套旧 Launch 各两条真实 Score；13 个 API page，不改旧 Builtin Manifest |
| 新生产读取器读取 #214 和本次 Score | PASS：[native client receipt](evidence/native-client-readback/receipt.json) 与逐页原始响应；`page_size=1`，真实 cursor 多页/省略末页 cursor |
| 新 Managed Launch 实际采纳 | PASS：[receipt](evidence/live-adoption/receipt.json)、[首次诊断](evidence/live-adoption/diagnostics-first.json) |
| 同一真实 Score 重复查询 | PASS：[第二次收集](evidence/live-adoption/collection-repeat.json)、[第二次诊断](evidence/live-adoption/diagnostics-repeat.json)；两条 `ALREADY_ADOPTED`，有效结果仍为两条，结果载荷不变 |
| Agent 未重复执行 | PASS：[执行后 Item](evidence/live-adoption/items-after.json)，Attempt=1、Trace/Observation 不变、quality=unknown、scores={} |
| 自有测试 Rule 清理 | PASS：[本次两条 Rule 已暂停](evidence/live-adoption/owned-rules-paused.json)；未重启/修改 #214 的 Rule |
| 负例与故障注入 | Mock/数据库自动测试，不声称真实注入了 401/429、跨项目或恶意 Score；见第 5 节 |

**真实身份**：

- Launch：`33969c21-e67c-4282-89c1-3498aeeaa8dc`
- Item：`b6c920e7-d950-412d-990e-466c92cd0b41`
- Trace：`47c0a6bd069c57989b84536c1a299327`
- Observation：`8df5997b4fbb53ef`
- Code Score：`8c66eceb-635d-52af-9c2d-f32216495a7a`，BOOLEAN `true`
- Judge Score：`867fb112-3be4-57de-9d72-6bd3b8cb323e`，BOOLEAN `true`
- 两条 Score 的 `source=EVAL`、Rule/Evaluator/job/target metadata、原始 value/type、subject 见诊断结果；Rule 使用 Langfuse latest，**不是 Argus 冻结了其实现版本**。

## 2. 受控采纳与身份链

1. 必须是创建时显式选择 `langfuse_managed` 的 Launch；验证 #215 已冻结的 `acceptance_digest`、project/environment/target，不读取客户端临时提交的采纳条件。
2. Item 必须属于当前 Launch 且执行成功；Trace/Observation 不为空；同一 Trace+Observation 不得映射到另一个本地 Item（即使尚未采纳 Score，也拒绝歧义）。
3. 验证当前 dispatch generation 的 checkpoint：Launch、Item、Dataset Item、final attempt、Trace/Observation、Manifest digest、原始 Agent output digest、TTL。过期、缺失、旧代、损坏不能以远程名称/最新记录补齐。
4. Observations v2 必须唯一找到同 project/environment 的 `remote-agent-http / TOOL`；空 replay SPAN 即使 ID 相同也不是业务评分目标。IO 原始 JSON 字符串本地解析；request 与 checkpoint 输入、完整 output 与 checkpoint 摘要必须一致。
5. Scores v3 按 Trace、Observation、`source=EVAL`、期望名称查询，包含 `fields=details,subject`。所有页和所有期望名称读取完成后才开始采纳；HTTP/分页中途失败不会写入部分结果。API 的 `name` 是 CSV，名称含逗号时不发歧义的名称过滤，仍按 subject/source 定向查询并本地 exact match。
6. 每条候选重新验证 project、environment、名称、dataType、完整 observation subject、`source=EVAL`、Rule ID、可选配置的 Evaluator ID，以及 Langfuse job 和目标 Trace/Observation metadata；存在 Code IO hashes 时与完整原生 Observation IO 对照。`configId` 单独存在不足以证明 Rule 归属。
7. 同一 ID 同载荷去重；同一 ID 不同载荷、同一期望多份合法候选均拒绝，不任选 latest。与已保存结果冲突时只追加拒绝审计，不能替换原结果。不同 Rule 的同名 Online Score 不满足冻结条件，不会污染所选评分。
8. 写入前取得 Item 写锁并重新读取完整 Context；执行代、评测代、身份、checkpoint 或 Manifest 变化均拒绝晚到数据。数据库同时约束 `(Item, evaluator_id)` 和 `(external_project_id, external_score_id)`，避免并发或跨 Item/Launch 重复有效结果。

缺分只有诊断审计，不生成 Typed Result，更不会变成 0 / FAIL / PASS。数值 NaN/Infinity、boolean 假数值、非法类别、不支持的类型遵循现有 Typed Result 校验，不做宽松转换。

## 3. 持久化与 API

迁移 `019_managed_score_evidence.sql`：

- 既有 `evaluation_results` 只增加 nullable 外部 project/Score ID 及联合唯一约束，Builtin 历史行不变。
- 新表 `managed_score_evidence` **只是追加式采纳/拒绝审计，不是第二套结果模型**；复合外键约束 Item/Launch。确定性审计 ID 让相同读取不膨胀为多份有效采纳记录。
- 有效值使用原有 `TypedEvaluationResult`、`ResultProvenance`、`EvaluationResultRecord`、既有 API Result DTO。不创建 Evaluator Registry / 评分引擎 / 版本锁。
- 保存实际 Score ID、原始值/类型、source、subject、timestamp/createdAt/updatedAt、首次观测时间、数据库采纳时间、外部 configId、白名单 job/Rule/Evaluator metadata、checkpoint/Manifest/采纳契约引用。无需复制完整 Trace/业务输出或任意外部 metadata。
- `score_url` 为 Trace/Observation 的 UI 导航落点，真实 Score ID 独立保存；不声称官方提供了一个独立 Score 页面 URL。没有官方版本证据时 `evaluator_version=null`，observed `evaluator_version_id` 如有则仅保存在原始 metadata。

新增：

```text
POST /api/v1/experiment-launches/{launch_id}/items/{item_id}/managed-scores/collect
GET  /api/v1/experiment-launches/{launch_id}/items/{item_id}/managed-scores
```

POST 不接受任意远程 Score/契约注入，只从服务端配置的 Langfuse origin 和凭据读取。GET 完全本地，只读结果与审计。OpenAPI / Console 生成类型同步更新；本项未改 UI 组件。

示例：

```bash
curl -X POST "$ARGUS_URL/api/v1/experiment-launches/$LAUNCH/items/$ITEM/managed-scores/collect"
curl "$ARGUS_URL/api/v1/experiment-launches/$LAUNCH/items/$ITEM/managed-scores"
```

常见诊断码：`SCORE_MISSING`、`SOURCE_MISMATCH`、`RULE_MISMATCH`、`EVALUATOR_MISMATCH`、`PROVENANCE_MISSING`、`SUBJECT_MISMATCH`、`ITEM_IDENTITY_AMBIGUOUS`、`SCORE_AMBIGUOUS`、`SCORE_ID_CONFLICT`、`RESULT_CONFLICT`、`SCORE_ALREADY_CLAIMED`、`CHECKPOINT_MISSING/EXPIRED/MISMATCH`、`COLLECTION_CONTEXT_CHANGED`。

读取故障：`AUTHENTICATION_FAILED`(401)、`AUTHORIZATION_FAILED`(403)、`RATE_LIMITED`(429)、`TIMEOUT`、`UPSTREAM_UNAVAILABLE`、`RESPONSE_INVALID`、`PAGINATION_INVALID/LIMIT`。单次请求不隐藏重试，失败可诊断并保持 UNKNOWN；调用方可显式重试。支持数字 `Retry-After` 提示；不保存或回显上游异常原文、认证头和凭据。不跟随重定向发送凭据。

## 4. 真实联调与保留的失败事实

隔离 Runner 使用当前源码、独立 SQLite、单 Worker、`ARGUS_DATASET_SOURCE=langfuse`、`ARGUS_LANGFUSE_RUN_ITEM_STRATEGY=otel` 和 `LANGFUSE_TRACING_ENVIRONMENT=issue216-20261020`。AgentVersion `max_retries=0`；既有 Flowise 原生 Calculator Agent 和 #214 合成 Dataset/Evaluator 复用；没有给 Agent 加 SDK，没有重实现 Judge/Code，也没有 `POST /scores`。

`real_acceptance.py --execute` 才会新建本次隔离的两条 Rule 并调用一次 Agent；默认 dry run。调用新 Rule 的 scope 仅本次环境和 `remote-agent-http`，退出暂停自己创建的 Rule，不修改既有规则。**重新执行该脚本会产生真实模型费用**。

```bash
python docs/validation/issue216-managed-scores/real_acceptance.py \
  --argus-url http://127.0.0.1:29680 \
  --environment issue216-20261020 \
  --agent-endpoint "$FLOWISE_PREDICTION_URL" --execute
```

Runner 必须正常启动真实 Worker，不能设为 `ARGUS_DB_MODE=test`（该模式不启动后台循环）。Langfuse 三个凭据变量仅从安全环境提供，不写入仓库/命令参数。本项客户端不做隐藏重试，AgentVersion 重试为 0；Flowise 共享模型连接未改动，其模型配置沿用前置 #214 记录，本项不声称另行核验了共享服务内部的模型重试策略。

已执行的失败及修正：

1. [首次环境预检](evidence/preflight-test-mode/) 中误用 test mode，Launch queued、Agent Attempt=0，没有声称完成；该轮两条自有 Rule 也已暂停。随后新建正常运行的独立 Runner/Launch。
2. 实际 API 末页 `meta={}` 或 `{limit:1}`，会省略 cursor。最初客户端过严，记录 `RESPONSE_INVALID` 且没有采纳；补充来自真实响应形状的 RED 后，按官方 optional cursor 修正为 GREEN。
3. 实际 Code `input_sha256` 针对完整 `{agent, request}`，不是 checkpoint 的 request 子对象。最初拒绝 Code (`SCORE_IO_MISMATCH`)，Judge 已作为诊断采纳；补充 RED 并按完整原生 IO 修正。没有删拒绝记录或放松归属校验，未重复调用 Agent。
4. Rule 已产生实际 Score 后暂停。修正后重启 Collector，复用**同一真实 Launch/Item/checkpoint**，完整采纳，再重复查询；期间没有修改冻结 Manifest 或给旧 Builtin Launch 事后 opt-in。

保留失败历史：[collection-history](evidence/live-adoption/collection-history.json)；最终：[diagnostics-first](evidence/live-adoption/diagnostics-first.json)、[diagnostics-repeat](evidence/live-adoption/diagnostics-repeat.json)、[API audit](evidence/live-adoption/adoption-api-audit.json)。审计会同时显示历史拒绝与后续合法采纳，而不是抹去失败。

无需付费重跑 Agent 的复核：

```bash
python docs/validation/issue216-managed-scores/verify_adoption.py \
  --argus-url "$ARGUS_URL" --launch-id "$LAUNCH" --item-id "$ITEM"
```

必须指向保有上述真实 Launch/checkpoint 的实例；不支持伪造 ID 或临时拼接 Manifest 使其通过。此命令仅重复查询与采纳，不创建 Rule、Score 或 Execution Attempt。

## 5. 自动化验证

- 测试先行：首次缺少采纳模型/模块产生 ImportError；API、末页 cursor、逗号名称、本地身份歧义、Code 原生 IO hash、非法 ID JSON 响应均有实际 RED，再修至 GREEN。
- 定向测试覆盖 401/403/429/500、超时、页预算/循环/中途错误、畸形响应、空集、NaN/Infinity/非法类型、同名不同来源、跨 project/环境/Trace/Observation/Rule/Evaluator、checkpoint 损坏/过期/旧代、网络读取中代变化、同 ID 不同载荷、多个合法候选、迟到冲突、重复/并发采纳与 JSON-safe API。
- SQLite 与独立 PostgreSQL 16 均覆盖迁移、外部 Score 唯一约束、审计跨 scope 外键；四个并发 Collector 最终只有一条有效结果/采纳审计。
- 真实 receipt 自动测试只核验留存证据的一致性，不将其称为本次单测发起了真实网络/模型执行。
- 本项没有改 OTLP 摄取链路，不把普通 CI / Cloud policy skipped 视为真实专项通过；真实托管证据仅以上述接口/远端 Score 为准。

命令及最终统计见 [测试报告](evidence/tests.json)、[make validate 日志](evidence/make-validate.log)、[PostgreSQL 定向日志](evidence/postgresql-tests.log)。

```bash
python -m pytest -q tests/test_issue_216_managed_scores.py
# 显式提供独立测试库才会执行 PostgreSQL 专项；不会连接生产数据库。
TEST_POSTGRES_URL="$ISOLATED_TEST_POSTGRES_URL" python -m pytest -q tests/test_issue_216_managed_scores.py
make validate
```

## 6. 契约缺口与不扩大的结论

本次实际 project 的 Scores v3 metadata 足够证明 Rule/Evaluator/job/subject/目标归属，未遇到必须猜测才能采纳的缺口。但这些 metadata 是公开 Score 中**实际可获得的证据**，不能推断所有部署都必然提供。缺少必要 Rule/job/target 关联时明确 `PROVENANCE_MISSING`，即使名称、value、configId 或 Trace 看起来正确也不采纳。

不以本项宣称通用 Ground Truth 动态映射、远端 Evaluator 实现冻结、持续轮询、多 Worker 后台恢复、正式 Gate 发布、历史数据回填或完整 UI 交付。受控外部分数路径没有改变 Builtin 的原有单向 Langfuse 投影。

官方依据（2026-10-20，Context7 官方文档及实时 OpenAPI 复核）：[Scores API](https://langfuse.com/docs/api-and-data-platform/features/scores-api)、[Public API](https://langfuse.com/docs/api-and-data-platform/features/public-api)、[实时 OpenAPI](https://cloud.langfuse.com/generated/api/openapi.yml)、[Stable Evaluator/Rule API](https://langfuse.com/changelog/2026-08-27-stable-evaluator-api)。来源和实现制品摘要见 [provenance](evidence/provenance.json)。
