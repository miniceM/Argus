# Issue #217：托管评分异步等待、可靠恢复与执行/评测解耦

## 1. 结论与证据边界（Mock 与真实区分）

- **Mock 收敛（本机已实测）**：Agent HTTP 成功后进入
  `execution=succeeded / eval=pending / quality=unknown / evaluating`，
  执行消息已确认（不重投、不复调 Agent）；Score 缺席时有界退避、不调 Agent；
  迟到 Score 到达后自动收敛到 `eval=succeeded / recovered`，429/5xx/超时按策略重试，
  超限后 `eval=failed / quality=unknown` 且不伪造分数；多 Worker 单采纳、
  旧代迟到不覆盖、取消丢弃、重跑代际隔离、租约失效崩溃恢复均通过。
  证据：`evidence/mock-async-convergence/`（时间线、故障注入、并发结果均为受控 Mock，不声称真实 Langfuse）。
- **真实 Langfuse 异步收敛（待运行）**：本机无 Langfuse 凭据/服务，
  未执行真实联调。真实步骤见 §4，按 #216 的隔离 Runner + 自有 Rule
  方式运行，成功后将 receipt 补入 `evidence/live-async-convergence/`。
  在此之前不得宣称 #217 真实收敛已验证。

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
  `(generation, lease_token)` CAS 丢弃；`dispatch_generation` 变化亦丢弃。

## 3. 重试、超时与恢复默认值

环境变量（默认）：

```text
ARGUS_MANAGED_SCORE_MAX_ATTEMPTS=10
ARGUS_MANAGED_SCORE_BASE_DELAY_SECONDS=5
ARGUS_MANAGED_SCORE_MAX_DELAY_SECONDS=300
ARGUS_MANAGED_SCORE_DEADLINE_SECONDS=1800
```

- 退避：`min(5 * 2^(N-1), 300)` 秒，N 为当前代际（1 起始）。
- Deadline：自 `evaluation_started_at` 起 1800s；超期即终态 UNKNOWN。
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

## 4. 真实联调步骤（待执行，需凭据）

```bash
# 1. 参考 #216 起隔离 Runner（独立 SQLite、单 Worker、otel 策略、独立环境）
# 2. 在 Langfuse 为本次环境创建两条 100% 采样 Rule（LLM Judge + Code），
#    目标 remote-agent-http / TOOL，仅本次 environment。
# 3. 发起 Managed Launch，确认 Agent 成功后 Item 为 pending/evaluating。
# 4. 等待 Rule 出分（或先缺分观察 pending + 退避），确认自动收敛到
#    succeeded/recovered，无人工介入；记录 Trace/Observation/Score ID。
# 5. 暂停并清理本次自有 Rule，不修改 #214 既有 Rule。
# 6. 将 receipt 存入 evidence/live-async-convergence/（脱敏）。
```

**重新执行真实联调会产生模型费用；默认测试均为 Mock。**

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
