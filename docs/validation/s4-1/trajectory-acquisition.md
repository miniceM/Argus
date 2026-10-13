# S4.1 有界轨迹 Acquisition

关联 #128，依赖 PR #170；核验日期 2026-10-14。

`acquire_trajectory(scope, contract, *, client: ObservationReader)` 消费一轮只读分页并返回冻结 `TrajectoryAcquisition`。`AcquisitionRuntime` 由内部协调器注入事务工厂、原始 `started_at` 和共享 limiter；ContextVar 隔离调用上下文，入口精确核对 scope、contract digest 与所选 profile，Reader 不获得存储权限。

## 状态、预算与证明

- 未收敛返回 PENDING，尚无 EvidenceRef。原始截止为 started_at + 冻结 deadline_seconds；单调时钟预算与墙钟剩余时间取较小值，回拨不延长期限。重建 Runtime 使用同一 started_at，重新观察 quiet，不继承旧进程的静默证明。
- 每轮 page_size/max_pages 固定；检测游标回环，逐页计 wire bytes，整个 Runtime 重查共用 max_source_bytes 余额。轮次没有读完、容量/Step 截断、inventory 缺口均不能升级 COMPLETE。不能继续读时保留已验证的规范化证据，原始页只在当轮调用栈内存在。
- 静默集合采用无证明轨迹的语义摘要：到达顺序与等价重复不重置，语义变化重置。只比较完整分页结束的观测时刻，不把归一化/组装耗时算作观察时间；证明绑定当前完整 NormalizedObservations digest。完整还必须满足 terminal inventory、父闭包及 required/source 能力。
- 进程事件循环内全局四槽，并按合同遵守更低并发上限；槽等待计入原期限，每请求 timeout 不超过 request_timeout_seconds 或剩余读预算。取消释放上下文/槽/HTTP response，不吞 CancelledError；闲置 Runtime/limiter 不保留事件循环。
- 永久鉴权、响应/版本契约错误返回 UNAVAILABLE；429/5xx/timeout 在原期限内可 PENDING 重查。暂态故障仍保留已读页及此前可验证闭包，截止固化 PARTIAL；没有可信 Root 时持续服务故障到期为 UNAVAILABLE；截止点自身的超时保留先前有效 Root 或本轮已读页；截止返回的重复/空页不抹去先前完整快照，新事实或冲突仍安全降级，不变成“无工具”，也不发完整证明。没有可信 Root 不补造轨迹。
- 收敛的完整轨迹提前 READY；ROOT_ONLY/PARTIAL 等到读截止才固化。READY 必须来自真实 insert-only 仓储事务完成后的 Ref。存储错误/timeout 返回稳定不可用原因，不暴露内部文本、不补造 Ref。

读截止后才能固化 ROOT_ONLY，因此最终保存独立受已冻结的单请求 timeout（最多 5 秒）限制。读查询不越原截止；等待读/保存各有上界。同步 SQL 线程不能由 asyncio 强制回滚，取消或保存等待超时后事务可能完成为未引用制品；调用方不得在取消后发布 checkpoint Ref，后续 lease/generation CAS 仍是必要门禁。

## 实施选择与边界

内部 Runtime 注入保证公开签名不增加旁路 scope/profile/timeout/存储参数。每调用处理一个分页轮次，协调器可保存 PENDING.deadline_at 并调度重查；当前 Runtime 只在内存保留脱敏候选。错误选择的调整成本限于内部 Worker 注入/调度与预算组织，不改公开 REST。

本切片提供采集入口与真实集成消费者，尚未在正式 Worker/Manifest 1.3 启用。checkpoint deadline 与累计预算的持久化、lease/CAS 发布、READY 引用恢复与 qualified 工厂/负载校准仍须继续实施；不声称重建 Runtime 单测已证明完整数据库恢复。原 Binding/Snapshot 与质量判定、CI 均保持原行为。

## 验证

实际 RED 包括缺失采集模块、预算耗尽丢证据、截止后完整证明、存储错误文本、闲置 limiter 保留、截止请求误抹 Root、下一页超时丢已读页、CPU 时间误作 quiet。独立审查提出暂态分页错误丢已读页，四项实际 RED 后修复并补健康轮次重新 quiet、冲突 Root 不挑赢家断言；真实回归另发现截止重复页误降 ROOT_ONLY，补四项确定性回归：重复/空页保留快照，新 Step 仍 PARTIAL，Root 冲突不挑赢家；两项实际 RED 后修复。修复后 46 项 Acquisition 与 40 项非 PG 仓储测试通过，ruff 通过。

真实测试经生产 Reader→Acquisition→Assembler→仓储读取：两框架各 10 次及 HTTP-only 10 次；额外覆盖真实 late_all/terminal_first，首轮 PENDING，沿用原 deadline 后获取完整终结清单并保存。消费链采用已冻结的测试限额（3/10 秒 deadline、0.05 秒 quiet、2 条/页），不将其作为生产 SLA。单次手工 Reader 消费仍 PARTIAL，只有实际 Acquisition 观察收敛才允许完整。

最终代码于 2026-10-14 复验：隔离 PostgreSQL 501 passed，JUnit 0 failure/error/skip（11.268 秒），容器按 owner label 清理；`make validate` 8 步 PASS，1445 Python / 847 Console，OpenAPI/schema 无 drift，Compose 校验通过。独立真实采集 40 passed，JUnit 0 failure/error/skip（144.536 秒）。field-matrix 核对 32 条真实采集保存/读取：22 OBSERVED_COMPLETE（native 12、Pydantic 10），10 ROOT_ONLY（HTTP-only）；两条延迟场景均首轮 PENDING、保留原 deadline。旧轮结果不替代本次最终证据。

PR #171 首轮远端 Python CI 在 50ms 墙钟期限的分页单测失败，真实采集 CI 通过。单测改为控制逻辑截止在第二页实际被 asyncio.timeout 取消时推进，并增加首查/取消阶段断言；保留原超时、PARTIAL 与三条已读 Step 断言，排除机器 CPU 速度对前置条件的影响。该调整未改变采集实现；带 coverage 的定向测试已通过，最新远端 CI 另核验。

```bash
python -m pytest -q tests/test_trajectory_acquisition.py tests/test_trajectory_evidence.py
make trajectory-test
make validate
```
