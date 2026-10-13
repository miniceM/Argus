# S4.2：首批轨迹规则与逐用例诊断

状态：**提议，待方案 PR 评审；本文是待实施方案，不表示功能已交付。**

关联：[父 Issue #5](https://github.com/miniceM/Argus/issues/5)、[Issue #129](https://github.com/miniceM/Argus/issues/129)。共同契约见 [01-technical-design.md](01-technical-design.md) §3–§7。

核验基线：2026-10-13，`main@4b043739c63357ecc10131e1ba54bc1bc53d862a`。

## 1. 目标和当前实现

S4.2 将过程规则接入现有冻结 Evaluator 链路，以 Langfuse Observations 组装的 `StandardTrajectory` 为唯一过程证据。

本期交付 `require_tool`、`forbid_tool`、`tool_order`、`max_tool_calls`，并在 Launch Case 详情中显示冻结结果、三态原因和可回链 Step。

Agent 返回体里的 `output.tool_calls` 仅是业务响应数据，不能证明工具实际发起、执行或成功。

当前 [`evaluators._tool_names()`](https://github.com/miniceM/Argus/blob/4b043739c63357ecc10131e1ba54bc1bc53d862a/services/eval-runner/app/evaluators.py) 从响应提取工具名；`EvaluatorExecutor` 的旧执行入口只接收 `output/expected_output`，见 [`evaluator_binding.py`](https://github.com/miniceM/Argus/blob/4b043739c63357ecc10131e1ba54bc1bc53d862a/services/eval-runner/app/evaluator_binding.py#L507)。

QualityPolicy 已按冻结规则输出逐条 PASS/FAIL/UNKNOWN；缺失、失败、skipped 或无值的 required 规则为 UNKNOWN，见 [`quality_policy.py`](https://github.com/miniceM/Argus/blob/4b043739c63357ecc10131e1ba54bc1bc53d862a/services/eval-runner/app/quality_policy.py#L604)。

`eval_status` 目前会汇总所有 Binding 的异常：optional 诊断失败可令它成为 `partial` 或 `failed`，即使必需质量规则通过，见 [`evaluator_binding.py`](https://github.com/miniceM/Argus/blob/4b043739c63357ecc10131e1ba54bc1bc53d862a/services/eval-runner/app/evaluator_binding.py#L1003)。

当前 Snapshot 将任何 `eval_status != succeeded` 判为 DIAGNOSTIC，见 [`result_snapshots.py`](https://github.com/miniceM/Argus/blob/4b043739c63357ecc10131e1ba54bc1bc53d862a/services/eval-runner/app/result_snapshots.py#L107)；现有测试只断言 optional 失败不改变质量结论，未验证 Snapshot 资格，见 [`test_issue_83_quality_policy.py`](https://github.com/miniceM/Argus/blob/4b043739c63357ecc10131e1ba54bc1bc53d862a/tests/test_issue_83_quality_policy.py#L526)。

## 2. Issue #129 的需求修订

保留四规则、冻结 Evaluator 身份、HTTP-only 降级展示、独立违规 Dataset slice 和现有 quality policy；Agent 仍零评测 SDK。

将 `tool_order` 从“存在一对 A 完成早于 B 开始”修订为 **`EVERY_AFTER`**：范围内每一次 B 请求都必须有同一工作流中、已成功且有可信因果先行证据的 A。

`B → A → B` 中第一笔 B 是确定违规，第二笔 B 不能覆盖它，整条规则为 FAIL。

`started_at/ended_at`、数组顺序、同一 parent 或不同服务的 wall-clock 时间均不能单独证明因果；可信 `causal_predecessor_ids` 与 `workflow_scope_id` 按共同契约产生。

将 Issue 当前“DEGRADED 下轨迹规则一律 skipped”的表述修订为按规则判证据：已观察到的明确违规仍产出 `succeeded + false`，逐规则结论为 FAIL；未观察到违规但覆盖不足为 `no_result` / UNKNOWN；仅规则未触发或输入类型不适用时才 `skipped`。

业务答案评测保持原有输入与质量判断；轨迹降级横幅不得覆盖答案结论。按 QualityPolicy 现有 unknown-safe 逻辑，其他 required 证据缺失时总体仍可为 UNKNOWN，但逐规则已知违规必须保留。

修订 HTTP-only 验收为：轨迹规则没有所需能力时返回 `skipped` 或 `no_result`，结论 UNKNOWN，required 证据不完整，Snapshot 为 DIAGNOSTIC；答案 Evaluator 仍按原链路判定。

## 3. Frozen Evaluator 接口与规则语义

规则注册继续使用 `EvaluatorDefinition + EvaluatorVersion`、`freeze_binding → EvaluatorExecutor → TypedEvaluationResult → QualityPolicy`；不创建新注册表、结果 DTO 或版本体系。

Trajectory Binding 在冻结 `input_contract` 中声明 `input_kind="trajectory"`、所需 capability、rule id、params 和算法版本；Manifest 新版本遵循 01 的 `1.3` 规则。

本期在既有 executor 增加 Context Adapter，消费 S4.1 的冻结轨迹，构造 `EvaluationContext` 并校验 `EvaluatorBinding` 与 Manifest schema identity；Evaluator 不查询 Langfuse、不读取在线最新 Trace，也不调用 Agent。

```python
def evaluate_trajectory_rule(
    context: EvaluationContext,
    params: TrajectoryRuleParams,
    *,
    binding: EvaluatorBinding,
    manifest_schema_version: str,
) -> TypedEvaluationResult: ...
```

测量结果经现有 `build_result()` 生成；可证明的 false 是成功测量值，不是执行错误。

证据不足走现有 `build_result(..., raw_value_present=False)` 形成 `no_result`；规则不适用或冻结的诊断触发条件不满足时走 `skipped_result()`。

Adapter 验证 `binding_id/evaluator_id/version/definition_digest` 与 provenance 一致，保留 `no_result/skipped`，不进入 legacy numeric 转换。

| 规则 | 冻结参数与 PASS/FAIL/UNKNOWN 语义 |
|---|---|
| `trajectory.require_tool` | `tool_identity` 必填，`phase` 必填、默认 `EXECUTED`；有匹配且所需状态的 Step 为 true；覆盖完整却不存在为 false；否则 no_result。 |
| `trajectory.forbid_tool` | `tool_identity` 必填，默认 `phase=EXECUTED`；观测到禁止执行即 false；可显式冻结 REQUESTED 以禁止尝试，包括随后被 BLOCKED 的请求。未见违规只在所选 phase 的覆盖充分时为 true。 |
| `trajectory.tool_order` | 参数含 A/B identity、A 成功状态、业务 scope 和 `EVERY_AFTER`；每个 B 请求单独检查可信因果先行 A。 |
| `trajectory.max_tool_calls` | 参数含 identity、计数 phase、scope、max_count；distinct Step 已去重；超限 false，未超限且覆盖充分 true，否则 no_result；observed_count 只作安全证据。 |

`require_tool` 默认只接受 `EXECUTED + SUCCEEDED`；显式配置 `REQUESTED` 才表示检查是否发起。

`forbid_tool` 默认约束真实执行；禁止尝试的业务策略必须显式冻结 REQUESTED，并要求 tool_requests 能力。两种含义使用不同 params/digest，不能静默互换。

max_count 是 boolean 规则的业务约束参数，改变它属于 Measurement 变化；QualityPolicy 对该结果的 operator/expected_value 是独立决策口径。安全 count 证据不作为未声明的 numeric result。后续若要将计数作为纯 numeric 测量，须注册独立版本并定义部分证据的下界语义。

`max_tool_calls` 的参数固定 phase；REQUESTED 与 EXECUTED 不合并计数。缺少工具调用覆盖时，未超限不能判 PASS；已观测数量超过上限可立即判 FAIL。

## 4. EVERY_AFTER 因果算法

先按冻结 `workflow_scope_id` 划定同一业务工作流；若契约声明 `ROOT_SUBTREE`，仅在完整 Root 子树内判断。不得按时间窗口拼接客户或请求身份。

只从 `EXECUTED + SUCCEEDED` A 中选先决事件；B 检查目标默认是 `REQUESTED`，因为规则约束的是尝试 B 前必须完成 A。

`happens_before(a, b)` 只承认已验证 profile 的因果前驱闭包或可信同源序列，且必须证明 A 成功完成先于 B 请求；只有开始序号不足以证明完成。v1 同时要求可靠 ended_at/started_at 支持完成早于开始，时间缺失、重叠或矛盾为 UNKNOWN；时间戳本身仍不能替代因果边。

```python
violations, unknowns = [], []
for b in observed_b_requests:
    candidates = same_scope_successful_a(b)
    if any(trusted_completion_before_request(a, b) for a in candidates):
        continue
    if prerequisite_scope_and_order_coverage_complete(b):
        violations.append(b)  # 完整先决范围内确实没有合格 A
    else:
        unknowns.append(b)  # 可能尚未采到更早 A；已见较晚 A 不足以判违规
if violations:
    return measured(False, reason="PREREQUISITE_UNSATISFIED", steps=violations)
if unknowns or not b_request_coverage_complete():
    return no_result("CAUSAL_ORDER_UNPROVEN", steps=unknowns)
return measured(True, reason="ALL_B_HAVE_SUCCESSFUL_CAUSAL_PREDECESSOR")
```

实现以 01 新增的 `causal_predecessor_ids/workflow_scope_id` 为唯一跨 Step 因果输入；parent 关系不自动等于先后。

没有 B 时，只有相关请求覆盖完整才能得到 vacuous PASS；覆盖不全返回 UNKNOWN。

B 与 A 并行、重叠、时钟倒置、父链成环、关联 scope 缺失或来源不可信时不得猜测排序，返回 UNKNOWN。

多次 B 采取 FAIL 优先于 UNKNOWN、UNKNOWN 优先于 PASS 的聚合；先检查所有 B，再归结结果，不能在第一笔 UNKNOWN 就提前返回。整体 PARTIAL 仍可在某个 B 的先决证据范围完整且可信时证明违规；只有一个较晚 A、但可能遗漏更早 A 时必须 UNKNOWN。

## 5. Required Evidence、Snapshot 与 Gate 修复

保留 `eval_status` 原值和现有用途，继续显示评测运行异常；不把 optional 故障改写成成功，也不隐藏 optional 错误。

新轨迹 Manifest 冻结 required/optional 集合；在评测结果与 Snapshot 中增加 `required_evidence_status = COMPLETE | INCOMPLETE`，只描述 required Binding 是否都存在、状态 succeeded 且 value 合法。

推荐纯函数签名：

```python
def required_evidence_status(
    policy: QualityPolicy | None,
    results: Sequence[TypedEvaluationResult],
) -> Literal["COMPLETE", "INCOMPLETE"]: ...
```

`policy is None`、required result 缺失、`failed/skipped/no_result`、无值或 provenance 不匹配一律 INCOMPLETE；optional 结果不参与此字段。

Snapshot 新契约用 01 定义的 `evidence_contract_version="trajectory-1.0"` 明确分支。仅该 discriminator 存在且被支持时按 `required_evidence_status`、执行状态与质量结论计算资格；不得用 Manifest 版本字符串大小比较。

新契约下 required evidence COMPLETE、执行成功、质量结论为 PASS 或 FAIL 时，Snapshot 可为 COMPLETE；质量 UNKNOWN、required 不完整、执行失败或用例缺失仍为 DIAGNOSTIC。

Optional failed/skipped 的 `eval_status=partial/failed` 仍可在结果面板解释；只要 required 集合和质量结论完整，不得单独使新契约 Snapshot 失去 Baseline 资格。

旧 Snapshot 没有 `evidence_contract_version`，继续使用现有 `eval_status` 判断；不回写历史记录、不推断历史 required 集合。

实现边界包括 `aggregation.py` 与 `release_gates.py`：新契约的 evaluation coverage / Gate 完整度按 required 集合算；optional 指标缺失仍保留诊断计数，不贡献伪造分数或 0。

`aggregation.py` 的 `compare_case_results()` 也识别新 discriminator，只允许 required 集合支撑正式结论；optional 缺项作为诊断，不令必需比较失效。`comparison_contracts.py` 继续负责三层冻结 digest 的可比性判断。

当既有聚合接口缺少 Snapshot/Manifest discriminator 时，为其增加显式 keyword-only 参数；旧调用默认走当前语义，避免全仓静默切换。

## 6. API 和 Case Detail 诊断

API 路由与响应字段遵循 01 §7 的 `GET /api/v1/experiment-launches/{launch_id}/trajectory`，query 精确绑定 `snapshot_id` 和 `dataset_item_id`。

服务端必须校验 Snapshot 属于 Launch、Case 属于 Snapshot，轨迹制品属于冻结 Item/Attempt；不从 Langfuse 在线端补旧证据。

旧 Snapshot 返回契约化 `HISTORICAL_TRAJECTORY_UNAVAILABLE`；证据缺失返回受控状态与 reason code，不伪装成 500 或空 PASS。

Console 仅从现有 [`CaseDetailPanel`](https://github.com/miniceM/Argus/blob/4b043739c63357ecc10131e1ba54bc1bc53d862a/services/console/src/features/launches/tabs/CasesTraceTab.tsx#L454) 懒加载；该组件当前已按固定 Snapshot 读取 Case 输出，并将 Launch/Snapshot/Case 放入 query key。

查询必须使用 01 定义的 query key，并同时包含 Launch、固定 Snapshot、Dataset Item；没有 Snapshot 时不发请求。

新增 `TrajectoryDiagnosis` 与 `TrajectoryStepList` 同属 launches feature；点击规则的 `evidence_step_ids` 定位对应 Step，Step 反向标出命中规则。

渲染 PASS、FAIL、UNKNOWN 三态时带文字和 reason code；执行成功、答案质量、轨迹质量和证据完整度分开展示。

并行 Step 以 DAG/并行分组展示，不按 `index` 伪装先后；REQUESTED、EXECUTED、BLOCKED 分开展示。

降级横幅说明“答案仍按原规则评测；轨迹证据不完整”，但已观察到的违规仍展示 FAIL。固定 Snapshot 与 Langfuse 当前在线 Trace 分区呈现，在线链接标注为当前观察内容。

UI 只消费脱敏 Step 摘要和后端结论，不在浏览器重算 QualityPolicy，也不直接请求 Langfuse Observation API。

新增 API 类型必须先导出 `docs/openapi.json`，再运行 `pnpm --dir services/console api:generate`；Console 不手写重复 DTO。

所有样式用语义 Token，遵循 `.agents/skills/argus-design-system/SKILL.md` 与 `AGENTS.md` §5.6；通过 `lint:tokens`、键盘访问和屏幕阅读器名称检查。

## 7. 目标文件清单

以下均为**后续实施提议**；本方案 PR 只新增本 Markdown，不改产品代码、测试或 API。

现有模块拟修改：`services/eval-runner/app/evaluators.py`、`evaluator_binding.py`、`manifest.py`、`models.py`、`api_results.py`、`result_snapshots.py`、`aggregation.py`、`comparison_contracts.py`、`release_gates.py`；Console 同步 `query-keys.ts` 和 CaseDetail 数据入口。

若 #128 已建立统一 Adapter/Acquisition，S4.2 只接入规则与聚合，不复制或重写其模块。

拟新增规则模块：`services/eval-runner/app/trajectory/rules.py`；拟新增测试工厂由 #128 提供 `tests/trajectory_fixtures.py`。

拟新增测试：`tests/test_trajectory_rules.py`、`tests/test_trajectory_required_evidence.py`、`tests/test_trajectory_api.py`。

拟新增独立数据：`tests/fixtures/trajectory/issue_129_slice.json`，覆盖 query-then-balance 与 no-transfer；不改 `data/dataset.json` 和既有 2/6、6/6 基线。

拟新增 UI：`services/console/src/features/launches/TrajectoryDiagnosis.tsx`、`TrajectoryStepList.tsx`、对应 `__tests__/CasesTraceTab.trajectory.test.tsx` 和 `services/console/e2e/trajectory-diagnosis.spec.ts`。

现有 `docs/openapi.json` 与 `services/console/src/api/schema.d.ts` 只由 API schema 工具生成，不手工编辑。

## 8. RED → GREEN 验证计划

以下命令是实施期预期命令，必须先写断言并实测 RED，再实现；本方案撰写期间未执行这些新增测试。

`RED`：`pytest -q tests/test_trajectory_rules.py tests/test_trajectory_required_evidence.py`；预期失败于缺失规则、required evidence 字段和 Snapshot 判定。

`RED`：`pytest -q tests/test_trajectory_api.py`；预期失败于 Snapshot 绑定校验、历史无证据响应和 evidence Step 映射。

`RED`：`pnpm --dir services/console test -- src/features/launches/__tests__/CasesTraceTab.trajectory.test.tsx`；预期失败于诊断懒加载、三态和 Step 跳转。

`GREEN`：运行上述 Python 与 Vitest 命令；同时运行 `pnpm --dir services/console typecheck`、`pnpm --dir services/console lint:tokens`。

E2E 在完成最小 API fixture 后执行：`pnpm --dir services/console test:e2e -- e2e/trajectory-diagnosis.spec.ts`。

最终门禁：`make validate`；涉及真实 Langfuse Observation 形态的集成验收按 #127 的本地集成环境执行，不能以 Mock 代替真实采集协议。

## 9. 逐项验收

- [ ] **S42-01**：返回体谎报工具名但 Observation 未出现调用时，`require_tool` 不得 PASS；输入证据仅来自冻结轨迹。
- [ ] **S42-02**：`REQUESTED/EXECUTED/BLOCKED` 区分可测；禁止执行默认 EXECUTED、显式禁止尝试按 REQUESTED；计数遵循冻结 phase/上限并输出 boolean 与 observed_count 证据。
- [ ] **S42-03**：每次 B 都有成功完成且可信因果先行 A 才 PASS；完整 `B → A → B` 必须 FAIL；先 UNKNOWN 后确证违规仍 FAIL；PARTIAL 中仅见较晚 A 不冒充完整负证据；时间/数组/parent 单独不足时 UNKNOWN。
- [ ] **S42-04**：PARTIAL 中已观察到的 forbid/order 违规逐规则仍为 FAIL；未见违规但覆盖不足为 UNKNOWN；HTTP-only 不伪造过程结论。
- [ ] **S42-05**：optional failed/skipped/no_result保持诊断且不降低required完整度；required缺Binding、failed/skipped/no_result、无值或provenance不符为INCOMPLETE/DIAGNOSTIC；required false为完整FAIL，Snapshot可COMPLETE、Gate仍拒绝；覆盖DB→Snapshot→Baseline/Gate；Writer序列化/提交故障整体回滚、无半契约Snapshot，Reader缺字段fail closed，旧Snapshot语义不变。
- [ ] **S42-06**：新API只返回选定Snapshot的冻结证据；历史缺证据、跨Case身份和Snapshot错配都有稳定响应；大于2^53的整数/高精度小数在artifact/API与Console参数显示不舍入，显示字符串与同artifact一致。
- [ ] **S42-07**：CaseDetail 懒加载、三态、并行/阶段、规则↔Step 定位及历史/在线标签通过 Vitest 与 Playwright。
- [ ] **S42-08**：规则变化进入 Measurement identity；Console OpenAPI 类型同步；`make validate` 通过，Demo 原有预期基线未改。

## 10. 依赖、风险与回退

前置依赖是 #127 对真实 Observation 字段与 profile 的验证，以及 #128 的 Acquisition、StandardTrajectory、因果元数据、冻结制品和最小脱敏；Context Adapter 与 Snapshot 资格切换在本期原子接入。

若 profile 无可信因果元数据，`tool_order` 只能 UNKNOWN；不允许临时降级为时间戳比较来换取通过率。

若业务工具没有非敏感 `workflow_scope_id`，只能按冻结的 `ROOT_SUBTREE` 整体流程语义评测，或返回 UNKNOWN；不得用客户敏感值创建关联键。

启用轨迹规则需要新 Manifest `1.3` Binding；关闭或移除新 Binding 可让后续 Launch 回到既有行为。既有 Manifest 和 Snapshot 不迁移、不覆盖。

新证据制品只追加；规则修复以新 EvaluatorVersion/新 Snapshot revision 纠正结果。不能通过重评回调 Agent 来补轨迹。

保留的风险：source profile 的因果可信度与采集完整性是规则成立的前提；它们未通过 #127 实测时，本期不能宣称跨框架正式发布可用。
