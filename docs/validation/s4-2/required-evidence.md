# S4.2 required 证据与正式 Snapshot 资格

核验日期：2026-10-14。关联 #5、#129；在汇总 [PR #181](https://github.com/miniceM/Argus/pull/181) 中延续原 PR #180 的[正式四规则执行](frozen-rule-execution.md)，实施 [T4](../../plans/2026-10-13-s4-trajectory/08-implementation-plan.md#8-t4修复-optional-与正式证据资格断层)。

## 三个独立结论

`eval_status` 保留运行诊断；`required_evidence_status=COMPLETE|INCOMPLETE` 按冻结 QualityPolicy 的 required 集合计算；质量结论保持 PASS/FAIL/UNKNOWN。成功返回 false 是完整测量与已证违规。

| required 测量 | optional 诊断 | Snapshot / Gate |
|---|---|---|
| 全部有值、符合 Binding provenance，质量 PASS | failed、skipped 或 no_result | 保留诊断；Snapshot COMPLETE，可作 Baseline；符合 ReleasePolicy 时 Gate PASS |
| 全部完整，其中已证 false | 任意 | Snapshot COMPLETE，可作比较 Baseline；质量门禁 FAIL |
| 缺 Binding、缺结果、failed/skipped/no_result、缺值、类型错误、重复结果或 provenance 失配 | 任意 | INCOMPLETE / UNKNOWN；Snapshot DIAGNOSTIC，不能作正式 Baseline 或发布 |
| required 轨迹结果有值，但冻结引用不可用或不匹配 | 任意 | 不信任单独的成功值；冻结为 INCOMPLETE / UNKNOWN |

正式资格还要求执行成功、质量已决定及全量用例覆盖。缺失数据库用例仍计入冻结 Dataset 的分母。新契约的 `evaluation_coverage` 为必要测量完整且已决定的用例数 / 冻结用例总数；`evaluator_error_count` 继续记录 optional 异常。

## 冻结与事务

新创建且选择轨迹 Binding 的 Manifest 仍为 `1.3`，额外冻结 `evidence_contract_version="trajectory-1.0"` 与 `comparison-v2-required-evidence`。新聚合摘要包含 required 完整度、全量分母、required 测量比较和 optional 诊断隔离；资格/聚合口径变化不混入 Measurement digest。

沿用现有 JSON 列，无新增表或迁移：共享 summary 函数校验策略、Binding 和七个 provenance 字段，把完整度随 `quality_evaluation` 写入 Item；正常执行与 eval-only 共用。Snapshot Writer 再校验 typed 记录及冻结轨迹输入，在同一事务保存 `manifest` 的 discriminator/聚合合同、`items` 的版本/完整度/逐规则决策及 `summary` 的完整/缺失计数，随后写 Run Score outbox。计算、真实 JSON 序列化、outbox 或提交失败均整体回滚。

required 轨迹资格只接受受控 Loader 产生的 `EvaluationContext.trajectory_input`；单独构造成功测量和完整格式引用仍不能使实时 Item 成为 COMPLETE。恢复后的合并结果同样携带已验证输入。Snapshot Writer 显式传入当前 Session；Reader、Baseline 和 Gate 在所属 Session 中复用冻结 Loader，检查引用结构、Item/Attempt/generation/trace/root 归属、实际 Artifact 存在性、制品/合同摘要与完整度。没有所属 Session 的 required 轨迹快照不能获得未核验的正式资格；只有 required 答案规则时不强制要求轨迹制品。

Reader 不修补字段。版本、Binding/策略/聚合摘要、结果 provenance、冻结引用、计数或 checksum 不符即 fail closed；手工缺字段并重算 checksum 也不能绕过。制品删除、损坏或错配后，保留原始 Snapshot JSON 和历史 `evidence_state`，拒绝当前正式使用。公共读取/手动冻结返回有限 `409 EVIDENCE_CONTRACT_INVALID`，Baseline 拒绝，Gate UNKNOWN。Manifest 为 null、items 非数组或包含 null、summary/comparison 形状错误同样收敛到有限错误；Gate 不在校验失败后继续解析损坏内容。

用例 API 新增 nullable `required_evidence_status`，评测前和重评中为 INCOMPLETE；`quality_evaluation` 与 Run Summary 同时暴露版本和完整度。OpenAPI/Console 类型同步，诊断界面留给 T5。历史 `ResultSnapshotDetail.releasable` 表示正式证据资格，实际发布以 Release Gate 的 decision/releasable 为准。

## 比较与历史

新口径下 optional 失败不令用例不可比较，也不会以缺失 optional 数值制造回归；诊断仍保留，required 数值、质量与 critical 违规继续决定比较/Gate。Baseline 资格来自固定 Snapshot，不依赖历史 Launch 的当前状态。

`1.0/1.1/1.2` 及未冻结新 discriminator 的旧 `1.3` 保持原判定，历史 optional 异常仍为 DIAGNOSTIC。不回填、不迁移、不重写旧结论；采用新语义需新 Launch。跨新旧聚合契约的正式比较报告合同变化。内部采集校准继续显式 opt-in，保持原采集协议。

## 验收

```bash
python -m pytest -q tests/test_trajectory_required_evidence.py tests/test_issue_83_quality_policy.py tests/test_issue_85_snapshot_revision.py tests/test_issue_86_comparability.py tests/test_issue_7_release_gates.py
python scripts/export_openapi.py
pnpm --dir services/console api:generate
make validate
python -m pytest -q tests/test_trajectory_rule_integration.py -m trajectory_local
make trajectory-test
```

定向测试同时执行 SQLite 与隔离 PostgreSQL；缺少 `TEST_POSTGRES_URL` 的开发 skip 不计正式验收。真实来源覆盖正常执行、采集前/READY 后恢复、eval-only、完整违规及退化 UNKNOWN，并新增两框架 optional no_result→required COMPLETE→Baseline/Gate PASS。使用已校准本地 Langfuse，无 Cloud 依赖。

实际 RED：资格/半契约 17 项、缺 discriminator 1 项、真实消费者 1 项、冻结引用不可用 1 项、缺用例分母 1 项、公共 HTTP 2 项。独立审查为 Critical 0 / Required 3；补修测试实际 RED 为 10 failed、2 passed（另 12 个 PostgreSQL 参数因该 RED 运行未提供隔离数据库而跳过）。导入/夹具错误单列，不计行为 RED。完成一次 Required 修复，不进行重复审查。

最终 PostgreSQL 定向回归 1190 passed、0 failed、0 skipped；其中资格矩阵 SQLite 49 + PostgreSQL 49。`make validate` PASS：Python 1894 passed、0 skipped，84 项真实采集由独立门禁执行；Console 52 files / 847 tests passed，Token lint、typecheck、build、契约同步及 Compose 全部通过。`make console-e2e` 124 passed，独立测试端口已释放；`make trajectory-test` 84 passed、0 failed、0 skipped，785.05s，含 90 个重复校准样本与两框架 optional no_result 完整资格闭环。

版本、复现命令、断言、逐文件 SHA-256、JUnit/日志摘要及三份真实来源矩阵摘要见 [机器可读验收记录](required-evidence-receipt.json)。原始日志、JUnit 与 metadata-only 制品保留在本任务验收目录；校准字段不保存业务原文或凭据。

本批之后继续 T5 诊断、T6 其余四规则、T7 轨迹 Gate/投影及 T8/T9 Judge/整体验收；不关闭 #129 或完整 S4 目标。
