# Issue #217：托管评分异步生命周期验收

## 最新复验入口：PR #224 → #225（2026-10-21）

请先阅读 [九项审查矩阵与本轮重新验收](PR224_REACCEPTANCE.md)。本轮在 #225 原 head
`85424e3` 又用 RED 探针确认并补齐锁内 Deadline、真实线程在途预算、提交缺口取消、
同 token 终态审计和并发配置五个剩余边界，并补齐默认执行器枯竭时的真实 HTTP 调度隔离；最终功能冻结提交为 **`3ce3fcf`**。
真实 Langfuse 双评分、SIGKILL/重启与独立 Agent 请求计数已在最终功能提交重新执行；旧 `e9f69ed` / `b8bca1d`
及下文 2026-10-20 记录只作为历史证据，不替代本轮最终源码/最终 head CI 验收。

---


## 1. 本次交付与证据边界

本次基于已含 #215、#216、#224 的 `3f31c8d` 做独立可靠性加固，复用现有
Managed 来源契约、Score Collector、Checkpoint、Lease/CAS、Reconciler 和
Langfuse Outbox。没有新增通用 Workflow Engine、Langfuse Evaluator Worker、
HTTP Agent 协议、迁移、OpenAPI/Console 契约或 Release Gate 重构。

**功能冻结提交：`e9f69edd87212bb99b96f42257afb5169dfca74c`。**
本目录后续文档/证据提交不改变该功能源码；[源码哈希](evidence/live-final-restart/source-identity.json)
和 PR #225 的最终 head checks 分别核对。下列三类证据不得互相替代：

| 层级 | 已执行结果 | 证据 |
|---|---|---|
| 受控故障 + SQLite / 真实 PostgreSQL | 生命周期专项 **53 PASS / 2 SKIP**；两个 SKIP 仅是 PG 专用用例的 SQLite 参数，相应 PG 参数均通过 | [逐用例结果](evidence/lifecycle-hardening/lifecycle-results.json)、[故障矩阵](evidence/lifecycle-hardening/fault-matrix.json) |
| 本地全量门禁 | `make validate` PASS；Python **1612 PASS / 2 SKIP / 31 deselected**，Console **54 files / 868 tests PASS**，契约、build、Compose 均通过 | [验证摘要](evidence/lifecycle-hardening/validation.json) |
| 真实 Langfuse + Flowise + 进程重启 | **PASS，2026-10-20**；无手动 collect、无人工 Score、无 Agent 重试 | [最新 receipt](evidence/live-final-restart/receipt.json)、[重启证明](evidence/live-final-restart/restart-proof.json)，详见 §4 |

31 个 `trajectory_local` 用例由全量门禁按设计排除；S4 自托管集成及 Cloud
策略以 **PR 当前精确 head 的 CI/receipt** 单独核验，不把 `skipped_by_policy`
当作真实 Cloud 通过，不用这里的 Managed 联调替代 S4。

## 2. 状态转换与并发约束

| 事件 | Execution | eval / evaluation | Quality 与动作 |
|---|---|---|---|
| HTTP 成功、Checkpoint 持久化 | `succeeded` | `pending / evaluating`，generation N | `unknown`；确认执行消息并释放 Agent permit；排入 `MANAGED_SCORE` |
| 完整可评分 Observation 未持久发布 | 不变 | 保持 pending，按预算退避 | 不查 Score、不把 OTLP accepted 当作评分完成；现有 Outbox 补发 |
| Score 缺失、429、5xx、网络/单次超时 | 不变 | 未超限时 `pending / evaluating`，generation N+1 | `unknown`；`available_at` 持久退避；只重读原 Observation |
| 必选 Score 齐全、归属/类型/来源合法 | 不变 | `succeeded / recovered` | 按既有 QualityPolicy 判断；所有 sibling settle 后生成新 Snapshot Revision |
| 401/403、无效配置/Checkpoint、取消 | 不变 | `failed / failed` | `unknown`，明确错误码，不捏造值，不重调 Agent |
| 重试预算耗尽或绝对 Deadline 到期 | 不变 | `failed / failed` | `unknown`；分别记录 `MANAGED_SCORES_EXHAUSTED` / `MANAGED_SCORES_DEADLINE_EXCEEDED` |
| 活跃评分 Lease 过期 | 不变 | Reconciler 锁内复核；计入一次尝试后换代退避，或耗尽终结 | 不能同代直接抢占；旧 token 失效 |
| 修复后人工重试评测 | 不变 | 新收集周期、新 generation | 重置本周期预算/Deadline；复用原 Checkpoint 和 Agent Output |
| 重复消息、失租、旧代迟到、旧 dispatch | 不变 | CAS 拒绝过期写入 | 不覆盖当前有效结果或胜出 attempt 审计 |

- 锁顺序统一为 **Launch → Item**。Reconciler 扫描只是候选提示：锁内重读
  generation/token/dispatch/expiry，扫描之后续租的持有者不会被清租。
- `evaluation_generation` 是 Argus **收集代际**，不是 Langfuse Evaluator Job
  代际。自动恢复只 GET 原 Observation/Score，不创建新远端评分任务。
- 分数写入与最终状态均检查当前租约、代际、取消和绝对 Deadline；超时后仍在
  返回的旧线程不能写入下一代。旧 Worker 迟到的 attempt-start 也必须通过围栏，
  不能污染新人工周期的预算。
- Worker 在 claim 后、attempt-start 前崩溃时，Reconciler 补建最小 attempt，
  **先 flush 再计数**；最后一次崩溃同样耗尽预算，不能无限换代。
- 同一 Score 重读 `ALREADY_ADOPTED` 可复用；同名歧义/冲突、本轮读失败不能只因
  DB 已有一行成功记录就终结 PASS。缺证据始终 UNKNOWN，历史 Snapshot 不改写。
- Launch 终态按 HTTP 执行聚合，不表示评测已完成。待评分 Snapshot 为 DIAGNOSTIC；
  只读诊断端点的质量投影与最终冻结质量证据是不同视图，不在本项扩展 Gate 语义。

### 2.1 Agent 并发与可评分 Observation 的持久恢复

Managed 收集使用独立并发上限（默认 4），不经过 Agent HTTP 执行信号量。
达到上限的冗余唤醒消息可确认，由已有持久 Item 状态和 Reconciler 到期补投，
不积累无界等待协程。真实 `lifespan` 调度测试在 HTTP concurrency=1 时阻塞评分，
仍能执行下一个 HTTP Item。

真实 SIGKILL 暴露了 SDK 内存缓冲丢失 TOOL 的缺口：旧 Outbox 只能补空关联 SPAN。
Managed **OTLP** 路径现从校验过的 Checkpoint，经原 Outbox 重建完整 `remote-agent-http / TOOL`
（固定 trace/span、input/output、时间、environment、checkpoint digest）。SDK 仅发
不同名称的 transport SPAN，避免两套可评分生产者。Outbox CAS 成功保存
`_managed_observation_digest` 后才允许收分；旧 SYNCED 空标记、FAILED/SKIPPED
修复后可按人工周期有界补投。Builtin 和 legacy 保留原兼容行为。

**持久 TOOL 重建与真实重启保证针对 #214 已验证的 `otel` 路径**；不把 legacy
SDK 内存缓冲声明成同等持久性保证。

## 3. 默认预算、错误分类与恢复

```text
ARGUS_MANAGED_SCORE_MAX_ATTEMPTS=10             # 上界 100，非法/非有限值安全回退
ARGUS_MANAGED_SCORE_BASE_DELAY_SECONDS=5
ARGUS_MANAGED_SCORE_MAX_DELAY_SECONDS=300
ARGUS_MANAGED_SCORE_DEADLINE_SECONDS=1800
ARGUS_MANAGED_SCORE_COLLECT_TIMEOUT_SECONDS=45
ARGUS_MANAGED_SCORE_CONCURRENCY=4
```

- 退避 `min(5 * 2^(本周期尝试序号-1), 300)` 秒；考虑可用的 `Retry-After`，
  但不得越过周期 Deadline。重复消息不能绕过未来的 `available_at`。
- 预算按本周期的 EvaluationAttempt 行计数，不按单调 generation 大小计数。
  Deadline 自人工周期起点算绝对 1800s：收集前、返回后、锁内终结均复核。
- 单次完整收集预算 45s，且受本周期剩余时间限制；hung 调用触发可重试 TIMEOUT，
  不长期占 Agent HTTP 槽。45s 是整个 Collector 的预算，不是每一分页各给 45s。
- 可重试：`RATE_LIMITED / UPSTREAM_UNAVAILABLE / NETWORK_UNAVAILABLE / TIMEOUT`、
  缺分、未就绪 Observation、分页/响应校验或 Score 采纳拒绝；预算外一律停止。
- 直接终态：`AUTHENTICATION_FAILED / AUTHORIZATION_FAILED / CLIENT_NOT_CONFIGURED /
  CLIENT_CONFIG_INVALID / CONTRACT_INVALID / CHECKPOINT_* / EVALUATION_CANCELLED` 等。

### 3.1 操作步骤（不重调 Agent）

1. 查看 Item 的 `execution_status`、`eval_status`、`eval_error`、`evaluation_*`。
   `execution=succeeded + eval=pending + quality=unknown` 是等待评分，不是 HTTP 失败。
2. 检查 Langfuse project/environment、Rule 是否命中 `remote-agent-http / TOOL`、采样率、
   Score 名称/来源/类型，以及 Outbox/Checkpoint。401/403 先修凭据或权限；不要输出密钥。
3. 超限/超期/投影失败修复后，调用既有 **重试评测** API（无 request body）：
   `POST /api/v1/experiment-launches/{launch_id}/retry-evaluation`。
   Managed pending/失败可重提；新周期由后台自动收集，Agent HTTP attempt 不增加。
4. `POST /api/v1/experiment-launches/{launch_id}/items/{item_id}/managed-scores/collect`
   **只是单次诊断采纳**，不会开启后台新周期，也不能代替上一步的自动终结。
5. 进程重启无需手动补消息；现有 Reconciler 依据持久状态补投。取消可在无租约且仍在
   未来退避时 settle。若用户另行重跑执行形成新 dispatch，旧收集任务自行失效。

## 4. 最新真实联调：HTTP 成功后强杀并重启

源码 `e9f69ed`；独立 SQLite、真实后台 Worker/Reconciler、HTTP concurrency=1、
`ARGUS_DATASET_SOURCE=langfuse`、`ARGUS_LANGFUSE_RUN_ITEM_STRATEGY=otel`。
仅为本次 tracing environment 创建两条 Rule，复用 #214 合成 Dataset、真实 LLM Judge
与 Code Evaluator。Flowise 是未集成 Argus/Langfuse SDK 的通用 HTTP Agent，
模型/Agent 重试关闭（AgentVersion `max_retries=0`），真实调用 Calculator。

[时间线](evidence/live-final-restart/timeline.json)（相对 `/run`，采样间隔 15s）：

- **t+6.0s**：HTTP succeeded；eval pending / quality unknown；generation 2。
- **2026-10-20 11:55:18.884 UTC**：SIGKILL 本次自有隔离 Runner；
  **11:55:19.894 UTC**：相同 SQLite 重启完毕。
- **t+21.0s**：后台恢复、继续 pending，generation 3。
- **t+36.1s**：自动 `eval=succeeded / evaluation=recovered / quality=pass`。

[重启前后 DB 证明](evidence/live-final-restart/restart-proof.json)：HTTP attempt 恒为 **1**，
Checkpoint output digest 恒定。只读复核 Langfuse 原始 API：
[两条原生 `source=EVAL` Score](evidence/live-final-restart/langfuse-scores.json)、
[唯一完整可评分 TOOL](evidence/live-final-restart/langfuse-observations.json)、
[Outbox SYNCED 且 digest 匹配](evidence/live-final-restart/producer-state.json)。
[请求审计](evidence/live-final-restart/request-audit.json)证明无手动 collect、无 `POST /scores`。
[自有 Rule 暂停回读](evidence/live-final-restart/owned-rules-readback.json)均为 false。

**证明范围**：真实强杀发生在评分退避、无活跃评分 Lease 的窗口；活跃 Lease 崩溃、
两个 Reconciler 的竞争、迟到线程和四 Worker 抢占由 SQLite/PG 故障注入覆盖，不能把
真实单 Worker 重启冒称真实多 Worker 故障演练。模型评分 rubric 仅是合成 smoke，
不声称验证业务质量或外部 Evaluator 的不可变历史实现版本。

### 4.1 失败到修复的记录（不删除失败证据）

| 记录 | 结果与归因 |
|---|---|
| [endpoint-404](evidence/live-hardening-endpoint-404/failure-receipt.json) | FAIL；夹具把旧 Flow ID 指向另一套 Flowise 服务；无 Agent 自动重试，自有 Rule 已暂停 |
| [pre-fix-sdk-loss](evidence/live-hardening-pre-fix-sdk-loss/failure-receipt.json) | FAIL；源码 `592a917` 在 SIGKILL 后丢 SDK 可评分 TOOL；转为 Outbox + Checkpoint 回归并修复 |
| [首次修复后重启](evidence/live-hardening-restart/restart-proof.json) | PASS，历史功能提交 `a9de722`，不是当前提交证据 |
| [最终重启](evidence/live-final-restart/restart-proof.json) | PASS，功能冻结提交 `e9f69ed`，本节使用此结果 |
| `live-async-convergence/`、`mock-async-convergence/` | #224 原实现的历史验收；保留，不作为本轮故障修复的当前证明 |

### 4.2 重现方式

先准备隔离的 Runner 数据库与进程，注入项目凭据和独有 tracing environment，开启
Worker/Reconciler；不可指向现有生产库或复用本次证据目录。使用仓库脚本：

```bash
.venv/bin/python docs/validation/issue217-managed-async/real_async_convergence.py \
  --execute --argus-url "$ISOLATED_ARGUS_URL" \
  --environment "$ISOLATED_ENVIRONMENT" --agent-endpoint "$HTTP_AGENT_ENDPOINT" \
  --evidence-dir "$NEW_EVIDENCE_DIR" --converge-timeout 1200
```

脚本会产生真实模型费用；默认 dry run。它只创建本次 environment 的 Rule，退出时暂停。
重启演练应另行监督此**自有** Runner PID：在新目录生成 `items-before.json`、确认
HTTP succeeded / eval pending 后 SIGKILL，用原数据库和同一环境重启。不要杀共享
Runner，也不要调用 `/run`、collect 或手写 Score 来替代恢复。独立核对 DB attempt 数、
Checkpoint digest、原生 Score 和 Rule 清理状态。

## 5. 自动化验证与兼容性

- RED：在已含 #224 的基线重放当时新增的 25 个边界参数，**19 FAIL / 4 PASS / 2 SKIP**；
  最后一次“claim 后、attempt 前崩溃”回归在 `84386f8` 上两库均 RED，flush 修复后均 GREEN。
  见 [RED 记录](evidence/lifecycle-hardening/red-results.json)。
- GREEN：`tests/test_issue_217_lifecycle_boundaries.py`，55 参数化用例中的 **53 PASS / 2 SKIP**，
  覆盖 Deadline、Retry-After/退避门禁、取消、崩溃预算、失租/换代、重复消息、迟到线程、
  四 Worker 单采纳、双 PG Reconciler、真实运行时 HTTP 并发隔离、持久 TOOL、Outbox 故障修复。
- 既有 `test_issue_217_managed_async.py` / `test_issue_217_managed_review.py`，以及
  #215/#216、Builtin、#84 Evaluation Recovery、Worker/Queue、S3 回归由全量门禁覆盖。
- 重现专项：设置仅测试库的 `TEST_POSTGRES_URL`，运行
  `.venv/bin/python -m pytest -q tests/test_issue_217_lifecycle_boundaries.py`。
  不设置该变量会跳过 PG，不能据此宣称 PG 并发已验收。
- 唯一本地门禁：Node 22、Python 3.12 下设置同一隔离测试库，执行 `make validate`。
  本机 Homebrew Node 动态库损坏，使用校验官方 SHA256 的隔离 Node 22.22.0，未改宿主环境。
- 没有 API/Console 行为变更；生成契约无 drift。Builtin/legacy 默认语义保持，
  新的围栏/退避参数仅由 Managed 显式选择。既有 Snapshot、Release Gate 和 HTTP 协议不变。
