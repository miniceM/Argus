# S4.5 Trajectory Release Gate 实施方案

状态：**PR #136 已批准方案；T7 正在 PR #181 实施。T4 已交付 required evidence、Snapshot/Baseline 资格和比较合同基础；本期继续完成 Snapshot 级投影、门禁增量及整期验收，不表示 T7 已完成。**
核验基线：2026-10-13，`main@4b043739c63357ecc10131e1ba54bc1bc53d862a`。
关联：[Issue #5](https://github.com/miniceM/Argus/issues/5)、[#132](https://github.com/miniceM/Argus/issues/132)。
共同契约：[01 技术设计](01-technical-design.md)；总阶段见[完整实施计划](08-implementation-plan.md)，ID 对照见[验收矩阵](09-acceptance-matrix.md)。

实施记录（2026-10-14）：T6 已推送为 `d53d541a5d11b9fde4cfa383166f0ac7fd491e28`，其 CI 独立核验中。T7 首先验证 Snapshot 维度 Score 身份：新路径冻结 `(item_id, snapshot_id, binding_id)` 的 canonical SHA-256，旧 generation ID 保持原样。新增回归 RED 为 20 failed / 1 passed（新 API 尚不存在），最小实现后与旧投影回归合计 56 passed。此结果仅证明 ID 合同；尚未接入 Outbox，不作为 S45-15 整体通过。下一步追加现有 Outbox 的 nullable Snapshot FK/部分唯一性，并将固定 Snapshot 任务在冻结事务内入队。

## 1. 目标、现状与 Issue 修订

本阶段使冻结轨迹规则结果进入已有 Snapshot、Baseline、Comparison 和 Release Gate，并保留缺证据时的 UNKNOWN。
Issue #132 的方向与现有系统一致；实施应聚焦 trajectory-aware 证据完整性、required/optional 语义、轨迹 digest 归属和回归保护。
实施前应把本文 S45 验收项补入 Issue，并将已有通用机制标为复用项，避免把已实现能力误记为新交付。

| 通用机制 | 当前已有行为 | S4.5 增量 |
|---|---|---|
| UNKNOWN | QualityPolicy 把必需证据缺失归为 UNKNOWN；Gate 的 UNKNOWN 不可发布 | 轨迹 Judge/规则的 UNKNOWN 按 required 证据状态进入既有 fail-closed |
| Baseline | `baselines.validate_baseline_snapshot()` 要求 COMPLETE 和完整结果 | 验证 trajectory-enabled 1.3 Snapshot 依新必需证据状态正确分类 |
| 三层 digest | Measurement、Quality Policy、Aggregation/Comparison 三个 digest 已比较 | trajectory contract 进入 Measurement；不把单个 Case 的 `trajectory_digest` 当双方契约 |
| Snapshot | 不可变结果、revision、source digest、COMPLETE/DIAGNOSTIC 已有 | 1.3 明确 required evidence，使 optional skip 不污染 COMPLETE |
| Formal comparison | 合同不兼容时 `formal.available=false`，返回 `withheld_reasons` | 加入 trajectory evidence/ref 完整性与实际 required Case 覆盖检查 |
| 投影 | Langfuse Score 经 outbox 从 Snapshot 单向投影，score ID 可幂等重放 | 投影带冻结 root/trajectory ref；不从 Langfuse 回读来更改 Argus 决定 |

当前非轨迹 Manifest schema 为 1.2；已实现的轨迹启用配置使用明确的 1.3 分支，Snapshot 冻结 `evidence_contract_version="trajectory-1.0"`。
当前 Snapshot 分类会把任意非 succeeded `eval_status` 归为 DIAGNOSTIC；optional evaluator skip 可导致 `eval_status=partial`，即便 QualityPolicy 已 PASS。
当前 Comparison 有 `MEASUREMENT_CHANGED`、`QUALITY_POLICY_CHANGED`、`AGGREGATION_COMPARISON_CHANGED`；阈值改变会撤销正式可比性。
当前 Gate 要求 COMPLETE、Case 覆盖、全评测覆盖、Frozen identity 和 critical 证据；critical FAIL 可一票否决。[实现](../../../services/eval-runner/app/release_gates.py)
当前聚合已暴露 `decided_pass_rate` 与 `decision_coverage`；S4.5 要让 1.3 的二者及 Gate 使用的既有 `pass_rate/evaluation_coverage` 固定到完整 Dataset required Case 集合。
当前投影 key 是 Item + dispatch generation + evaluator；eval-only recovery 不增加 dispatch_generation，因此它不能单独隔离新 Snapshot revision。本期只为 trajectory 新路径增加 Snapshot 维度，旧任务与 Score ID 不改写。[投影实现](../../../services/eval-runner/app/langfuse_projection.py)

## 2. 不变条件与兼容策略

业务 Agent 不添加 Evaluation SDK；不改变 Agent DTO，不从 Agent 响应里的 `tool_calls` 推断真实轨迹。
Langfuse 保持 Trace/Observation System of Record；Argus 冻结最小证据制品并负责发布决定。
新 Launch 只有显式 trajectory Binding 时采用 schema 1.3；Manifest 1.0/1.1 legacy 与 1.2 分别保留已注册 Reader 和 Snapshot 判定。新资格 discriminator 在 T4 与 required_evidence_status/聚合合同原子启用，Artifact schema_version=1.0 本身不激活资格。
未知 schema 不得降级到 1.2 解释，也不得把缺轨迹当空轨迹；应返回不可评估/UNKNOWN 和稳定原因码。
历史 Manifest、Snapshot、Baseline 和 Gate 结果均保持不可变；不为它们补算 trajectory digest 或重建质量结论。
继续使用既有 QualityPolicy、三层比较合同、Snapshot、ReleasePolicy、Gate 记录、Outbox 和投影。
Demo Agent v1 `overall_pass=2/6`、v2 `overall_pass=6/6` 与 `tests/test_expected_pass_rate.py` 不修改。
现有断言不删除、不放宽；本阶段只增加新 schema 和轨迹路径的回归测试。

## 3. Required evidence 与 Snapshot 状态

schema 1.3 的每个 Snapshot Item 增加冻结 `required_evidence_status`：`COMPLETE` 或 `INCOMPLETE`；Snapshot payload 同时记录 `evidence_contract_version="trajectory-1.0"`。
该字段只由冻结 QualityPolicy 的 required Binding 集合、有效 typed value、status 和 provenance 计算，不使用 optional eval_status、执行状态或总体质量替代。执行成功与质量 PASS/FAIL 是 Snapshot 资格的独立条件。
所有 required 结果已产生、status=succeeded、值及 provenance 有效时 required evidence 为 COMPLETE，boolean false 也属于完整测量。
任一 required 结果缺失、failed/skipped/no_result、值无效或 provenance 不符时为 INCOMPLETE。完整性是逐规则所需证据的充分性：整体 PARTIAL 仍可支撑确证违规，不能对每条规则机械要求所有轨迹能力 COMPLETE。
已观察到禁止行为可以产生确定 FAIL；只要必需证据完整，失败仍是完整证据，Snapshot 可为 COMPLETE，最终是否发布由 Gate 决策。
optional 规则缺席、skip、failed 或 no_result 留在 typed results 和诊断区，但不降低 required evidence 完整度。
若 optional 结果本身被配置为 critical，则配置必须在冻结阶段拒绝或转为 required；采样结果不能充当 critical evidence。

分类伪代码：

```python
if manifest.schema_version in REGISTERED_LEGACY_VERSIONS:  # 1.0 / 1.1 / 1.2
    evidence_state = classify_legacy_eval_status_and_quality(items)
elif (manifest.schema_version == "1.3"
      and snapshot.evidence_contract_version == "trajectory-1.0"):
    validate_required_fields_and_aggregation_contract(snapshot)
    evidence_state = classify_all_required_items(items)
else:
    return unsupported_schema_unknown()
```

1.3 的 Snapshot `COMPLETE` 条件：至少一个 Dataset Case；每个 Dataset Case 已执行完成；所有必需 Evidence COMPLETE；质量结论均为 PASS 或 FAIL。
任一 Dataset Case missing、execution 未成功、required evidence INCOMPLETE 或 quality UNKNOWN，则冻结 `DIAGNOSTIC` 和具体原因。
`eval_status=partial` 仍用于运行诊断，但 1.3 COMPLETE 判定读取 `required_evidence_status`；1.2 则保留既有行为。
`source_result_digest` 是当前 Snapshot 的结果制品指纹，包含 required 状态、typed results、quality evaluation 与 TrajectoryEvidenceRef；相同输入仍幂等，相异输入生成新不可变 revision。
Snapshot 的 `evidence_reasons` 应指出缺失的 evaluator/rule、trajectory capability 或 acquisition code；不得包含敏感参数。

## 4. 指标、零分母与 Critical 语义

对冻结 Dataset item cohort 计算：N 是不同 dataset_item_id 的总数，每 Case 只计一次；P=Case 总体 PASS 数，F=总体 FAIL 数，U=N-P-F。6 个 Case 各有 2 条 required 规则时 N 仍是 6；缺证据的 Case 留在 N 中记 UNKNOWN，不因规则数、采集成功数或 Judge 采样数缩小分母。
`decided_pass_rate = P / (P + F)`，只描述已判定 Case 中的通过比例。
`decision_coverage = (P + F) / N`，描述所有 required Case 中已有 PASS/FAIL 结论的比例。
对 schema 1.3，Gate 仍可引用已有策略 metric：`pass_rate = decided_pass_rate`，`evaluation_coverage = decision_coverage`。
schema 1.2 的 `pass_rate/evaluation_coverage` 保持旧聚合语义；1.3 的值依赖 QualityPolicy required evidence，不依赖任意 optional evaluator 的运行状态。
当 `P+F=0` 或 `N=0` 时相应指标为 null，Console 显示 `—`；不产生 0%、不当作 PASS，也不通过 Baseline/Gate。
optional evaluator 的 skip/failure 不进入上述 required Case 状态；它仍可显示为诊断计数。
`U > 0` 时 Gate 依据 `unknown_not_releasable` 返回 UNKNOWN，不允许只用已判 Case 平均分发布。
trajectory release 策略要求 block_critical_failures=true 并在冻结时验证；旧策略保持既有开关含义。critical FAIL 一票否决发布；critical UNKNOWN/missing 或未经准入的 single-source Judge 也不可发布。沿用当前 decision 优先级：证据/比较缺口或 required UNKNOWN 时 UNKNOWN，否则已证 critical/阈值违规为 FAIL；两者均 releasable=false。critical FAIL + required UNKNOWN 返回 UNKNOWN，并保留逐规则 FAIL 与 CRITICAL_FAILURE 原因码。
关键违规作为独立 `critical_failure_count`/reason code 处理，不被非关键 Case 或 Judge 分数均值稀释。
有序评分和业务均值仍遵循其冻结的类型与 aggregation contract；不得将 categorical/text、no_result 或 UNKNOWN 转成数字。

每份 GateEvaluation 同时返回计数、`decided_pass_rate`、`decision_coverage`、`unknown_count`、`critical_failure_count`。
已有 ReleasePolicy 可继续以 `pass_rate` 和 `evaluation_coverage` 设阈值；对 1.3 两个 metric 映射到上述公式，响应字段明确其分母定义与 evidence contract。
当策略只配置一个指标时，Gate 响应仍呈现两项观测指标；返回 `null` 的 UI 文本统一为 `—`。

## 5. Formal Comparison 与 digest 规则

Formal comparison 继续检查 Dataset 身份、完整 Baseline/Candidate、required Case 全覆盖和三层合同 digest 全等。
`trajectory_contract_digest` 包含 Schema、Assembler、Source Profile、Mapping、Redaction、能力/完成协议、读取限额与规则证据范围。
该契约 digest 纳入 Measurement digest；改变轨迹构造/规则实现意味着 `MEASUREMENT_CHANGED`。
`trajectory_digest` 是单个 Item/Attempt 的冻结语义输入摘要，用于结果来源核验、复评恢复和审计。
`source_result_digest` 是 Snapshot 结果制品幂等指纹，因输入输出和结果变化而变化；它不要求 Baseline 与 Candidate 相等。
`evidence_contract_version="trajectory-1.0"` 固定 required evidence 判定和 coverage 语义，进入既有 Aggregation/Comparison digest。
Baseline 与 Candidate 的 Agent 行为预期不同，因此双方的 per-case `trajectory_digest` 不要求相等，也不得因自然不同阻断比较。
正式比较只要求双方采用可比的冻结轨迹契约，且 required Case 都有属于自身 Snapshot 的完整 evidence ref。
输入 artifact 缺失、digest 校验失败、尝试串线或 required Case 轨迹不可用，正式结论 withheld，加入稳定原因码 `TRAJECTORY_EVIDENCE_INCOMPLETE`。
质量阈值、operator、required、critical 或 unknown policy 改变时，三层中的 Quality Policy digest 改变，返回 `QUALITY_POLICY_CHANGED`。
遇到 `QUALITY_POLICY_CHANGED` 时 `formal.available=false`，`withheld_reasons` 包含该码，逐 Case 只标 `DIAGNOSTIC_ONLY`。
聚合分母、decision coverage、零分母和分类算法改变时提升 `AGGREGATION_COMPARISON` 版本与 digest。
不新增第四个比较维度；三个现有维度仍为 Measurement、Quality Policy、Aggregation/Comparison。

示例：

```text
Baseline: contract=C1, case-7 trajectory_digest=T_old
Candidate: contract=C1, case-7 trajectory_digest=T_new
结果：该 Case 的证据各自有效时，per-case 行可比较；T_old != T_new 是预期行为。
若 Candidate contract=C2，则 Measurement 不匹配，formal withheld，原因包含 MEASUREMENT_CHANGED。
```

## 6. Baseline、API 与 Console

Baseline eligibility 继续基于指定不可变 Snapshot，而不读取 Launch 的当前活动状态。
后端只允许 1.3 的 COMPLETE Snapshot 且 required Case 覆盖完整时设为 Baseline；DIAGNOSTIC 返回已有拒绝响应并解释原因。
1.3 Baseline 校验按 `required_evidence_status` 判断评测完整度；不能再用总 `evaluator_error_count` 把可选 Judge 故障误当成 required failure。
1.2 仍保留现有 `evaluator_error_count` 判定，以免重释历史快照。
Baseline 的“证据完整”表示所有必需规则已决，不等价于质量通过；Gate 仍读取 PASS/FAIL 和 ReleasePolicy 决定是否发布。
API response 应新增明确定义的 `required_case_counts`/`trajectory_coverage` 摘要，而非把内部 Artifact 全量暴露。
ReleaseGate response 扩充上述双指标与 `unknown_count`/`critical_failure_count`；现有 gate id、decision、releasable、reason_codes、rules 保持。
Snapshot/Comparison 详情只暴露脱敏 Step 摘要、freeze ref、状态、digest 与原因码；不得返回 Header、原始 Observation dump 或未授权参数。
更新 OpenAPI 源后执行 `python scripts/export_openapi.py`，再执行 `pnpm --dir services/console api:generate` 同步类型。
在现有 Case/Snapshot 页面展示 required evidence 状态、两项比率、`—` 零分母、critical/UNKNOWN 阻断原因和 `DIAGNOSTIC_ONLY` 行。
Baseline 控件依据后端返回的 baseline eligibility/拒绝原因；不使用 Gate.releasable 冒充基线资格，证据完整但质量 FAIL 的 Snapshot 仍可依法设基线。UI 不自行重算资格。
Score 投影只从已冻结 Snapshot 的 evaluator result 和 root reference 构造；断连状态不能改变 Snapshot、Comparison 或 Gate。

## 7. 投影与幂等

扩展现有 `langfuse_projection.project_typed_result()` 的 evidence 字段，包含 `snapshot_id`、revision、trajectory contract digest、per-case trajectory digest 和冻结 root observation ID。
trajectory 投影采用新且冻结的 key scheme：score:trajectory-v1:SHA256(canonical(item_id,snapshot_id,binding_id))。同 Snapshot 重试同 ID；新 Snapshot ID 则新 ID，即使 dispatch_generation 不变也保留旧投影。旧非轨迹投影保持现有 stable_score_id。
trace/root 从 Snapshot 冻结的 Item 字段与 EvidenceRef 所属 artifact.scope 交叉校验取得；EvidenceRef 本身不新增旁路 root 字段，不从在线 Trace 覆盖。
在现有 LangfuseSyncTaskRecord 增加可空 result_snapshot_id，并设 FK 到 Snapshot、阻止隐式级联删除；旧任务为 NULL。追加迁移先建立历史路径部分唯一索引(item_id,dispatch_generation,task_type) WHERE result_snapshot_id IS NULL，以及新路径(item_id,result_snapshot_id,task_type) WHERE result_snapshot_id IS NOT NULL，再移除原全局唯一约束；不删除旧数据，不修改已发布迁移。新类型 TRAJECTORY_ITEM_SCORES 只在 Snapshot 冻结后同事务入队，payload 固定该 Snapshot/Binding/typed result/ref。
retry 对新任务不得调用 _resolve_snapshot_provenance 的 latest 查询；只读任务指向的 Snapshot。状态聚合只统计当前 Snapshot 对应任务，历史修订任务不污染当前同步状态；旧任务继续原 generation 路径。PostgreSQL 测试同时验证旧唯一性、新多revision并存、崩溃重投、eval-only recovery 新Snapshot入队与旧Score保留。

claim payload 必须携带 task_type/result_snapshot_id，process_single_task 显式分流，未知类型拒绝。trajectory Launch 的 FULL_EVAL_SYNC 保留 Dataset Run Item 关联职责、冻结为 link-only，停止该任务的重复 Score 写入；TRAJECTORY_ITEM_SCORES 只投影固定结果，不重复创建 Run Item。select_current_run_evidence 仅消费 FULL_EVAL_SYNC 链路证据；aggregate_launch_sync_status 与 launch_sync_breakdown 区分链路任务和当前 Snapshot 投影，不能用(item,generation)字典任意覆盖多个任务。新历史 Snapshot 任务以自身 lease/CAS防重投，不被当前 Item generation误删，其完成不改当前 Snapshot 状态。可复用既有 Snapshot级 Run Score 的入队模式，但不混用两种 score职责。
轨迹 result 没有可验证 root ref 时保持 NOT_APPLICABLE/明确原因；不可把它投到 Agent 的另一个 Trace。
投影失败由现有 Outbox 重试；Gate 不依赖投影是否成功，也不从 Score API 回读分数。

## 8. 具体改动文件

| 文件 | 实施内容 |
|---|---|
| `services/eval-runner/app/result_snapshots.py` | 按 manifest 1.2/1.3 与 `evidence_contract_version` 显式分类，冻结 per-item required evidence 和摘要 |
| `services/eval-runner/app/aggregation.py` | 对 1.3 required Case 计算 PASS/(PASS+FAIL)、(PASS+FAIL)/N 与 Critical/UNKNOWN 计数 |
| `services/eval-runner/app/comparison_contracts.py` | 更新 Aggregation/Comparison contract 版本与 canonical digest |
| `services/eval-runner/app/release_gates.py` | 连接 required coverage、Critical Judge 准入、trajectory completeness 和 withheld 原因 |
| `services/eval-runner/app/baselines.py`、`api_baselines.py` | 保持 COMPLETE 唯一资格，并返回 1.3 缺证据的具体原因 |
| `services/eval-runner/app/api_release_gates.py`、`models.py` | 暴露冻结 Gate 双指标、UNKNOWN/Critical 计数和类型化理由 |
| `services/eval-runner/app/api_results.py` | 把 per-case trajectory ref/状态安全地并入 Snapshot/Comparison response |
| `services/eval-runner/app/langfuse_projection.py`、`langfuse_sync.py` | 投影固定 root ref 和 digest provenance，维持 outbox idempotency |
| `services/eval-runner/app/db_models.py`、`evaluation_recovery.py`、Snapshot finalize path 与追加迁移 | 现有 Outbox 增加 Snapshot ref/部分唯一性，新修订冻结时入队；不新建 Outbox |
| `services/console/src/features/launches/LaunchDetail.tsx` | Snapshot/按钮展示 required evidence 和后端资格原因 |
| `services/console/src/features/launches/tabs/GateComparisonTab.tsx` | 并列显示已判通过率与判定覆盖率，null 渲染 `—` |
| `services/console/src/api/schema.d.ts`、`docs/openapi.json` | 由 OpenAPI 同步生成，不手写重复 response 类型 |
| `README.md`、`TECHNICAL_DESIGN.md` | README 加能力/导航摘要；技术细节只记在 TECHNICAL_DESIGN 对应节 |
| `tests/test_trajectory_release_gate.py` | 端到端验证 Snapshot、Baseline、三层比较、Gate、投影和恢复 |
| `tests/trajectory_fixtures.py` | 使用 #128 统一工厂，含 1.2/1.3 Manifest 和固定轨迹 refs |

不新建数据库表或投影系统；#128 追加 artifact 表，#132 为既有 Outbox 追加 nullable Snapshot ref 与索引迁移，实施时在 artifact 迁移后重新分配顺序编号。

## 9. TDD 和命令

以下是后续实施顺序，不是文档方案 PR 已执行的测试结果。

RED：先增加单元和集成用例，确认当前 optional skipped→DIAGNOSTIC、轨迹引用缺失放行等反例被测试捕获：

```bash
pytest -q tests/test_trajectory_release_gate.py -k optional_skip
pytest -q tests/test_trajectory_release_gate.py -k trajectory_digest
```

第一个用例构造 required rule PASS + optional Judge skipped；旧分类预期暴露 DIAGNOSTIC，1.3 目标应为 COMPLETE。
第二个用例构造双方同 contract、不同 per-case digest；目标应正式可比；只改 contract digest 则必须 withheld。
GREEN：实现后运行新门禁集成测试和已存在门禁/快照/比较测试：

```bash
pytest -q tests/test_trajectory_release_gate.py
pytest -q tests/test_issue_7_release_gates.py tests/test_issue_85_snapshot_revision.py tests/test_issue_86_comparability.py tests/test_api_results_s3.py
```

API 类型变化时同步契约：

```bash
python scripts/export_openapi.py
pnpm --dir services/console api:generate
make console-test
make console-e2e
```

最终执行仓库唯一质量门禁 `make validate`；Docker 步骤如环境不具备须按仓库规则记录 SKIP。
本方案只新增文档；未运行以上测试，也不把计划命令写成已通过证据。

## 10. 验收标准

| ID | 验收项 | 通过证据 |
|---|---|---|
| S45-01 | schema 分支 | 1.0/1.1 legacy 与1.2保持原Reader；1.3需完整资格discriminator/字段；未知或半契约schema为UNKNOWN |
| S45-02 | optional skip | required PASS + optional skipped/failed 时 Snapshot 为 COMPLETE；optional 诊断仍可见 |
| S45-03 | required 缺失 | required no_result、failed、skipped 或 UNKNOWN 使 Item/Snapshot INCOMPLETE/DIAGNOSTIC |
| S45-04 | FAIL 与 COMPLETE | 所有证据完整但规则明确 FAIL 时 Snapshot 仍可 COMPLETE；Gate 可拒绝发布 |
| S45-05 | 零分母 | `P+F=0` 或 `N=0` 返回 null，Console 见 `—`；Baseline/Gate 不因 null 通过 |
| S45-06 | 两项比率 | API/UI 给出P/(P+F)和(P+F)/N；N为冻结cohort的不同dataset_item_id数；6 Case×2规则仍N=6，缺证据留在分母 |
| S45-07 | UNKNOWN fail-closed | 任一 required UNKNOWN 时 `decision=UNKNOWN`、`releasable=false`，不得被均值稀释 |
| S45-08 | Critical veto | 新策略冻结block_critical_failures=true；critical FAIL+required UNKNOWN→UNKNOWN并保留CRITICAL_FAILURE、不可发布；纯已证违规→FAIL；无单Judge绕过 |
| S45-09 | Baseline qualification | DIAGNOSTIC 的设基线请求被拒并给原因；COMPLETE 根据冻结 Snapshot 判断 |
| S45-10 | QualityPolicy changed | 三层 digest 中 policy 改变时 `formal.available=false` 且含 `QUALITY_POLICY_CHANGED` |
| S45-11 | Case rows | policy 不匹配时受影响行标 `DIAGNOSTIC_ONLY`，不伪造 regression/正式均值 |
| S45-12 | 轨迹契约可比性 | trajectory contract digest 进入 Measurement；改变时 formal withheld |
| S45-13 | per-case digest | 同契约下 Baseline/Candidate trajectory digest 不同仍可比；两侧各自 ref 必须自洽 |
| S45-14 | 覆盖范围 | required Dataset Case 缺失、跨 Attempt 或证据不全时 withheld，列出稳定原因码 |
| S45-15 | 投影幂等 | 同Snapshot重试同ID/ref/provenance；dispatch_generation不变的新Snapshot使用新ID与Outbox任务；旧投影保留且唯一性/聚合状态正确 |
| S45-16 | 投影隔离 | 投影失败不改质量结果；不得回读 Langfuse Score 决定 Argus Gate |
| S45-17 | 历史保护 | 1.2 Snapshot、旧 Baseline、旧 Gate 记录不被重写或补造 trajectory digest |
| S45-18 | Demo 基线 | `tests/test_expected_pass_rate.py` 原样通过，v1 2/6 与 v2 6/6 不变 |
| S45-19 | OpenAPI 同步 | export 后 `docs/openapi.json` 和生成的 `schema.d.ts` 无 drift |
| S45-20 | 全门禁 | 新测试、兼容回归、Console 测试与 `make validate` 通过；所有跳过项说明原因 |

## 11. 依赖、风险与回退

完整计划在 #130/T6 使全部确定性规则和参数合同稳定后交付 #132-A；不需要等待 #131 全部交付。
阶段 A：确定性规则接入 required evidence、Snapshot、Baseline、三层 digest 和 Gate。
阶段 B：#131 Judge 补齐结构化结果、critical 来源校准和全量 required coverage，再接同一 Gate 契约。
`tests/trajectory_fixtures.py` 由 #128 实现；本 issue 只复用，不重复定义测试 schema。
主要风险是把 optional 诊断状态误当 required 不完整、把 Case digest 当比较合同、或把 UNKNOWN 混入均值。
另有旧 Snapshot 迁移误读、关键 evidence ref 串 Case、Langfuse 投影重试改写 root 和 Console/OpenAPI drift 风险。
通过显式 1.2/1.3 Reader、schema 约束、Snapshot 引用验证、outbox 幂等键、公式独立单测和全量门禁缓解。
回退时停止为新 Launch 创建 trajectory-enabled Binding；保留已冻结的 1.3 Artifact、Snapshot、Baseline 和 Gate 记录。
不得回滚或删除已发布迁移；配置回退保留可读1.3及新任务类型的Runner，禁止直接退回不了解它们的旧binary。未知schema/类型在新读者中fail closed，binary回退另需兼容与待处理任务评审。


## T7 当前实施记录（2026-10-14）

Snapshot Outbox、Gate 双比率/Case 计数、冻结轨迹覆盖摘要和 Console 展示已实现；完整阶段验收仍在进行。详见 [实施进度与证据](../../validation/s4-5/gate-progress.md)。

`trajectory_coverage` 以冻结 Dataset 全量 Case 为分母，分子只计受控 Loader 校验通过的实际 Artifact；它不替代 `required_evidence_status`。旧合同、零 Case 或脱离校验 Session 的对象返回 null。新 Gate 引擎固定为 `trajectory-release-gate-v1`，API 幂等 request digest 使用同一版本；历史 `release-gate-v3` 记录不补算新增字段。历史 summary 的未存储指标保持缺失。

新 trajectory 的 optional critical 在新 Launch/Gate 冻结边界拒绝（`TRAJECTORY_OPTIONAL_CRITICAL_INVALID`）；幂等查询先返回已有请求，旧合同不回写。关闭 critical 阻断则新 Gate 冻结返回 `TRAJECTORY_CRITICAL_BLOCK_REQUIRED`。损坏 Manifest/comparison 返回稳定冲突，包含 legacy 1.2 的 null 比较对象。

只读独立审阅发现的任务分流、历史投影兼容、optional critical、损坏 Manifest 与幂等重放问题均已通过针对性 RED/GREEN 和静态复核修复。最终本地门禁为 Python 2288、Console 861、E2E 129 项通过；完整真实来源门禁及发布 CI 未验收，不能据此标记 T7 或完整 S4 完成。
