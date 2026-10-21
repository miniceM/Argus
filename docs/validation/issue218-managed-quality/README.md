# Issue #218 真实联调验收记录

脚本：`real_gate_acceptance.py`（默认 dry-run；`--execute` 真跑，会产生模型费用，
使用隔离测试容器凭据，仅内存传递，不手写 Score，不改共享 Rule，结束时暂停
本次自有 Rule 并停止本进程服务）。

回查：`verify_score_readback.py --evidence-dir <dir>` 独立只读回查
`Scores V3 GET`，**不加 `source` 筛选**（API 重复回写也会被发现），断言每项
恰好一条 `source=EVAL` 行并与冻结采纳逐项一致。

## 冻结证据（`evidence/issue218-20261021t030256z/`，`receipt.json: PASS`）

| 场景 | Agent 输出 | Score 来源 | 质量 | Gate |
|---|---|---|---|---|
| pass | 真实 Flowise 模型/工具答案 | 真实 Langfuse Judge + Code（双 `true`） | pass | PASS（可发布） |
| fail | 合成错误 HTTP（`text=0`） | 真实 Langfuse Judge + Code（双 `false`） | fail | FAIL |
| missing | 同 fail | 暂停自有 Code Rule（模拟缺分），judge 有 / code 无 | unknown | UNKNOWN（不可发布） |

`request-audit.json`：Argus 零 Score 写回、零手动收集；仅暂停自有 Rule 的
Langfuse PATCH 清理。`independent-score-readback.json`：6 项回查 PASS。
`historical_snapshots_and_stored_gates_unchanged: true`。

## 已知局限（不得外推）

- 三套真实证据均为**单用例**（`total_cases=1`）：多用例分母正确性由
  `test_partial_run_keeps_all_cases_in_denominator` /
  `test_missing_case_record_stays_in_managed_denominator`（mock）覆盖。
- FAIL 是**合成错误输入触发的真实 Judge 失败**，非自然业务质量失败。
- missing 用暂停自有 Rule 模拟，非 Langfuse 自然缺分；真实迟到 Score 到达后
  新 revision 的 E2E 仅由 mock（`test_late_required_...`）覆盖。
- `receipt.json: source_commit` 对应代码提交；末提交仅加证据时
  `make validate` 需重跑，以最终 CI 为准。
