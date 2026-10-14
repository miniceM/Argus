# S4.1 不可变轨迹证据仓储

关联 #128，依赖 PR #169；核验日期 2026-10-13。

`trajectory/evidence.py` 复用已发布的 `017` 表结构，实现 `persist_trajectory_evidence(session, trajectory, contract)` 与 `load_trajectory_evidence(session, ref, *, scope, contract)`。调用方拥有事务，仓储不提交、不修改 Item 结果、不读在线 Trace、不调用 Agent。它提供真实制品引用，尚不启用正式 Binding、Acquisition READY 或 Snapshot 资格分支。

## 写入与读取边界

- 写入先验证类型、全部 provenance、合同/profile、最终字节预算、获准内容、引用与能力声明，再校验 DB 中的 Launch/Item/DatasetItem/Attempt/generation/profile。冻结 checkpoint 的 Trace/Root/final Attempt 必须匹配；当前 generation 不一致拒绝新写入。事务锁保护 Item/Attempt 与原 checkpoint 读取，Worker 发布 checkpoint ref 时仍须沿用后续 lease/CAS 门禁。
- PostgreSQL / SQLite 使用指定唯一身份的 `ON CONFLICT DO NOTHING`。冲突后重新读取并完整校验，不覆盖旧 payload 或 created_at；原行损坏不能被幂等重写“修复”。证据变化保留新旧两份独立制品。
- 读取验证 Ref、表列、payload scope、DB 所属身份、全部 provenance、canonical 字节与重算摘要。读到缺失、删除、损坏、身份或契约不符均返回安全原因码，不回退到最新 Langfuse。
- 历史读取核对原 Attempt 的 generation，不要求等于 Item 当前 generation；不依赖 checkpoint 是否过期或已删除。沿用 artifact 的既有 RESTRICT 外键及 Snapshot 保留约定，不增加独立 TTL 或清理任务。
- scalar 查询与 `no_autoflush` 避免只读入口隐式提交调用方脏 ORM 状态，也不覆盖其内存修改。数据库异常只暴露稳定 `STORAGE_UNAVAILABLE` 原因，事务回滚由调用方负责，不输出 SQL 参数或内部异常文本。

内容校验不把 `*_redacted` 字段名当作脱敏证明：重新核对 allowlist 投影、参数 visibility、受控 name/tool/workflow 身份及仅含安全字段的终结协议。即使调用方重算了正确 SHA，未获准字段、被标 REDACTED 却仍带值、额外原始 metadata 也不能保存。warnings 仅允许当前内部算法的稳定原因码集合；当前来源未定义 error_code 映射，因此拒绝非空 error_code，错误消息不携带原文。`$.terminal_inventory.observed_events` 为仓储保留协议，result_paths 不允许投影该路径、其父路径或子路径；同名业务 account_count 仍按普通 allowlist 保存。精确 Decimal 保存在 canonical TEXT 中，不改变全局 JSON serializer。

## 终结覆盖的可重算映射

原 `StandardTrajectory` Step 不含业务 event ID/sequence；只保留 terminal 自报清单不足以从冻结制品核对覆盖。Assembler 现为获准 terminal summary 补入 `terminal_inventory.observed_events`，每项仅含已规范化的 `step_id/event_id/sequence`。它包含 terminal 自身，按序号/Step ID 稳定排序；最终字节裁剪时只保留实际存在的 Step 映射。没有原始 Observation 或 metadata。

`inventory_complete(trajectory, contract)` 按冻结 profile 排除已声明且形态一致的结构包装，其余非根业务 Step 必须具有 workflow；它按 workflow 核对映射覆盖、唯一事件、连续序号、count/set/final 及 terminal 自身位置，能重算冻结的 inventory 覆盖。它不证明静默或分页过程，不能把 PARTIAL 制品改成 COMPLETE。OBSERVED_COMPLETE 声明必须通过这项核对及 required/source 能力校验，缺口不能因“摘要正确”被接受。

该补充进入最终语义摘要；source/mapping/redaction/公开 DTO 不变。正式轨迹制品与 Binding 尚未启用，后续 qualified 工厂必须冻结包含映射的组装算法版本；不改写已有历史内容。调整成本是内部终结协议读取方与算法身份变化。

## 验证与后续

实际 RED：缺少安全 event→Step 映射、缺失 evidence 模块、更低 artifact 预算误报 redaction 错误；同名普通业务结果误作协议；完整度遗漏无 workflow 的业务 Step；reason 形态文本可进入 warnings/error_code；合同可投影保留映射路径。均经过最小修复并重新验证，未放宽断言。

最终证据：

- 34 项 Assembler、40 项非 PostgreSQL 仓储测试通过；另 1 项隔离 PostgreSQL 并发写入测试验证四个调用方只得到一个不可变 Ref。隔离 PG 定向门禁共 455 项通过，JUnit 0 failure / 0 error / 0 skip，容器按 owner label 清理。
- `make validate` 八步通过：1399 项 Python、847 项 Console，契约生成零 drift、Token lint、构建与 Compose 校验通过；40 项真实集成由单独门禁执行。
- 真实 Langfuse 集成 40 项通过，JUnit 0 failure / 0 error / 0 skip，103.66 秒。原生 OTel / Pydantic AI / HTTP-only 各 10 条生产 Reader→Normalizer→Assembler→真实仓储保存/读取链，30 次 canonical 精确往返；保持 20 PARTIAL / 10 ROOT_ONLY，不伪造分页/静默完成证明。
- 参数四状态、幂等/新 revision、调用方事务回滚、scope 错配、旧 generation 拒写/历史可读、TTL 解耦、缺失/损坏不回退与不修复旧行、隐私、只读不 flush、数组 INVALID、已冻结结构包装形态均有断言。
- 单次独立整切片审查：无确认 Critical；两项 Required 已由主代理通过实际 RED→GREEN 修复；Optional 的保留路径冲突通过明确禁止业务投影及回归处理。审查为只读、未代跑测试，无第二轮审查。ruff 通过。

```bash
python -m pytest -q tests/test_trajectory_evidence.py tests/test_trajectory_assembler.py
make trajectory-test
make validate
```

下一步接通生产 Acquisition 的 deadline/分页/静默证明与 checkpoint 状态保存、lease/generation CAS 和恢复。旧 Manifest/Snapshot 语义保持，整套 S4 与相关 issue 仍未完成。
