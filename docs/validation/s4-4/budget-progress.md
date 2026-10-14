# T8 跨 Worker Judge 预算账本

日期：2026-10-14。关联 #131；依据 [S4.4 §5](../../plans/2026-10-13-s4-trajectory/06-s4-4-judge.md)。本切片实现短事务账本及受预算约束的异步 Provider Adapter，**正式 Worker/recovery、模型能力准入及真实 token 上界仍待接入，S44-08 尚未整体验收**。

## 冻结限额与运行状态

追加 migration `020_trajectory_judge_budget.sql`，在既有 Launch 增加 nullable `judge_budget_state_json`。旧 Launch 仍为 NULL，旧 Manifest、Checkpoint、Artifact 和 Snapshot 不重写。SQLite/PostgreSQL 历史升级测试保留原数据、原摘要，验证新列 NULL 与重复迁移为空。

`FrozenJudgeBudget` 保存 Launch 的 call/input/output 总上限，以及每个 Binding 的 Definition 摘要和单次 input/output 预留上界。账本要求 Manifest 1.3 的 `judge_budget={profile,digest}` 严格匹配；没有冻结 profile 不启用。正式 profile 生成、测量摘要接线、全量 required cohort 最坏预算校验和输入 token 上界证明，仍属后续冻结与 Provider 准入实施，不能从任意文本字节数猜测 tokenizer 计数。

运行 JSON 只记录计数、状态、受控身份与租约，不保存请求正文、模型回复、rationale 或 API Key。key 固定为 `(item_id,evaluation_generation,binding_id)` 的摘要；调用还绑定 input/definition digest 与输出上限。已有 key 的身份变化拒绝，不能将同一 reservation 换成另一份输入。

## 事务和生命周期

`JudgeBudgetStore` 自持短事务，按 Launch → Item 顺序获取 PostgreSQL 行锁。已锁 Launch 为取消状态时，在等待 Item 锁前拒绝，避免与 Reconciler 取消收尾反向等待。其它终态孤儿修复的既有反序路径未在此切片重构；锁等待上限 2 秒、statement 上限 5 秒，存储失败不调用 Provider。校验真实 Item 归属、generation、租约 token/expiry 和运行状态。execution owner 要求 `running`，evaluation owner 要求 `evaluating`；有效 eval-only lease 可在执行失败的 Launch 上工作，不调用 Agent。

预留先提交，`SENT` 标记也先提交，再 await Provider；等待模型时没有开放的账本事务。两个独立连接竞争最后的 call/input/output 额度，最多一个成功。进程内 semaphore 不能代替此行锁账本。

- `RESERVED`：预留一次调用和完整 token 上界；重复 delivery 不能同时领取。新的有效租约可以接管未发预留，旧 receipt 失效，不重复扣费。
- `SENT`：保留最坏预留；不能通过取消退款或自动重发。
- `SETTLED`：可信 usage 在单次上界内，退回 token 差值，但调用次数仍记账。用量结算不表示质量结果已落库；没有持久质量结果可复用时，重复 delivery 仍为 `JUDGE_CALL_OUTCOME_UNKNOWN`。
- `SETTLED_UNKNOWN`：没有 usage，保持最坏预留；迟到的可信 usage 可结账，但不写质量结果。
- `CANCELLED`：仅未发预留可退款。新 key 需要空间时可淘汰这些未发占位、归还调用容量；不删除已发记录。key 总数不超过冻结 `max_total_calls`，旧 receipt 无法复活。

`BudgetedJudgeProvider` 在实际请求来源 seal 验证后接通账本与现有 Provider。取消期间 `to_thread` 不会停止已开始的事务，因此对 reserve task 使用 shield，重复取消下仍取回提交后的 receipt；finally 只释放未发预留。新租约接管或存储失联导致清理失败时，不覆盖 `CancelledError`。发送标记失败也退款。错误保持稳定原因码，不补零、不伪造 PASS/FAIL 或 usage。

## RED 与验证

- 初始 RED：**31 failed**，缺少账本模块/字段；最小实现后 **31 passed**。
- Owner 状态与输出上限 RED：**4 failed**，修复已完成 Item 可发送及 Provider 输出上限大于预留的问题。
- 取消与接管 RED：**2 failed**，修复旧 receipt 清理异常吞掉取消。
- 独立审阅问题的 RED：**8 failed**，复现 reserve 提交后取消、发送标记失败不退款、取消占位占满新 key；均增加回归并修复。
- 用量不等于质量结果落库的 RED：**4 failed**，所有已发状态重复投递统一返回 `JUDGE_CALL_OUTCOME_UNKNOWN`。
- 取消锁等待 RED：**1 failed / 61 deselected**，实际持有 PostgreSQL Item 锁、Launch 为 CANCELLING；修复前锁超时，前移检查后直接返回 `JUDGE_BUDGET_CANCELLED`。
- 最终六模块：**238 passed / 0 skipped**（21.29 秒），其中预算模块 **62 项**。覆盖真正 PostgreSQL 连接争抢、两种 DB 的租约/取消与双次取消、未知及迟到用量、历史升级，与来源准备、HTTP 生命周期、Checkpoint 的兼容性。
- 最终 `make validate` 8 步通过：Python **2555 passed / 0 skipped / 86 real deselected**（175.09 秒），Console **864 passed**；OpenAPI/生成类型、Token lint、typecheck、build 与 Compose 校验通过。独立复审未发现新的确认阻断。Reader 实测与校准摘要未变，本切片不重写原真实集成版本证据。
- 首次扩大回归有 **225 passed / 2 failed**：旧升级测试精确迁移列表未纳入 020。追加预期迁移，并新增旧 Launch 运行预算 NULL 断言，保留全部历史内容与幂等断言；上述最终六模块与完整门禁已重新通过。

测试命令：

```bash
pytest -q tests/test_trajectory_judge_budget.py tests/test_trajectory_judge_provider.py \
  tests/test_trajectory_judge_input.py tests/test_trajectory_storage_schema.py \
  tests/test_trajectory_evidence.py tests/test_trajectory_checkpoint.py
make validate
```

PostgreSQL 测试使用测试用 `TEST_POSTGRES_URL`，每项创建并清理自己的隔离 schema；未配置时的 skip 不计通过。本次使用隔离 PostgreSQL 17，全部执行。

## 后续实施

注册 `trajectory_judge` 执行器、冻结 Binding 与预算 profile、可靠 tokenizer/Provider 准入；在 Worker 和异步 recovery 中准备固定输入并调用此 Adapter，保留空目标集合、旧成功结果、generation/lease CAS 和完整 required 分母。还须完成 critical 独立来源、真实独立 holdout、T7 Judge 阶段 B 与 T9；当前测试不能替代这些产品和真实模型验收。
