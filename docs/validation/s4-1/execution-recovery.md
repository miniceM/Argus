# S4.1 成功响应后的执行恢复

关联 #5、#127、#128，基于 PR #176 的 Worker 自动采集。本批接通成功响应已保存、原执行租约过期后的队列恢复；eval-only 轨迹输入 Adapter、规则、Snapshot 新资格仍在后续实施。

## 恢复契约

- 仅显式 Manifest 1.3 且当前 generation 存在响应 Checkpoint 时进入此路径。Reconciler 以原 lease/generation/status/过期条件 CAS 重新入队，保留原 generation、Attempt、执行者与响应；不消耗 Agent 重试次数，即使 Agent 非幂等也不会再发业务请求。
- Worker 领取新执行租约后，在 Agent 许可、凭据预取、新 Attempt 或 HTTP 之前优先读取保存的响应。按 Launch→Item→Attempt→Checkpoint 顺序加锁，锁后核验当前时间与 ownership，验证输出摘要、TTL、Manifest/Binding、Dataset 输入和原 Attempt 归属；仅把原 Attempt 的 lease fence 转交当前持有者。
- 恢复不会刷新 Checkpoint 的 created_at、TTL 或输出。新租约通过独立 heartbeat 续期；Coordinator 继续原 scope、deadline、累计扣减/保守预留和冻结制品。PENDING 只在原限额内继续，原 deadline 到期或未知在途额度已扣满则在原边界收敛；终态回读不开 Reader。
- Manifest/输出/归属损坏或响应过期时返回有限 `TRAJECTORY_RECOVERY_*`，评测失败、质量 UNKNOWN、无 Score；禁止回退为另一次 Agent 调用。已有成功响应不能因为缺少轨迹 Root 而丢弃；缺少可信 scope 仍按 Worker 有限采集诊断处理，由后续 Adapter 判定缺少轨迹证据。
- 1.3 响应写入同事务记 Attempt 的 `RESPONSE_RECEIVED`。Checkpoint 在重新调度前或后被清理时，Reconciler 仍以可信已回答 Attempt 保留原代调度；Worker 遇到原代活动 Attempt 而缺响应时关闭普通执行入口，有限 `TRAJECTORY_RECOVERY_CHECKPOINT_MISSING`、评测失败/UNKNOWN/无分数。已确认业务响应的事实保留，不为幂等或非幂等 Agent 新建 Attempt。
- 外部 Task 取消保留原响应并停止后续评测；新租约失效返回未完成，原响应保持可供再次调度。恢复最终写入锁 Launch 后锁 Item，取消与结果保存串行；期间取消使 Item cancelled、评测 skipped、质量 UNKNOWN，不提交先前算出的通过结果。原业务响应的 Attempt 仍成功。
- 原租约不能保存响应、提交采集轮次或最终结果。1.3 的响应写入补齐 Launch→Item→Attempt 锁序与锁后 lease/generation 校验；已存在响应视为不可替换，不刷新原期限。1.2 保持原重试与幂等保护策略。

## 验收与复现

```bash
python -m pytest -q tests/test_trajectory_execution_recovery.py \
  tests/test_trajectory_worker.py tests/test_issue_84_eval_only_retry.py \
  tests/test_orchestrator_and_worker_s2.py
make validate
python -m pytest -q tests/test_trajectory_backend_integration.py \
  tests/test_trajectory_observation_integration.py -m trajectory_local
```

新状态机测试在 SQLite 和独立 PostgreSQL schema 上分别运行，覆盖非幂等/幂等的同代入队、零业务许可/调用、单 Attempt、Checkpoint 损坏/过期、旧租约拒写、当前租约不可替换响应、取消前/评测期间取消、恢复期间任务/租约中断。另有两项正式 Collector 回归，禁止调用 Reader Factory，以验证原截止与保守 wire 预留。

真实消费者将原五种 native OTel/Pydantic AI/HTTP-only/8s late_all/未批准来源场景分别运行正常、采集前中断、采集终态后中断三条路径，断言同 Attempt、原 Checkpoint 时间/摘要、原 deadline 和原评分；终态恢复设置 Factory 禁止调用，避免在线最新 Trace 替换历史输入。恢复消费 Reconciler 新投递的队列消息，原中断消息可成为重复投递并被正常 claim fence 拒绝。

2026-10-14 核验（基于 PR #176 `a0853d4`，对应本批差异）：

| 门禁 | 实际结果 |
|---|---|
| TDD | 初始 7 项实际 RED→GREEN；评测期间取消和无效 Attempt 残留分数分别确认 RED；审阅指出的调度前后清理 Checkpoint 再次调用 Agent 均实际 RED→GREEN，未放宽断言 |
| 最终恢复回归 | 17 个状态机场景分别运行 SQLite/PostgreSQL，加 2 项正式 Collector 原 deadline/保守 wire 回归，共 36 项，包含在最终定向门禁内 |
| 独立 PostgreSQL 定向 | 最终修复后 885 PASS、零跳过，29.70s；独立 loopback Docker PostgreSQL，退出时核验 owner 后清理；测试独立 schema |
| `make validate` | 最终修复后全部 8 步 PASS：1645 Python、847 Console；67 个真实集成用例单独运行；Token lint、生成契约无 drift、Compose 通过 |
| 完整真实门禁 | 67 PASS、零跳过，673.60s；在审阅修复前启动，含 90 次来源资格校准、15 项正常/恢复消费者及原兼容用例 |
| 修复后真实消费者 | 15 PASS、零跳过，101.37s；同五种来源场景的正常/采前中断/终态恢复，原 12s/8s 合同不放宽，终态恢复禁止 Reader Factory；有限缺 Checkpoint 修复由两库故障注入证明 |
| 独立审阅 | 整批一次只读审阅 Critical 0、Required 1、Optional 0；主代理接受清理 Checkpoint 后不得 Agent 回退，补实际 RED→GREEN 后解决；无二审 |

定向 PostgreSQL、完整真实与修复后消费者的 JUnit 均 errors/failures/skipped=0。日志/JUnit/真实观测元数据按 `t1-recovery-final-v2-pg-targeted`、`t1-recovery-final-v2-validate`、`t1-recovery-initial-real`、`t1-recovery-final-auto` 前缀保存在交付执行记录；CI 实时真实门禁会另产出 artifact。初次 PostgreSQL 测试夹具事务契约和真实恢复队列消息选择问题已修正；中间门禁失败保留，以上仅计最终成功证据。既有 `datetime.utcnow()` 与 Console chunk 警告未在本批处理。

本批不宣告 S41-10、T1 或全 S4 完成。eval-only Adapter、Snapshot 保留/资格、规则与 Gate 的验证仍须继续；Cloud 未运行，工作流与凭据由 #162 所在任务负责，不将占位检查当作实际 Cloud 验收。
