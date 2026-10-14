# S4 Trajectory Evaluation Implementation Plan

> **For agentic workers:** 按本计划逐任务执行；具备 executing-plans 时可按其执行流程推进，委派遵循项目要求仅用 luna_worker，每文件唯一写入者。先读 Spec 与阶段方案，提交真实 RED/GREEN 和验收证据；本方案 PR 不授权启动产品实施。

**Goal:** 完成 #5 及 #127–#132 的跨框架 observed trajectory 评测、可解释诊断和正式发布门禁。

**Architecture:** 扩展现有冻结 EvaluatorExecutor、Typed Results、QualityPolicy、Snapshot、Comparison 与 Outbox。新增只读采集、归一化和最小冻结证据，保持 Agent 业务 API 无 Evaluation SDK。诊断与 Release 覆盖分开，按规则能力判断 UNKNOWN。

**Tech Stack:** Python 3.12 / FastAPI / Pydantic / SQLAlchemy / PostgreSQL；Langfuse当前锁定SDK `4.16.0`、服务 `4.38.0`；OTel；测试Agent `pydantic-ai-slim==2.55.0`（无extras、FunctionModel），与Runner分离环境；React / TypeScript / Vite / Vitest / Playwright；Node 22、pnpm。

**Spec:** [01-technical-design.md](01-technical-design.md)。分期细节见 [02](02-s4-0-observation-validation.md)、[03](03-s4-1-acquisition-and-schema.md)、[04](04-s4-2-rules-and-diagnosis.md)、[05](05-s4-3-arguments-and-redaction.md)、[06](06-s4-4-judge.md)、[07](07-s4-5-release-gate.md)。状态：提议 / 待实施。

## 1. Global Constraints

- Trajectory Schema 1.0；轨迹启用 Manifest 1.3；T2仅冻结Artifact/Checkpoint，T4同时接入required_evidence_status、新聚合合同与Snapshot资格discriminator，旧Manifest/Snapshot不改写。
- Agent 零 Evaluation SDK；OTel 是深度轨迹的可选增强，最终答案评测不依赖它。
- 首批 observed 规则使用独立 identity/version；旧 `required_tool_match` 含义不变。
- 执行成功、评测运行状态、规则质量、必需证据完整度、Langfuse 同步状态分别表达。
- 同一文件只由一个执行者写；并行改动先核对，禁止覆盖、回退他人版本。
- 迁移顺序追加；当前基线最高 016，实施时再次分配下一序号；禁止修改已发布迁移。
- API 类型来自 OpenAPI；任何 DTO/路由变化同步 `docs/openapi.json` 与 Console schema。
- Console 使用现有 feature、语义 Token、键盘/状态文本，运行 token lint、Vitest、Playwright。
- 保持 Demo v1=2/6、v2=6/6；trajectory 独立 slice 不改现有断言。
- 首版正式 Release 的 required Judge Case 全量覆盖；诊断采样不进入正式均值。
- 新 Binding opt-in；不开启新 Binding 时不采集轨迹、不改变旧 Launch 的评测含义。

## 2. Review Focus

| 最易遗漏的条件 | 预期 | 负责任务 |
|---|---|---|
| HTTP Root 已到、Tool Span 迟到 | 有界等待；未成熟不能判不存在 | T0 / T1 |
| 同 Trace 两个 Item、重试 Attempt 混入 | 真实身份闭包隔离，禁止时间窗口猜测 | T1 / T2 |
| B→A→B 或并行分支 | EVERY_AFTER；不能因为存在一对就 PASS | T3 |
| optional skipped、required 全 PASS | 诊断可见、必需证据完整、Snapshot 可正式使用 | T4 |
| 固定快照打开时在线 Trace 已变/过期 | 展示冻结结论及不可恢复原因，不重算历史 | T2 / T5 |

另由 T6/T8 覆盖“全脱敏参数”“Judge 注入/超时/未触发样本”，T7/T9 覆盖阈值变更、迟到 generation、Projection 断连与比较覆盖。

## 3. 依赖、人员与估算

执行顺序：T0→T1→T2→T3→T4→T5→T6→T7；T8 在 T6 后开展，T9 等待 T7/T8。T1 的采集 Adapter 与 Schema 可在接口锁定后并行只读分析，公共文件写入顺序由集成执行者安排。#132 分确定性接入和 Judge 补验，不能因等待 Judge 推迟确定性结果交付。

| 角色 | 独占模块 | 共享入口写入规则 |
|---|---|---|
| 证据执行者 | `trajectory/schema.py, acquisition.py, normalizer.py, assembler.py, evidence.py, redaction.py`；真实观测夹具 | 对 worker/checkpoint 的变更提交集成建议，集成执行者统一写入 |
| 规则/Judge 执行者 | `trajectory/rules.py, judge.py, judge_provider.py` 及对应测试 | 不并行写 Registry/Binding/Policy/Gate |
| Console 执行者 | `features/launches/Trajectory*.tsx`、Vitest/E2E | 读已冻结 API；生成 schema 由集成执行者执行 |
| 集成执行者 | manifest/binding/worker/recovery/checkpoint、models/db_models、API、policy/snapshot/aggregation/comparison/gate、baselines、langfuse_projection/sync/links 与迁移 | 同一时刻唯一写入者；按任务门槛接入 |

估算前提：熟悉现有系统、测试服务可用、两个 Demo 框架可提供所需观测；每任务包含测试/评审/证据，不能把等待服务当成编码已完成。总量约 **23–34 人日**，已计入Snapshot投影修订与async Judge/恢复桥接，是计划区间，不是交付承诺。业务标注集交付等待不计入编码人日；Source Profile缺关键字段时先补普通instrumentation或降低能力，不硬冻不成立的契约。

| 任务 | 估算人日 | 可验收交付 / 关联 |
|---|---|---|
| T0 | 2–3 | 真实链路矩阵 / #127 |
| T1 | 3–4 | 受控采集、Schema、Assembler / #128 |
| T2 | 2–3 | 冻结证据及恢复 / #128 |
| T3 | 2–3 | 四规则与兼容 Adapter / #129 |
| T4 | 1–2 | required/optional 集成资格 / #129 |
| T5 | 2–3 | Case API / 诊断交互 / #129 |
| T6 | 2–3 | 安全参数和剩余规则 / #130 |
| T7 | 3–4 | 确定性 Gate、Snapshot级Case投影 / #132-A |
| T8 | 4–6 | Async Judge/恢复桥接、校准与准入 / #131 |
| T9 | 2–3 | Judge Gate 和母 issue Demo / #132-B、#5 |

交付方式更新（2026-10-14）：按用户要求，#160–#180 的全部实现汇入 [PR #181](https://github.com/miniceM/Argus/pull/181)，19 个未合并源 PR 已关闭，#162/#165 已合并并继承；源分支与历史保留。后续任务继续向该汇总 PR 追加可独立验证、单一目的的提交。PR 用 `Refs` 关联当前 issue；只有对应 issue 的全部验收完成后才使用关闭引用。

实施责任更新（2026-10-14）：用户将 #131 剩余工作交给维护者，已开发成果保留；
随后用户进一步明确：后续凡需数据进行测试的任务跳过代理执行，交管理员；
已有实现与实测结果保留。当前代理继续静态检查、文档、PR 和 Issue 交接，
不等待 GitHub Actions。
T8、S45-08 阶段 B 和母 issue 的 Judge 验收要求保持原定义，
标记交接/待验，不计通过、不提前关闭 #131、#132 或 #5。
当前实现、剩余事项和安全边界见[维护者交接](../../validation/s4-4/maintainer-handoff.md)。
所有分期的数据测试责任见[管理员验收交接](../../validation/s4-final/administrator-validation-handoff.md)；
原始验收标准和关闭条件不变，交接或跳过不计通过。

## 4. T0：证明真实 Observation 链路

**Files:** 拟新增 `tests/test_trajectory_observation_integration.py`、`tests/fixtures/trajectory_agents/`；复用或补齐 PR #133 的 `observation_link.py`；必要时最小修改 `execution.py/worker.py`。详细技术方案：[02](02-s4-0-observation-validation.md)。

**Interfaces:** 消费现有业务 `/invoke` 和 W3C context；产出版本化 Source Profile、两框架字段/延迟矩阵、真实 trace/root/item/attempt 证据。

- [ ] RED：写真实 integration 场景，断言 Agent span 的 parent 关联本次 Runner Root，另一 Item/Attempt 不混入；仅回显 Header 的 handler 不满足断言。
- [ ] 执行 `python -m pytest -q tests/test_trajectory_observation_integration.py`；记录缺真实字段/错误关联的 RED，不把缺环境产生的跳过当通过。
- [ ] 实现本地服务测试夹具：使用专用测试项目/独立数据库，启动原生OTel与Pydantic AI真实Agent.run/FunctionModel，普通instrumentation提供业务workflow序列；独立环境锁文件与已安装依赖闭包无Eval SDK，无外网运行。
- [ ] GREEN：分页查询真实 Observation，并产生如下最小断言及矩阵；隔离 fixtures 不改现有 Demo。

```python
assert agent_root.trace_id == runner_root.trace_id
assert agent_root.parent_observation_id == runner_root.observation_id
assert tool_step.workflow_scope_id == expected_workflow_scope
assert tool_step.observation_id in queried_observation_ids
```

- [ ] 提交 `test: prove real trajectory observation linkage`；S4.0 的真实字段、延迟、时序能力和部署版本证据齐全后，才进入 T1。

## 5. T1：Schema、Acquisition 与确定性组装

**Files:** trajectory/schema.py/acquisition.py/normalizer.py/assembler.py；registry.py/models.py/db_models.py的可选AgentVersion profile声明、Spec digest、Manifest冻结与API快照同步；统一追加Schema迁移（候选migrations/017_trajectory_evidence.sql），包含profile列、artifact表、checkpoint扩展；tests/trajectory_fixtures.py、test_trajectory_schema.py/test_trajectory_acquisition.py/test_trajectory_assembler.py/test_trajectory_profile_selection.py及test_launch_manifest_freeze.py。细节见03。

**Interfaces:** 消费T0 Profile；产出01的Scope/Contract/Acquisition/StandardTrajectory；同一冻结profile_set/选择算法覆盖两个框架，实际profile由AgentVersion声明并保留provenance，不改变测量合同。

- [ ] RED：缺parent/冲突ID/父环/cursor环/并行/迟到/超限；terminal先到但前序缺号不能COMPLETE；两框架选不同获准profile仍同contract，不获准拒绝；profile非空进入spec_digest、持久化且冻结，旧空值digest保持；Trace自报不覆盖声明。
- [ ] 运行 `python -m pytest -q tests/test_trajectory_schema.py tests/test_trajectory_acquisition.py tests/test_trajectory_assembler.py tests/test_trajectory_profile_selection.py tests/test_launch_manifest_freeze.py`，确认失败对应未实现行为；PostgreSQL迁移验证历史行保留。
- [ ] 实现字段Adapter、Root闭包、provider+ID去重与能力降级；Reader使用S4.0确认的v2 fields/cursor/timeout，优先SDK，必要时最小async HTTP Reader保证解码前字节/深度预算；受contract限额约束。
- [ ] GREEN：固定输入稳定、变化输入新digest，提供的Step.duration_ms变化改变digest，采集/组装耗时不改变；父环/截断PARTIAL，时间相同不推出因果；phase/status与timing按真实profile映射。

```python
first = assemble_trajectory(scope, normalized_rows, contract)
second = assemble_trajectory(scope, normalized_rows, contract)
assert first.provenance.trajectory_digest == second.provenance.trajectory_digest
assert assemble_trajectory(scope, cyclic_rows, contract).completeness.status == "PARTIAL"
```

- [ ] 提交 `feat: assemble bounded versioned trajectory evidence`；所有类型、能力和 Source Profile 收敛后进入 T2。

## 6. T2：证据制品、Checkpoint 与评测恢复

**Files:** 拟新增 `trajectory/evidence.py`、`test_trajectory_evidence.py`；修改 `execution_checkpoint.py, worker.py, evaluation_recovery.py`及T1已有db_models；复用T1的Schema迁移，不重复分配编号；扩展 `test_issue_84_eval_only_retry.py`。

**Interfaces:** 消费StandardTrajectory、产出TrajectoryEvidenceRef；T2写Checkpoint/Artifact并提供Snapshot引用序列化，T4再启用完整资格契约；恢复验证冻结manifest/binding/input/expected_output/digest。Artifact随引用它的Snapshot保留，不继承checkpoint TTL。

- [ ] RED：构造采集失败、制品缺失、digest 损坏、expected_output 不符、旧 generation 迟到；确认恢复不会再次 invoke Agent。
- [ ] 运行 `python -m pytest -q tests/test_trajectory_evidence.py tests/test_issue_84_eval_only_retry.py`。
- [ ] 实现 insert-only 最小 artifact 表、唯一键与 FK、checkpoint acquisition extension；先验证身份及所有 digest，再加载证据；使用既有 lease/generation CAS。
- [ ] GREEN：重查/重评不增加Agent/ExecutionAttempt，旧artifact/Snapshot不变；canonical JSON TEXT精确保留Decimal，往返digest一致；checkpoint TTL不删除Snapshot证据，授权删除/损坏/身份不符使后续重评UNKNOWN；T2不能写半契约Snapshot。

```python
before = (agent_call_count, execution_attempt_count, old_snapshot_digest)
recover_evaluation_only(item_id)
assert agent_call_count == before[0]
assert execution_attempt_count == before[1]
assert read_snapshot(old_snapshot_id).digest == before[2]
```

- [ ] 提交 `feat: freeze recoverable trajectory artifacts`；执行数据库追加迁移的 PostgreSQL 集成测试，证明无历史数据覆盖。

## 7. T3：独立 observed Binding 与首批四规则

**Files:** `trajectory/rules.py`、`evaluator_binding.py, evaluators.py, manifest.py, evaluator_results.py`；`tests/test_trajectory_rules.py`；扩展 #80/#81/#82 契约测试。详细方案：[04](04-s4-2-rules-and-diagnosis.md)。

**Interfaces:** 输入 `EvaluationContext` 和冻结 Binding/params；输出现有 TypedEvaluationResult，reason_code/evidence_step_ids/ref 经校验。

- [ ] RED：完整轨迹无工具但响应谎报→false；完整B→A→B→false；先UNKNOWN后确证违规仍false；PARTIAL仅见较晚A且可能遗漏更早A→UNKNOWN；明确禁止执行+PARTIAL→false；HTTP-only required→no_result；旧函数仍只收output/expected_output。
- [ ] 运行 `python -m pytest -q tests/test_trajectory_rules.py tests/test_issue_80_evaluator_versioning.py tests/test_issue_81_frozen_execution_identity.py tests/test_issue_82_typed_results.py`。
- [ ] 实现 context Adapter 与独立 identity/version 注册，绑定 schema/algorithm/artifact/contract；尊重因果和 phase，不读取旧 output.tool_calls。
- [ ] GREEN：每结果同时有真实 typed status、boolean value 或缺失原因；false 不变成计算失败。

```python
result = evaluate_trajectory_rule(context_b_a_b, order_params,
    binding=order_binding, manifest_schema_version="1.3")
assert result.status == "succeeded"
assert result.value is False
assert result.evidence["evidence_step_ids"] == [first_b_step_id]
```

- [ ] 提交 `feat: evaluate observed tool trajectories with frozen bindings`；验证现有基线、历史版本和新版本身份互不漂移。

## 8. T4：修复 optional 与正式证据资格断层

实施记录（2026-10-14）：[T4 资格链路与验收](../../validation/s4-2/required-evidence.md)及[版本/命令/制品摘要](../../validation/s4-2/required-evidence-receipt.json)，随汇总 PR #181 提交。T5 及后续任务继续实施。

**Files:** evaluator_binding.py、quality_policy.py、result_snapshots.py、aggregation.py、models.py、comparison_contracts.py、release_gates.py/baselines.py；test_trajectory_required_evidence.py；扩展#83/#85测试。

**Interfaces:** 原 eval_status 保持诊断；新 `required_evidence_status` 按冻结 required 集合生成，Snapshot 按 discriminator 使用新契约。

- [x] RED：先锁定当前复现：required 全 PASS、optional skipped，质量 PASS 但 Snapshot DIAGNOSTIC；新期望为保留 optional skipped 而 Snapshot COMPLETE。
- [x] 运行 `python -m pytest -q tests/test_trajectory_required_evidence.py tests/test_issue_83_quality_policy.py tests/test_issue_85_snapshot_revision.py`。
- [x] 实现required集合完整度，同一Writer事务写required_evidence_status/资格discriminator与聚合合同；注入序列化/提交故障验证整体回滚，不留下半契约Snapshot；手工缺字段的新快照读取fail closed；旧Snapshot不重新分类。
- [x] GREEN：覆盖optional failed/skipped/no_result；required缺Binding、failed/skipped/no_result、无值、provenance mismatch均INCOMPLETE；required false为完整FAIL；1.0/1.1 legacy与1.2旧Reader保持。

```python
assert new_item.eval_status == "partial"
assert new_item.required_evidence_status == "COMPLETE"
assert new_item.quality_evaluation["conclusion"] == "pass"
assert new_snapshot.evidence_state == "COMPLETE"
assert required_false_item.required_evidence_status == "COMPLETE"
assert required_false_snapshot.evidence_state == "COMPLETE"
assert required_false_gate.decision == "FAIL"
assert required_false_gate.releasable is False
```

- [x] 提交 `fix: separate required evidence completeness from diagnostic status`；完成真实持久化到 Snapshot/Gate 的集成测试，不能只测试 Policy 函数。

## 9. T5：固定 Snapshot 的 Case 诊断闭环

**Files:** `api_results.py, models.py`、`tests/test_trajectory_api.py`；Console `CasesTraceTab.tsx, query-keys.ts` 及拟新增 `TrajectoryDiagnosis.tsx/TrajectoryStepList.tsx`、对应 Vitest 和 trajectory E2E。

**Interfaces:** GET trajectory 的 snapshot_id/dataset_item_id 必填；消费冻结 artifact、规则结果和 Policy conclusion；UI 只展示获准内容。

- [x] RED：跨 Launch/Snapshot/Case 返回404；旧无轨迹历史200并有原因；no_result不能显示绿PASS；快照切换不能复用上一 Case 数据。
- [x] 运行 `python -m pytest -q tests/test_trajectory_api.py` 与 Console 对应 Vitest；记录 RED。
- [x] 实现只读路由和获准DTO，step_argument_display_json从同artifact派生；在现有CaseDetailPanel懒加载，使用该字符串精确显示大整数/小数；点击规则定位Step，键盘可操作，声明原始链接指向在线观测。
- [x] GREEN：同步契约并执行用户交互验证。

```bash
python scripts/export_openapi.py
pnpm --dir services/console api:generate
pnpm --dir services/console lint:tokens
make console-test
ARGUS_E2E_API_PORT=28080 ARGUS_E2E_CONSOLE_PORT=28083 make console-e2e
```

- [x] 提交 `feat: diagnose frozen trajectory rules per case`，追加至汇总 PR #181。#129 在统一验收审计后关闭。

实施记录（2026-10-14）：[T5 固定诊断与截图](../../validation/s4-2/frozen-diagnosis.md)及[版本/命令/断言/制品摘要](../../validation/s4-2/frozen-diagnosis-receipt.json)。一次独立审阅、Required 一轮修复后，PG 1240、Python 1944、Console 858、E2E 128、真实诊断消费者 17 项全部通过，无正式 skip；完整 S4 目标继续 T6–T9。

## 10. T6：安全参数规则与其余确定性能力

**Files:** trajectory/redaction.py、rules.py、schema.py与test_trajectory_arguments.py/test_trajectory_redaction.py；增量核对comparison_contracts/manifest、evaluator_binding、Snapshot/API/Projection/Outbox/worker日志出口，并扩展test_trajectory_required_evidence.py、test_trajectory_api.py、#86/#87与Console摘要。已有模块只增量扩展，集成执行者统一写入口。

**Interfaces:** 获准 JSONPath 子集、显式类型/操作符、argument_visibility；Mapping 和 Redaction 独立 digest；四个扩展规则。

- [ ] RED：已观察缺字段/错误值→false；REDACTED/未采集→no_result；禁止 eval、递归下降与未批准 coercion；敏感内容不得进入 Snapshot/API/日志/Score。
- [ ] 运行 `python -m pytest -q tests/test_trajectory_arguments.py tests/test_trajectory_redaction.py tests/test_issue_86_comparability.py tests/test_issue_87_langfuse_projection.py`。
- [ ] 实现安全解析、白名单比较和明确缺失分类；重复按工具身份+获准参数指纹；HUMAN 规则使用明确事件，不把工具名当审批事实。
- [ ] GREEN：同轨迹改变脱敏策略移动 Measurement，改变 threshold 仅移动 QualityPolicy。

```python
assert hidden_argument_result.status == "no_result"
assert observed_wrong_argument_result.value is False
assert old_mapping_digest == new_mapping_digest
assert old_redaction_digest != new_redaction_digest
assert old_trajectory_contract_digest != new_trajectory_contract_digest
assert old_measurement_digest != new_measurement_digest
```

- [ ] 提交 `feat: evaluate governed tool arguments and advanced rules`；完成 #130 的安全与跨框架回归证据。

## 11. T7：确定性轨迹正式 Gate（#132-A）

**Files:** release_gates.py、comparison_contracts.py、baselines.py、langfuse_projection.py/langfuse_sync.py/langfuse_links.py、db_models.py、Snapshot/recovery finalize、追加Outbox迁移；test_trajectory_release_gate.py；Console既有Gate/Comparison入口与对应文档。

**Interfaces:** 消费 T4 完整度、T6 Measurement 与当前三个比较 digest；复用 Outbox，不新增投影系统。

- [ ] RED：required UNKNOWN阻止发布；critical FAIL+required UNKNOWN保留CRITICAL_FAILURE且decision UNKNOWN；纯critical违规FAIL；6Case×2规则分母仍6；不同实例digest同合同可比；eval-only新Snapshot必须新ScoreID/任务且不覆盖旧投影。
- [ ] 运行 `python -m pytest -q tests/test_trajectory_release_gate.py tests/test_issue_7_release_gates.py tests/test_issue_86_comparability.py tests/test_issue_87_langfuse_projection.py`。
- [ ] 实现新schema证据条件与withheld_reasons；新发布策略block_critical_failures=true；既有Outbox加nullable Snapshot ref/部分唯一索引，claim携带类型并分流，固定Snapshot级ScoreID；FULL_EVAL_SYNC link-only，新任务不读latest，Run evidence仅读链路任务，各状态统计过滤当前Snapshot。追加迁移验证历史唯一性/多revision并存/取消重投。
- [ ] GREEN：确证 Gate结果与界面比例分母一致，零分母为`—`，DIAGNOSTIC Baseline API拒绝。

```python
assert gate_unknown.releasable is False
assert policy_only_comparison.formal.available is False
assert "QUALITY_POLICY_CHANGED" in policy_only_comparison.reason_codes
assert same_contract_different_trajectory.formal.available is True
```

- [ ] 提交 `feat: gate releases on frozen deterministic trajectory evidence`；先交付确定性 Gate，#132 保持开放直到 T9。

## 12. T8：Judge Adapter、校准与 Critical 准入

**Files:** trajectory/judge.py/judge_provider.py、test_trajectory_judge.py/test_trajectory_judge_calibration.py；Registry/Binding/QualityPolicy/ReleasePolicy冻结入口；worker.py/evaluation_recovery.py的prepare→await→finalize桥接；db_models.py/追加预算JSON迁移；独立人标留出集fixture。

**Interfaces:** 01 的冻结 Judge 配置与 TypedEvaluationResult；诊断 trigger可采样，正式必需 Case 全量，CalibrationPolicy 必填。

- [ ] RED：Prompt文本变化新身份；模型失败无假0；未知step引用拒绝；critical单Judge拒绝；diagnostic PASS高置信Case不调用Judge；release必需Case不被采样漏掉。
- [ ] 运行 `python -m pytest -q tests/test_trajectory_judge.py tests/test_trajectory_judge_calibration.py`。
- [ ] 实现async Provider Adapter与prepared_results桥接、结构化校验、总timeout/字节/并发预算、数据防注入、证据保存；恢复claim与CAS之间await期间无DB锁。计算单侧95% Wilson上界(z=1.644854)、冻结repeat_runs分歧率和联合误放行；校准不足UNKNOWN。
- [ ] GREEN：固定模型/Prompt/实现/策略和实际输出可审计；模型返回不稳定不改变历史结果。
- [ ] GREEN：取消关闭HTTP response并释放许可，event loop heartbeat持续；prepared result的Binding/Item/criteria digest不符拒绝；恢复只调用缺失Judge、不重invoke Agent。
- [ ] GREEN：首版required Judge覆盖ALL_DATASET_CASES；Launch行锁预算的两Worker竞争、重复claim、发送前/后取消都正确，usage未知不退回已发送调用预留。

```python
assert diagnostic_high_confidence_pass.provider_call_count == 0
assert release_required_case.provider_call_count == 1
assert malformed_judge_result.status == "failed"
assert unqualified_calibration.release_eligible is False
```

- [ ] 提交 `feat: add calibrated frozen trajectory judges`；#131 需模型故障、预算、校准与配置准入全部证据齐全。

## 13. T9：Judge Gate 补验与 #5 整体验收

**Files:** `test_trajectory_release_gate.py`、真实 integration测试、Console E2E、独立trajectory Dataset slice；`README.md/TECHNICAL_DESIGN.md/docs/release-gates.md` 的已交付能力说明。

**Interfaces:** T7正式Gate + T8Judge资格；同冻结Evaluator与Mapping用于两个Agent实现。

- [ ] RED：Baseline/Candidate Judge覆盖不一致时正式比较不可用；同身份“双Judge”不能绕过critical准入；原始Trace变更不改固定Snapshot。
- [ ] 执行目标pytest、真实Langfuse场景和ConsoleE2E，逐项记录09要求的版本/ID/输出。
- [ ] 完成母issue Demo：两框架使用同一冻结Evaluator；客户查询→余额→故意禁止转账可定位；HTTP-only最终答案照判、必要轨迹UNKNOWN；受控参数与原始Trace链接；旧基线不变。
- [ ] GREEN：运行最终质量门禁并核对实际结果；新测试不得以跳过作为通过。

```bash
python -m pytest -q tests/test_expected_pass_rate.py
make console-test
ARGUS_E2E_API_PORT=28080 ARGUS_E2E_CONSOLE_PORT=28083 make console-e2e
make validate
```

- [ ] 提交 `test: prove end-to-end cross-framework trajectory release evaluation`；提交全部真实证据后关闭#132和#5。

## 14. 每次交付的证据、回退与停止条件

每个任务按“测试代码→实际RED→最小实现→实际GREEN→相关回归→独立评审→单目的commit”执行。新增示例在本计划中是拟实现测试设计，不是当前可运行功能；实施者须先建立类型和fixture再运行。不得复制本机工具wrapper/缓存路径进脚本。

环境不满足时保留源码与阻塞证据，列明实际缺少的服务/字段/依赖；真实链路未验证不得推进Schema冻结。跨文档类型、来源与版本不一致时先修契约，不让各模块自行选解释。

回退采用禁用新Binding与创建新版本：已写不可变Snapshot/Artifact不删除、不降schema、不回填旧结果；Reader需保留已发布版本支持。上线默认只在隔离slice opt-in，质量未知和critical违规始终不能发布。迁移回退不drop已含用户数据的表，修复通过追加迁移完成。

验收记录使用[09模板](09-acceptance-matrix.md)。计划任务勾选只在实际结果与验收相符时更新；文档合并不得提前勾选或修改产品issue状态。
