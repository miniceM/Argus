# T7 / S4.5 验收记录

核验日期：2026-10-14。基线：T6 `d53d541a5d11b9fde4cfa383166f0ac7fd491e28`，实施汇入 PR #181。

本记录区分确定性阶段 A 与依赖 T8 Judge 的阶段 B。**T7 整期及完整 S4 尚未验收完成**；发布 CI 和 Judge 准入未完成前，不关闭 Issue #132。

## 证据与复现

- [实施与 RED/GREEN 记录](gate-progress.md)、[稳定进度回执](gate-progress-receipt.json)。
- [真实 Snapshot / Score 回执](real-projection-receipt.json)：86 项真实集成通过，四场景 × 两 revision，40 个不同的远端 Score ID；同 generation、生产 Outbox 同步与历史原文不变均由测试断言。该次执行的 [99 个源文件指纹](real-source-manifest.json) 早于比较专项修复，不作为最终发布 HEAD 的替代证据。
- [Console 冻结观测截图](frozen-gate-observations.png)：Snapshot 键盘切换、null/0、Axe 与小屏验证见 Playwright 回归。

在安装项目依赖、准备隔离 PostgreSQL 并设置 `TEST_POSTGRES_URL` 后运行：

```bash
python -m pytest -q tests/test_trajectory_comparison.py tests/test_trajectory_required_evidence.py tests/test_issue_86_comparability.py tests/test_api_results_s3.py tests/test_trajectory_release_gate.py tests/test_issue_7_release_gates.py --disable-warnings
python -m pytest -q tests/test_trajectory_snapshot_outbox.py tests/test_trajectory_projection.py tests/test_trajectory_storage_schema.py --disable-warnings
make validate
make console-e2e
make trajectory-test
```

第一组修复后的专项与兼容回归为 **251 passed / 0 skipped**；其余命令结果按进度回执记录的源码版本与运行范围解释。缺 PostgreSQL、Docker 或真实服务而跳过的结果不算对应验收通过。

## S45 逐项矩阵

“本地已验证”表示对应断言在当前阶段已有证据，仍需最终提交与发布门禁；不表示整期完成。

| ID | 当前状态 | 断言与证据 |
|---|---|---|
| S45-01 | 本地已验证 | `test_trajectory_required_evidence.py` 验证 legacy schema、缺 discriminator / 半合同与损坏 Snapshot；新 Gate 显式使用 trajectory 引擎，历史记录不补算 |
| S45-02 | 本地已验证 | required PASS + optional skipped / failed / no_result 保持 COMPLETE；optional 诊断仍保留，relative Gate 可消费 |
| S45-03 | 本地已验证 | required 缺失、非法值、错误 binding/provenance、failed/skipped/no_result 进入 INCOMPLETE / DIAGNOSTIC |
| S45-04 | 本地已验证 | `test_proven_false_is_complete_evidence_but_gate_refuses_release`：完整 false 属于测量证据，Snapshot COMPLETE，Gate 拒绝 |
| S45-05 | 本地已验证 | Gate 双比率的 null；Console 单测及 E2E 保留 null/0，零分母不作 PASS，Baseline 保持 COMPLETE 资格 |
| S45-06 | 本地已验证 | `test_six_case_two_required_rule_denominators_are_cases_not_measurements`：4 PASS、1 FAIL、1 UNKNOWN，N=6，比率 4/5、5/6；Summary/Gate/Console 同源 |
| S45-07 | 本地已验证 | required UNKNOWN 保留在全量 Case 分母，Gate UNKNOWN / releasable=false；optional coverage 不替代 required coverage |
| S45-08 | 部分完成，阶段 B 待验 | 阶段 A 已验证 critical FAIL / UNKNOWN 与关闭阻断拒绝、optional critical 拒绝；single-source Judge、联合校准与准入须在 T8 实施后补验 |
| S45-09 | 本地已验证 | `validate_baseline_snapshot` 的 COMPLETE/DIAGNOSTIC 判定、实际 Artifact 重验、损坏引用拒绝；历史原文保持不变 |
| S45-10 | 本地已验证 | 真实持久化制品两侧只改 QualityPolicy，formal unavailable / QUALITY_POLICY_CHANGED |
| S45-11 | 本地已验证 | 策略不匹配、Baseline 引用失效及任一 Snapshot DIAGNOSTIC 时行均为 DIAGNOSTIC_ONLY；不标 FORMAL |
| S45-12 | 本地已验证 | `test_changed_trajectory_contract_enters_measurement_and_withholds_formal`：只改采集合同，Measurement 改变、其他两层摘要不变，formal withheld |
| S45-13 | 本地已验证 | `test_different_owned_trajectory_digests_are_formally_comparable`：同合同不同实际 Artifact/result digest 可正式 UNCHANGED，两侧受控加载成功 |
| S45-14 | 本地已验证 | Baseline 缺 Artifact / 跨 Attempt 返回 withheld 稳定码；Candidate 对应损坏为 409。双方共同缺失 Case 仍为 NOT_COMPARABLE，required_cases=2、coverage=0.5；损坏容器稳定 409 |
| S45-15 | 本地已验证 | canonical `(item_id,snapshot_id,binding_id)` Score key；同输入不重复入队，同 generation 多 revision 并存。旧任务唯一性、FK、租约回收和真实双 revision Score 保留见 Outbox 测试及真实回执 |
| S45-16 | 本地已验证 | `test_projection_outage_retries_same_score_and_preserves_frozen_quality`、missing Artifact、retarget/type 篡改回归；投影故障不改 Snapshot/Gate，远端确认后才 SYNCED |
| S45-17 | 本地已验证 | legacy Score key、未标记历史 Snapshot 投影路径、旧 Gate 响应、summary 缺字段及已有幂等 Launch/Gate 原样返回；无历史回填 |
| S45-18 | 本地已验证 | 修复后 `make validate` 2322 项通过，`tests/test_expected_pass_rate.py` 保持原断言，Demo v1 2/6、v2 6/6 |
| S45-19 | 本地已验证 | 修复后 `make validate` 的 export / api:generate 无 drift，OpenAPI 与 Console 类型同步 |
| S45-20 | 待发布验收 | 比较修复后 `make validate` 及合并主线兼容复验、最终 HEAD 的适用 CI 均须通过；远端旧 T6 真实集成超时取消不算通过，Cloud 按策略跳过不算真实验收 |

## 决策与剩余工作

不完整 Snapshot 的逐 Case 结果仍可诊断，但不标正式依据；两侧同时缺失 Case 不得缩小 required 分母。Baseline 容器损坏无法安全诊断时返回稳定 409，容器合法而 Artifact 失效时保留 withheld 与 DIAGNOSTIC_ONLY；均不回写历史。

剩余：同步主线并验证、提交与发布 CI、T8 Judge 准入及阶段 B 复验、T9 全部 89 项整体验收。比较专项不改变冻结质量结果，真实投影旧回执继续按其原源码指纹解释。
