# S4.1 / S4.2 生产采集协调器

关联 #128、#5，依赖 PR #172；核验日期 2026-10-14。

`trajectory/coordinator.py` 的 `TrajectoryCoordinator.run(scope, contract, client=..., lease_token=...)` 消费原业务响应 checkpoint 和调用方已有 execution lease，统一 reserve→Acquisition→publish→等待的循环。调用方持有 Reader 生命周期；协调器不调用 Agent、不创建 Attempt 或新租约。公开 `acquire_trajectory` 的 Reader-only 签名保持不变。

## 恢复与候选

- 深复制运行合同和 scope，校验实际 AgentVersion profile。第一次 begin 返回的原 started_at 和累计扣减注入 Runtime；后续余额必须一致，不恢复新预算。
- 新进程从空 quiet 状态重新观测；持久化候选只能支持已有事实的降级恢复，不能证明 OBSERVED_COMPLETE。原截止到达后沿用真实候选 Ref；历史 READY 仅读取校验，不读取源。
- 缺 Root、暂态源失败或崩溃耗尽预算时，保留仓储已校验的候选。观察到 Root 冲突、无效或错误 Trace 后明确清除候选；Normalizer 失败也保守失效，异常文本不持久化。永久源错误为 UNAVAILABLE。
- 截止在读取与发布之间到达时，store 仍拒绝过期 PENDING；协调器以同一 claim/Runtime 调用截止收尾并发布最终状态，不增加读取或重新预留。

## 数据库、取消与所有权

- 每次数据库操作独立 Session/短事务，经 `asyncio.to_thread` 执行，不占用事件循环；reserve 提交之后才调用 Reader，不跨源 IO 持有数据库锁。
- checkpoint 接收时钟函数，在获取锁后、制品校验后的发布前各自重新读取时间；锁等待或校验期间租约到期时拒绝预留/发布，制品与候选整体回滚。
- 同步事务等待受冻结 request timeout 限制；PostgreSQL 同时设置事务局部 lock_timeout/statement_timeout，不改变部署配置。取消/timeout 留弃用标记；同步操作在提交点前发现标记时整体回滚，不把迟到线程作为下一轮结果。已进入提交点的事务可能完成，调用方取消不返回 Ref；不撤销已提交历史，generation/lease/CAS 继续保护发布。
- 外部 task.cancel() 与可选取消 Event 都中止流程并原样传播 CancelledError；预先设置的 Event 不预留、不读取。Reader 返回后再次检查取消意图，防止快速源/数据库绕过 watcher。
- 已提交且仍有有效所有权的活动轮次调用 abort；未知在途预留不退。失去租约/数据库不可用时保留保守预留，不用清理覆盖新执行者。事务异常映射为有限 STORAGE_TIMEOUT/STORAGE_UNAVAILABLE，不输出原错误。

## 验收与后续

实际 RED→GREEN：模块缺失；预先取消仍发生读取；Normalizer 失败访问未赋值变量；调用方嵌套合同变更；Reader 返回时取消仍继续读取/发布。单次独立整切片审阅发现固定时间导致锁后及制品校验后复用旧租约时间，两个回归先失败，再改为时钟函数；另以真实 PostgreSQL 行锁验证等待期间过期后零预留、零源读取。新增用例覆盖 quiet 重建、预算种子校验、截止收尾、暂态候选保留、Root 失效、心跳、迟到事务回滚、数据库锁等待、竞争协调器与租约换主。

真实两框架和 HTTP-only 用例在原 Worker 响应 checkpoint 处暂时阻塞，使用生产 Coordinator 完成采集，再放行原评测。HTTP-only 在可信候选后主动等待到原截止，生产协调器承担收尾；保留成功评测、父链和单 Attempt 断言。

审阅 Required 修复后的最终实测：隔离 PostgreSQL 目标集 **573 passed**（JUnit 0 failure / error / skip，17.400s）；真实本地集成 **43 passed**（JUnit 0 failure / error / skip，152.609s）。三个来源的 `production_coordinator`、`checkpoint_restored`、`original_worker_lease`、`artifact_roundtrip` 均为 true；原生 OTel / Pydantic AI 为 OBSERVED_COMPLETE，HTTP-only 为 ROOT_ONLY。`make validate` 八步全部通过，含 **1517 Python / 847 Console**、Token lint、OpenAPI/schema 零漂移与 Compose 校验；未执行真实 Cloud。

正式 Manifest 1.3、Worker 自动启用、执行消息重投不重调 Agent、evaluation-only Adapter、qualified Reader 工厂/版本准入及负载校准仍须后续接入。本次不启用 Snapshot 资格或判断规则，不代表 T1/T2 整阶段完成。

```bash
python -m pytest -q tests/test_trajectory_coordinator.py tests/test_trajectory_acquisition.py tests/test_trajectory_checkpoint.py
make trajectory-test
make validate
```
