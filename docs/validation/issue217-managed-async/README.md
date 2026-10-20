# Issue #217：托管评分异步等待、可靠恢复与执行/评测解耦

## 1. 结论与证据边界（Mock 与真实区分）

- **Mock 收敛（本机已实测）**：Agent HTTP 成功后进入
  `execution=succeeded / eval=pending / quality=unknown / evaluating`，
  执行消息已确认（不重投、不复调 Agent）；Score 缺席时有界退避、不调 Agent；
  迟到 Score 到达后自动收敛到 `eval=succeeded / recovered`，429/5xx/超时按策略重试，
  超限后 `eval=failed / quality=unknown` 且不伪造分数；多 Worker 单采纳、
  旧代迟到不覆盖、取消丢弃、重跑代际隔离、租约失效崩溃恢复均通过。
  证据：`evidence/mock-async-convergence/`（时间线、故障注入、并发结果均为受控 Mock，不声称真实 Langfuse）。
- **真实 Langfuse 异步收敛（已执行，PASS，2026-10-20）**：见 §4，
  receipt 与时间线已落入 `evidence/live-async-convergence/`。

## 2. 状态机与解耦

```text
HTTP 200 → execution=succeeded, eval=pending, quality=unknown,
           evaluation=evaluating(gen N), available_at=None(立即到期)
           + MANAGED_SCORE(gen N) 入队；Agent permit 已释放。

收集成功(必选齐全) → eval=succeeded, quality=policy判定,
                     evaluation=recovered, 快照新修订。

缺分/429/5xx/超时(可重试, 未超限/超期)
  → eval=pending, evaluation=evaluating(gen N+1),
    available_at=now+backoff(N), 不调 Agent, 由 Reconciler 到期入队。

终态(401/403/契约/Checkpoint/取消/超限/超期)
  → eval=failed, quality=unknown, evaluation=failed,
    不伪造分数，可手动重提。
```

- 执行完成与评测完成解耦：Launch 仍按执行聚合终态（COMPLETED 等），
  快照按证据分为 COMPLETE/DIAGNOSTIC；pending 的 Managed Item 使快照保持
  DIAGNOSTIC，不可用于正式发布（由 #218 消费）。
- `available_at` 复用为 Managed 下次可执行时间（`succeeded+pending+evaluating`
  才有效），不新增迁移；`evaluation_generation` 每次重试递增，旧代经
  `(generation, lease_token, dispatch)` 围栏丢弃；`dispatch_generation` 变化亦丢弃。
- 收集携带租约围栏：远端查询耗时期间失租/换代/取消，迟到写入一律无效，
  只保留拒绝审计；手动单次诊断 API 不带围栏，保持原有语义。

## 2.5 语义：Retry Collection（只读重读），不是 New Remote Evaluation

- 自动轮询只做 **Score Collection**：对同一 Observation 的已有 Score 做
  只读重读（GET observations/scores），**永不创建**新的 Langfuse Evaluator
  Job，也**永不**调用业务 Agent（`attempt_count` 恒为 1）。
- `evaluation_generation` 是 Argus 侧的收集代际（防旧隔离用），**不是**
  Langfuse 的 `job_execution_id`；归属绑定看 Score `metadata`（rule /
  evaluator / job_execution_id / target trace+observation）与冻结契约。
- 终结要求已存成功行 **且** 本轮无歧义采纳：同名冲突（A 后出现合法 A+B）、
  来源冲突、本轮读失败一律阻塞 PASS，保持 UNKNOWN/重试；同一原始 Score
  的合法重读判 `ALREADY_ADOPTED`，不视为冲突。
- 证据不足永远判 UNKNOWN，不伪造分数；历史质量快照不可变。

## 3. 重试、超时与恢复默认值

环境变量（默认）：

```text
ARGUS_MANAGED_SCORE_MAX_ATTEMPTS=10
ARGUS_MANAGED_SCORE_BASE_DELAY_SECONDS=5
ARGUS_MANAGED_SCORE_MAX_DELAY_SECONDS=300
ARGUS_MANAGED_SCORE_DEADLINE_SECONDS=1800
ARGUS_MANAGED_SCORE_COLLECT_TIMEOUT_SECONDS=45
ARGUS_MANAGED_SCORE_CONCURRENCY=4
```

- 退避：`min(5 * 2^(N-1), 300)` 秒，N 为本周期内第几次尝试（1 起始）。
- 重试预算按人工收集周期统计（本周期内 `EvaluationAttemptRecord` 行数），
  与单调代际解耦；人工重提开启新周期并重置起点与 Deadline。
- 单次完整收集总预算 45s（小于 60s Worker 租约）：远端 hung 住即转
  可重试 TIMEOUT，不长期占位；收集结束关闭自建客户端连接。
- Managed 收集使用独立并发槽（默认 4），不占用 Agent HTTP 执行槽。
- Deadline：自本周期起点起 1800s；超期即终态 UNKNOWN。
- 可重试：`RATE_LIMITED / UPSTREAM_UNAVAILABLE / NETWORK_UNAVAILABLE /
  TIMEOUT / SCORE_MISSING / OBSERVATION_* / RESPONSE_* / PAGINATION_*` 及
  各类 Score 校验拒绝（迟到正确分仍可能到达，故到期前继续重试）。
- 终态直落：`AUTHENTICATION_FAILED / AUTHORIZATION_FAILED /
  CLIENT_NOT_CONFIGURED / CLIENT_CONFIG_INVALID / MANAGED_NOT_SELECTED /
  CONTRACT_INVALID / CHECKPOINT_* / ITEM_NOT_FOUND / EVALUATION_CANCELLED`。
- `RESULT_CONFLICT / ALREADY_ADOPTED / SCORE_ALREADY_CLAIMED` 按完整性判定：
  必选齐全即成功，否则继续重试/终态，不覆盖已有有效结果。

恢复步骤：

1. Item 详情查看 `eval_status/eval_error/evaluation_*`：`pending +
   LANGFUSE_MANAGED_SCORES_PENDING` 为等待中；`failed + MANAGED_SCORES_*`
   为已停止，需人工处理。
2. Langfuse 未出分：检查 Rule 是否命中 `remote-agent-http / TOOL`、
   project/environment 是否一致、采样率是否为 100%。
3. 401/403：检查 `LANGFUSE_*` 凭据与项目权限，不会自动恢复。
4. 超限/超期后 Langfuse 已出分：调用“重试评测”（`retry_failed_evaluations`，
   Managed pending/失败可重提）或 `POST managed-scores/collect` 单次诊断，
   将开启新代际收集，不会再次调用 Agent。
5. 误重跑执行（新 dispatch）后旧收集任务自动丢弃，无需清理。

## 4. 真实联调（已执行，PASS，2026-10-20）

脚本：`real_async_convergence.py --execute`（默认 dry run；重跑会产生真实模型费用）。

- 隔离 Runner：当前源码（含 #217）、独立 SQLite、单 Worker（并发 1）、
  `ARGUS_DATASET_SOURCE=langfuse`、`ARGUS_LANGFUSE_RUN_ITEM_STRATEGY=otel`、
  `LANGFUSE_TRACING_ENVIRONMENT=issue217-20261020t0745z`，后台循环运行（非 test 模式）。
- 自有 Rule 两条（100% 采样，目标 `remote-agent-http / TOOL`，仅本次 environment），
  沿用 #214 合成 Dataset 与 Evaluator（LLM Judge + Code）；Flowise 原生 Calculator Agent，
  `max_retries=0`；无 SDK、无 `POST /scores`、未改 #214 既有 Rule，退出时两条自有 Rule 均已暂停。
- 时间线（[timeline](evidence/live-async-convergence/timeline.json)，零手动 collect）：
  - t+18s：HTTP 成功，`eval_status=pending`、`quality=unknown`、`evaluation_status=evaluating`、generation 1；
  - t+33s：仍 pending，generation 3（缺分期两次自动有界重试）；
  - t+48s：自动收敛 `eval_status=succeeded`、`quality=pass`、`evaluation_status=recovered`。
- 全程 `attempt_count=1`（评分重试永不调用业务 Agent）；
  审计显示 Argus POST 仅建 Agent/版本/Launch/Run，Langfuse POST 仅建两条 Rule，
  `collect` 调用 0 次（见 [request-audit](evidence/live-async-convergence/request-audit.json)）。
- 终态（[items-after](evidence/live-async-convergence/items-after.json)）：
  冻结 `quality_evaluation.conclusion=pass、releasable=true`（Judge/Code 均为 true），
  Trace `0feb4fe4…`、Observation `1a979795…`、两条 `source=EVAL` 的 BOOLEAN Score 已采纳；
  只读诊断端点仍投影 `unknown`（与 #216 实测一致，为既有端点语义，非 #217 范围，不改）。
- 结论：[receipt PASS](evidence/live-async-convergence/receipt.json)；凭据已脱敏，无泄露。

## 5. Mock 证据索引

- `evidence/mock-async-convergence/timeline.json`：HTTP 成功 → 缺分 pending →
  迟到分收敛 succeeded 的状态时间线（10 项自动测试全绿为证）。
- `evidence/mock-async-convergence/fault-injection.json`：429/500/超时/401
  的客户端分类与收集终态（401 终态 UNKNOWN，429/超时可重试）。
- `evidence/mock-async-convergence/concurrency.json`：4 Worker 同代仅 1 采纳；
  旧代迟到不覆盖；取消丢弃；重跑隔离。
- 自动化：`tests/test_issue_217_managed_async.py`（14 tests，含执行消息确认、
  租约失效崩溃后同代重入收敛、未配置客户端直落终态、提交幂等/来源守卫）；
  新模块覆盖率 81%（企业基线 70%）；
  回归：#215/#216/#84/Worker/Queue/S3 全绿（见 PR 记录）。

## 6. 兼容性

- Builtin 路径不变：`evaluate_frozen_item` 仅将 Managed 初始从 `failed` 改为
  `pending`（#215 单测不断言该字段，已验证）；Worker Builtin 分支、#84 恢复、
  快照/门禁语义、HTTP 协议均未改。
- 无新增 DB 迁移、无 OpenAPI/Console 契约变更（无新路由）。
