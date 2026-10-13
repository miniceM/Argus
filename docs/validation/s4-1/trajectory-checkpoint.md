# S4.1 / S4.2 采集 Checkpoint 事务边界

关联 #128、#5；依赖 PR #171，核验日期 2026-10-14。

`trajectory/checkpoint.py` 提供调用方持有 Session 的 `begin_trajectory_round`、`publish_trajectory_round`、`abort_trajectory_round` 与只读 `load_trajectory_checkpoint`。调用方必须先提交 begin 的预留事务，再进行源读取，最后另开事务发布结果；不得跨网络持有数据库锁。正式 Worker/Manifest 与恢复调度仍须后续接入，本次不启用 Snapshot 资格分支或改变质量判断。

## 持久化及恢复

- 追加迁移 `018_trajectory_acquisition_runtime.sql`，既有行保持 nullable 扩展。`trajectory_acquisition_json` 仍只保存公开 Acquisition；私有 `trajectory_runtime_json` 仅保存原开始时间、合同/来源摘要、预算整数、revision/轮次身份和候选 EvidenceRef，不复制原始 Observation 或完整 Trace。
- 原开始时间来自业务响应 checkpoint 的 created_at，截止固定为它加冻结 deadline。输出摘要、Dataset input/expected_output、Manifest 与 Binding provenance 均校验，所选 profile 必须匹配不可变 AgentVersion。初始化及续查不能接受新来源或重置截止。
- PENDING 的安全候选先进入同一个 insert-only 仓储，私有指针用于崩溃恢复；它不成为公开 PENDING.evidence_ref，也不能被用于宣称 COMPLETE。原截止过后，已验证候选可原样转 READY；没有可信候选为 UNAVAILABLE，不查询最新 Trace。
- 发布 `candidate=None` 明确清除候选指针，适用于新发现的 Root 冲突或无效。调用方确认旧候选仍有效时，须显式传入 `claim.candidate`；不能自动恢复冲突前的旧轨迹。
- READY 恢复重新加载并验证制品、引用、身份、合同、digest 与 completeness。缺失/损坏为明确 UNAVAILABLE，仅读取诊断，不修复或回写原 READY。轨迹指针读取不依赖输出 checkpoint TTL；后续评测消费仍须执行其独立输出保留/恢复检查。制品自身沿用 Snapshot 生命周期，不新增 TTL。

## 预算及并发

- 读取前预留全部剩余 source bytes；成功发布才按 Runtime 的累计扣减释放已知未使用量。Reader 已报告的用量采用实际 wire bytes；外层超时或显式取消未能取得报告时，保守扣满该请求剩余额度，因此账本表示预算扣减，不能当作精确网络流量指标。崩溃/取消不退未知在途预留，新租约不能恢复已扣额度。这个选择在故障时可能减少可查询数据、产生更多 PARTIAL/UNAVAILABLE；它确保跨进程不会反复获得新预算。后续逐页账本可改进利用率，但不能突破冻结上限。
- 全局锁顺序为 Launch→Item→Attempt→Checkpoint。运行态、active Attempt、generation、live lease、取消及 revision/随机 round_token 全部核对；同租约重复轮次 BUSY，旧轮次发布 STALE。锁后读取生产时间，制品校验完成后再核对租约和截止。
- 候选制品与状态更新同一事务，故障由调用方整体回滚；函数不 commit。READY 只能引用真实仓储制品，未知外部 reason 文本不能进入 checkpoint。取消 abort 只释放轮次标记，不发布结果或退额度。
- 锁查询发现相关 ORM 记录存在未提交改动时明确 TRANSACTION_DIRTY，保留改动供调用方处理；不自动刷新覆盖，不 flush 无关改动。

## 验证

实际 RED→GREEN 覆盖冻结输入、profile 与跨 Item 候选校验、预留篡改、截止后的 PENDING、制品校验期间租约/截止失效、未提交 ORM 变更与非法 Binding 形状。另验证崩溃预算、旧轮次、READY 损坏/删除、事务回滚、TTL 与新 generation 下历史读取。

单次独立审查的 Required 为显式取消后复用 Runtime 会重新获得未知在途额度；回归测试实际观察到 `0 != 8388608`，修复为先保守扣减再原样传播 `CancelledError`，再次消费不会调用 Reader。主代理同期覆盖外层 timeout 相同边界。共享槽释放使用独立新 Runtime 验证。Optional 的 lease token digest 最小化留待后续：当前原 token 仅存在私有数据库账本，已有 Item/Attempt 租约字段沿用；不进入公开 Acquisition/API、日志或轨迹制品。

真实测试在 Worker 保存业务响应后暂时阻塞评测，使用原 execution lease 完成 Reader→Acquisition→预留/发布→新 Session 恢复，然后放行原 Worker。沿用原成功评测、Agent 单调用/单 Attempt 与父链断言；不伪造运行态或新租约。两框架和 HTTP-only 的测试 deadline/quiet 为 3 秒/0.05 秒，它们用于消费链验收，不作为生产 SLA。

真实复验曾出现 HTTP-only 读取返回 PENDING 后、发布前截止到达，事务层返回 DEADLINE_EXPIRED。消费链现在使用同一 claim 和 Runtime 在已到截止后再次调用 Acquisition 收尾，断言累计扣减不变、结果不再 PENDING，然后新事务发布最终状态；不重新预留、不再读取源、不重新调用 Agent。HTTP-only 用例在已有可信候选后主动等待到原截止以确定性覆盖该竞态。正式 Coordinator 必须实现相同行为，保留 store 的前后截止检查。

2026-10-14 最终复验：相关非 PostgreSQL 测试 83 passed；独占 PostgreSQL 定向 JUnit 541 tests、0 failures/errors/skips，13.881 秒。`make validate` 八步通过，Python 1485、Console 847，OpenAPI/schema 无 drift、Token lint 和 Compose 校验通过；测试数据库按 owner label 清理。

最终真实 JUnit 43 tests、0 failures/errors/skips，151.467 秒；field matrix 记录 35 次真实制品往返，24 OBSERVED_COMPLETE、11 ROOT_ONLY。新增三个 profile 均为 READY、checkpoint_restored=true、original_worker_lease=true；两框架 OBSERVED_COMPLETE，HTTP-only ROOT_ONLY。此前 42/43 的失败轮次分别暴露消费链截止收尾及错误的故障注入点，已修正并以全量最终结果替代，不计为通过。

正式协调器还必须把持久化余额注入 Runtime、重新建立 quiet、处理取消与截止竞态，并接入 Manifest 1.3、恢复及 qualified 工厂/负载校准；本次事务边界交付不能代表 T1/T2 整阶段完成。

```bash
python -m pytest -q tests/test_trajectory_checkpoint.py tests/test_trajectory_storage_schema.py tests/test_issue_84_eval_only_retry.py
make trajectory-test
make validate
```
