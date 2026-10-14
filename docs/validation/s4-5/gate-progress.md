# T7 Gate 冻结观测：实施进度

核验日期：2026-10-14。当前为基于 `d53d541a5d11b9fde4cfa383166f0ac7fd491e28` 的本地未提交增量；**T7 尚未验收**。本记录不替代 20 项 S45 标准及最终 HEAD 的完整门禁。

## 已实现合同

- Gate 始终从固定 Snapshot 重算 Case 聚合，返回 `decided_pass_rate`、`decision_coverage`、`unknown_count`、`critical_failure_count`、`required_case_counts` 与 `evidence_contract_version`，与 ReleasePolicy 是否选择对应 metric 无关。
- `required_case_counts` 的 `total/passed/failed/unknown` 以 Dataset Case 为单位；两项比率分别使用 PASS+FAIL 与 Dataset 全量分母，未决 Case 不冒充 FAIL，无已决 Case 时通过率为 null。
- 新 schema 1.3 trajectory Gate 使用 `trajectory-release-gate-v1`，API 幂等 request digest 使用同一引擎版本；历史引擎仍为 `release-gate-v3`。历史已保存结果不补算新增字段，读取时缺字段为 null。
- 新轨迹 Gate 冻结要求 `block_critical_failures=true`：API 否则返回 409 / `TRAJECTORY_CRITICAL_BLOCK_REQUIRED`；领域计算返回 UNKNOWN 且不可发布。旧策略保持既有开关语义。
- `trajectory_coverage` 使用 Dataset 全量 Case 分母，只计受控 Loader 校验通过的冻结轨迹；旧合同、零 Case 或脱离校验 Session 的对象为 null。optional 轨迹缺失可为 0，同时 required 已决覆盖为 1；轨迹完整不替代 Dataset/Agent 发布身份。
- Summary API 返回相同 Case 计数与冻结轨迹覆盖。Console 在门禁与版本对比页分别展示双比率、轨迹覆盖、UNKNOWN、critical 和必需证据状态，保留真实零，未知为 `—`，只展示当前 Launch/Snapshot 的观测。
- OpenAPI 与 Console 生成类型已同步。

## 实测

领域 RED：8 failed；API critical 策略 RED：2 failed、2 passed。修复后执行：

```bash
python -m pytest -q tests/test_trajectory_release_gate.py tests/test_issue_7_release_gates.py --disable-warnings
python -m ruff check services/eval-runner/app/release_gates.py services/eval-runner/app/api_release_gates.py tests/test_trajectory_release_gate.py
python scripts/export_openapi.py
pnpm --dir services/console api:generate
```

使用隔离 PostgreSQL 17 与 SQLite，GREEN 为 **67 passed / 0 skipped，3.19s**；Ruff 通过。测试覆盖三态比率、单 metric 策略、critical 冻结拒绝、API 幂等及持久读取、引擎身份摘要，以及现有发布门禁兼容性。后续 coverage RED 为 6 failed，Summary API RED 为 2 failed；追加覆盖修复和 Manifest 损坏兼容后，Gate / 既有 Gate / required evidence 回归 **173 passed / 0 skipped，23.73s**。6 Case、2 required rules 的专项另在 SQLite / PostgreSQL 实测 **2 passed**，证明 4 PASS、1 FAIL、1 UNKNOWN 的比率为 4/5 与 5/6，分母不按 12 次必需测量放大。

Console 新增单测 RED 为 2 failed / 1 passed，GREEN 为 3 passed；全量 Vitest **861 passed**，typecheck、build 与 Token lint 通过；完整 Playwright **129 passed，1.2m**。新 E2E 覆盖 Snapshot 键盘切换、null/0 显示、Axe 和 390px 无横向溢出；[冻结门禁观测截图](frozen-gate-observations.png) 已实看确认可读。

2026-10-14 远端 run 38513570098 attempt 2 的 annotation 明确报告 `The job has exceeded the maximum execution time of 20m0s`。本地将真实集成 Job 的有限预算从 20 分钟改为 40 分钟，并增加 `-vv -x --durations=10` 和无缓冲输出；CI workflow 回归 **11 passed**。该修复尚未发布，远端结果仍为 CANCELLED，不能计为通过。

当前源文件指纹见 [回执](gate-progress-receipt.json)；完整本地质量门禁、真实 Outbox 复验与独立审阅仍在进行，回执将在稳定后刷新。

## 后续验收（下文记录本轮新增证据）

- 比较专项修复后的最终本地门禁、逐项 S45 验收及提交后的 CI。
- T8 Judge 与 T9 整体验收继续在 PR #181 实施；本记录不表示完整 S4 已交付。


## 审阅修复与最终本地门禁

- 新 trajectory Launch 拒绝 `required=false, critical=true`，但拒绝规则位于已有幂等请求解析之后；历史同 payload/key 返回原 Launch，新 key 拒绝。新 trajectory Gate 的冻结边界也拒绝该配置，旧 1.2 配置保持原合同。
- Gate API 对损坏的 Manifest/comparison 稳定返回 409，包括 legacy 1.2 的 `comparison=null`；历史已保存 Gate 仍优先按原幂等结果返回。
- 新指标类型通过 wrap serializer 保持历史 summary 的缺字段原样，不把未存储指标补造为 null 或零；明确保存的 null 和真实零继续保留。
- migration 017 升级回归的精确增量序列追加 019，原有历史数据和幂等断言保留。

审阅修复 RED：8 failed / 2 passed；二次历史兼容 RED：3 failed。合并 GREEN **176 passed / 0 skipped，9.46s**；只读独立审阅已复核上述缺陷闭环。

最终隔离 PostgreSQL `make validate`：**2288 passed / 0 skipped / 86 real deselected，148.81s**；Console **861 passed**，Token lint、typecheck、build、契约同步与 Compose 均通过。此前完整门禁的 3 项失败（历史缺字段和升级序列）已修复并纳入最终结果。

生产 Outbox 的两框架受影响实测 **2 passed，155.43s**；随后追加同 generation 两个 Snapshot 的真实 Score 保留回执断言，完整真实门禁终态为 **86 passed / 0 skipped / 0 failures / 0 errors，949.95s**。四个场景分别保留两个 revision，共 **40 个不同的实际 Score ID**，均经真实 Langfuse Score API 核验 Snapshot metadata、生产 Outbox SYNCED、同 dispatch generation 及冻结结果不变。详见 [实际投影回执](real-projection-receipt.json) 和 [该次执行的源文件指纹](real-source-manifest.json)。

该次完整真实运行早于下述比较资格修复；不得把旧指纹的结果当作最终发布 HEAD 已验收。本阶段仍需逐项核验 S45 矩阵和发布 CI；Judge 的校准与准入在 T8 完成，整体验收在 T9 完成。

## 比较合同与归属专项（2026-10-14）

新增 `tests/test_trajectory_comparison.py` 在 SQLite 与隔离 PostgreSQL 使用真实持久化 Artifact，并通过生产受控 Loader / required contract 验证。旧夹具缺 Dataset/Agent 身份的 UNKNOWN 断言保留；仅比较夹具在冻结前补全身份。

- 相同合同、不同轨迹与结果摘要仍为正式 UNCHANGED；Baseline/Candidate 各自引用保持自洽。
- 只改 QualityPolicy 则 `QUALITY_POLICY_CHANGED`；只改 Binding 测量参数或 trajectory 采集合同则 `MEASUREMENT_CHANGED`，行均为 DIAGNOSTIC_ONLY。
- Baseline 制品缺失或跨 Attempt 时运行级 withheld、行 DIAGNOSTIC_ONLY、Case detail 为 NOT_COMPARABLE / BASELINE_EVIDENCE_CONTRACT_INVALID；Candidate 同类损坏返回 409 / EVIDENCE_CONTRACT_INVALID。历史 Snapshot 原文不回写。
- 两侧共同缺失的 required Case 留在冻结 Dataset 分母，并作为 NOT_COMPARABLE 行返回；2 Case 中仅 1 Case 可比时 coverage=0.5，含 COVERAGE_INCOMPLETE。
- 独立只读审阅补出的 DIAGNOSTIC Snapshot 行 FORMAL 标记已修复：Baseline 与 Candidate 都为 COMPLETE 才能标 FORMAL。
- 损坏 Baseline manifest/items/summary/dataset 容器在列表与 Case 接口均返回稳定 409，避免无效结构进入诊断聚合。

语义 RED：比较资格/分母 **6 failed / 6 passed**；独立审阅项 **2 failed**；损坏容器 **16 failed**。修复后专项及兼容回归 **251 passed / 0 skipped，44.50s**；`make validate` 为 **2322 passed / 0 skipped / 86 real deselected，171.40s**，Console 861、Token lint、typecheck、build、契约同步和 Compose 通过。结果见稳定回执；[S45 逐项记录](t7-acceptance.md) 明确保留 Judge 阶段 B 与发布 CI 的未完成状态。

可复现命令：

```bash
python -m pytest -q tests/test_trajectory_comparison.py tests/test_trajectory_required_evidence.py tests/test_issue_86_comparability.py tests/test_api_results_s3.py tests/test_trajectory_release_gate.py tests/test_issue_7_release_gates.py --disable-warnings
```
