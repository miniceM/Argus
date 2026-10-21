# PR #224 九项审查问题重新验收（2026-10-21）

## 对象、身份及授权边界

- 原审查：PR #224 / Issue #217 / 父 Issue #212，head `501edd01e38792833a739606b354daba5fb0e3a7`。
- 当前 main / #224 merge：`3f31c8df13fc4232ed55dd1b1cca1499e57003f2`。#224 已合并，不把它的历史缺陷直接归咎于当前修复版本。
- 继续现有修复 PR **#225**，base 为上述 main；本轮开始 head 为 `85424e3ee4b22e946ca10ab0787ab92f5a07d931`，checkout 干净。没有另建重复 PR。
- **本轮最终功能冻结提交：`3ce3fcfc491da44e7f7df47f66adf275d9197963`**。之后仅更新验收脚本、文档和证据；九个生命周期源码文件由真实运行的 `source-identity.json` 逐一 SHA256 核验。最终交付 head 和其 CI 以 PR #225 的实时 checks / 交付说明为准，不把功能提交的旧 CI 当作最终 head CI。
- 未合并、未发布 Release、未修改 Ruleset / Environment。只创建本次独有 environment 的两条 Langfuse Rule，结束后暂停并 GET 回读；不更改他人的 Rule。

## 九项问题矩阵

`85424e3` 的“已含修复”不等于通过；下表的最终状态均来自本轮重跑。测试位于默认执行的 `tests/test_issue_217_lifecycle_boundaries.py`，不依赖原审查目录。

| 项 | 对 `85424e3` 的核实 | 本轮补齐 / 复验 | 最终状态 |
|---|---|---|---|
| F1 周期 Deadline | 收集前/远端返回/写入围栏已修复；**锁内质量计算跨 Deadline 后仍可成功**，两库 RED | 质量计算之后、结论写入之前再次检查绝对周期 Deadline 和即时 Lease；既有超期不读、GET 跨界拒绝及新增锁内跨界回归均通过 | **已修复且验证通过** |
| F2 HTTP / Managed 隔离 | 真实 lifespan 已分槽；**超时 caller 释放后 daemon 收集线程可超出 Managed 在途预算**，RED | 保留真实 dispatcher 的有界任务；进程级非阻塞在途认领直到远端线程实际退出才归还，无排队线程/等待协程膨胀；满额返回 `COLLECTOR_BUSY` 持久退避。补充探针还在 `b8bca1d` 复现默认线程池枯竭，故 Managed 改用独立有界执行器并在 lifespan 收尾关闭；真实 lifespan + Worker 的默认池仅 1 线程时 HTTP 仍可调度 | **已修复且验证通过** |
| F3 Reconciler 竞争 | 已含 Launch → Item 锁内重读和 generation/token/dispatch/expiry 检查 | 独立 PG 实跑扫描后更新有效租约、双 Reconciler、四 Worker 单采纳；取消提交缺口扫描同样排序并沿用锁序。关键竞争组再重复五轮 | **已修复且验证通过** |
| F4 可评分 Observation 恢复 | 已含 Checkpoint → 现有 Outbox 的完整 TOOL 重建、digest 发布标记 | 两库解码 OTLP 检查完整 input/output；发布未就绪不能先查询 Score；损坏 Checkpoint、Outbox 发布失败与人工重提回归；真实 SIGKILL/重启后完整 TOOL + 两条原生评分只读核验 | **已修复且验证通过** |
| F5 崩溃预算 | 已改为过期换代，保留旧 attempt；claim→attempt 之间崩溃补最小审计并 flush 后计数 | 两库重复崩溃、无审计行崩溃、人工新周期和旧 attempt-start 竞争通过。达到上限由 Reconciler 直接 failed/UNKNOWN，不复活同代 | **已修复且验证通过** |
| F6 取消收敛 | 已覆盖正常排队、退避、收集中和过期租约；**HTTP 成功但首轮尚未提交时取消仍永久 pending**，两库 RED | 将该提交缺口纳入原锁内取消转换，清理调度状态，物化既有 settled Snapshot；新增回归断言 Snapshot failed/UNKNOWN、Agent attempt 不增加 | **已修复且验证通过** |
| F7 Attempt 审计 | 已防旧 token 改写胜出者；**同一胜出 token 的重复失败收尾仍把 succeeded 改 discarded**，两库 RED | 带租约关闭只允许修改仍 running 的对应 attempt；终态重复收尾只留拒绝日志，不改原 status/completed_at/error。旧 token / 旧代开始与关闭回归通过 | **已修复且验证通过** |
| F8 持久退避 | 已含 Retry-After（秒数/HTTP-date）和 claim 的 available_at 门禁 | 两库 RATE_LIMITED + 提前的下一代消息不读远端、不调用 Agent；原独立 `future_generation` 探针已正式纳入默认回归 | **已修复且验证通过** |
| F9 非法配置 | max_attempts / Deadline / delay / timeout 已清洗；**concurrency 的 Infinity 抛 OverflowError，极大有限值没有上界**，RED | concurrency 非有限/非法/非正数回退 4，合法数上限 100；inf/-inf/Infinity/NaN/溢出/bad/零/负数及极大有限值回归通过 | **已修复且验证通过** |

### 原探针的完整保留与解释

本轮未修改原审查文件；原样重放结果为 **33 PASS / 2 FAIL / 2 SKIP**。两个 FAIL 均是同一崩溃预算探针的 SQLite/PG 参数：它要求两次中断后“下一个 Worker 返回 EXHAUSTED”。修复版本已由 **Reconciler 更早持久终结 failed/UNKNOWN**，因此下一个 Worker 返回 `NOT_CLAIMABLE`。

没有删掉这两个失败，也没有把返回值放宽后冒充原探针通过。正式回归直接断言持久终态、`MANAGED_SCORES_EXHAUSTED`、恰好两条评分 attempt、一次 HTTP attempt；另覆盖 claim 后尚未写 attempt 即崩溃的预算。该行为优于等待下一条消息才耗尽，且业务 Agent 未重调。原始运行结果及逐 case 状态见 [本轮本地证据](evidence/goal-reacceptance/local-tests.json)。最初将探针放在仓库外运行时出现 fixture discovery error，随后在当前仓库 fixture 下原样重跑；不把 discovery error 当作产品 RED。

## 状态转换与预算

```text
HTTP succeeded + Checkpoint + durable Outbox
  ├─ 提交缺口：evaluation=none → Reconciler 幂等补交；取消则直接 settled failed/UNKNOWN
  └─ evaluating/pending/UNKNOWN + available_at
       ├─ 到期、无有效租约 → claim(generation, token, dispatch) → 记录本轮 attempt
       │    ├─ 发布 TOOL 尚未就绪 / 缺分 / 429 / 5xx / timeout / 容量满 → attempt 关闭
       │    │      → generation+1，持久 available_at，清 Lease，保持 pending/UNKNOWN
       │    ├─ 必选评分齐全且围栏有效 → 锁内 Deadline/Lease 再核验 → succeeded/recovered
       │    └─ 取消 / 认证失败 / 证据错误 / 超期 / 超限 → failed/UNKNOWN，清调度，settled Snapshot
       └─ 活跃收集中断 → 过期 Reconciler 锁内重验 → 关闭/补审计并消耗预算
              → 新代退避或直接耗尽；旧 Worker 不可写结果或改已终结审计
```

默认：10 次、5s 基础退避、300s 退避上限、1800s 周期；单次收集最多 45s，并取周期/Lease 剩余时间的最小值。Managed 并发默认 4，上限 100，与 HTTP `ARGUS_WORKER_CONCURRENCY` 分离，且阻塞收集使用独立线程执行器，不抢占队列轮询/HTTP 的默认执行器。超时未退出线程继续占 Managed 在途预算；满额不会新建后台线程。重试始终复用 Checkpoint，不调用业务 Agent。

人工处理凭据/Rule/证据故障后，用既有 `POST /api/v1/experiment-launches/{launch_id}/retry-evaluation`（无 body）开启独立新周期；单次 `managed-scores/collect` 是诊断，不代替自动恢复。旧代不能更新新周期预算。取消的 Launch 不因重投复活。Builtin 恢复保留既有语义，历史 Snapshot 不回写，Gate 不引入在线 Langfuse 依赖。

## 本地、PostgreSQL 与真实服务证据

| 层级 | 本轮结果 / 边界 |
|---|---|
| TDD RED | 质量计算跨 Deadline、提交缺口取消、同 token 审计各 SQLite/PG 两参数，共 6 FAIL；迟到线程逃逸预算 1 FAIL；并发配置 5 FAIL / 4 PASS；真实 Worker 默认执行器饥饿 1 FAIL。均保留，不降低断言 |
| 专项 GREEN | 104 PASS / 2 SKIP；SKIP 仅为两个 PG 竞争用例的 SQLite 参数，两个 PG 参数实际执行 |
| 重复竞争 | 5 轮，各 14 PASS / 2 SKIP；含真实 PG 扫描/行锁竞争及新收尾、取消、迟到线程边界 |
| make validate | Python 1631 PASS / 2 SKIP / 31 deselected；Console 54 files / 868 PASS，typecheck、token lint、OpenAPI 双快照无漂移、build、Compose PASS |
| S4 本地集成 | `make trajectory-prepare` + `make trajectory-test`：31 PASS；自托管真实采集，与 Managed Cloud 评分分别留证 |
| 真实 Langfuse + SQLite 重启 | 下节；不能代替 PostgreSQL 竞争测试，也不把受控 429/5xx 注入说成真实 Cloud 故障 |
| 最终 head CI | 必须核对 PR #225 最终 head 的五项常规检查和 Cloud Integration Policy；普通 PR Cloud 的 `skipped_by_policy` 不算真实 Managed 生命周期验收 |

独立 PG 使用专属容器、仅 loopback 随机端口、独立测试库，每个测试再使用唯一 schema，并在结束清理。没有连接已有应用 PostgreSQL。Python 3.12.11；隔离 Node 22.22.0 / pnpm 10.5.2；没有更改宿主 Node 环境。

### 真实重启（2026-10-21，Asia/Shanghai）

[最终验证报告](evidence/live-pr225-final-restart/verification-report.json)、[源码身份](evidence/live-pr225-final-restart/source-identity.json)、[重启及独立请求计数](evidence/live-pr225-final-restart/restart-proof.json)：

- 冻结 Runner build_id / 源码 commit 均为 **3ce3fcf**；environment `issue217-final-20261021t013748z`。
- t+6.1s：HTTP succeeded，eval pending，quality unknown；随后 **09:38:06.971 SIGKILL** 自有 Runner，**09:38:07.745** 原库重启。
- t+21.1s：后台仍 pending；t+36.2s：自动 succeeded / recovered / pass，无手工 collect，无 Score 写请求。
- HTTP 计数代理跨 Runner 重启存活，实际请求 **1**；数据库 execution attempt **1**；Checkpoint output digest 不变。
- [原生双 EVAL Score](evidence/live-pr225-final-restart/langfuse-scores.json)、[完整目标 TOOL](evidence/live-pr225-final-restart/langfuse-observations.json)、Outbox SYNCED digest 均与同一执行身份一致；[两条自有 Rule 暂停回读](evidence/live-pr225-final-restart/owned-rules-readback.json)确认 false。
- 强杀窗口是退避中、无活跃评分 Lease；活跃租约崩溃、续租与多 Worker 竞争由 PostgreSQL 故障注入证明，不扩大真实演练范围。

此前 `b8bca1d` 的真实运行和脚本回读失败保留为历史：当时只按 Observation ID 筛选，API 同时返回 TOOL 和 transport SPAN，监督脚本严格断言失败；[失败修正规则](evidence/live-pr225-goal-restart/supervisor-readback-correction.json)仍保留。后续因执行器隔离改动了关键代码，**没有复用该次运行作最终证据**，而是在 `3ce3fcf` 重新执行整套真实 Agent → SIGKILL → 原库重启 → 原生双评分 → 清理回读，监督脚本正常返回 0。最终筛选精确要求 **id + name + type** 与完整 input/output；不把 transport SPAN 或 OTLP accepted 单独当作评分前置证据。

## 可重复执行

```bash
# TEST_POSTGRES_URL 只指向专属测试 PostgreSQL；不能使用生产连接。
.venv/bin/python -m pytest -q tests/test_issue_217_lifecycle_boundaries.py \
  tests/test_issue_217_managed_async.py tests/test_issue_217_managed_review.py
make validate
make trajectory-prepare
make trajectory-test

# 真实调用有费用。预先在环境中安全提供 Langfuse 三项配置；先 dry run。
.venv/bin/python docs/validation/issue217-managed-async/verify_live_restart.py \
  --agent-endpoint "$HTTP_AGENT_ENDPOINT" --environment "$UNIQUE_ENVIRONMENT" \
  --evidence-dir "$NEW_EVIDENCE_DIR"
# 确认隔离环境后增加 --execute；仅杀该脚本创建的 Runner，原库恢复。
# 仅只读复核已有证据时使用 --verify-evidence，禁止为凑证据重调 Agent。
```

所有正式回归已在仓库内、默认测试发现范围内。真实证据不含凭据；私有进程日志不导入仓库。没有 API/Console 行为变更或迁移，未提前重构 Release Gate。
