# S4.2 工具规则核心实施记录

核验日期：2026-10-14。关联 [Issue #5](https://github.com/miniceM/Argus/issues/5)、[Issue #129](https://github.com/miniceM/Argus/issues/129)。实施基线是 PR #178 的冻结 `EvaluationContext`，规范见 [S4.2 方案](../../plans/2026-10-13-s4-trajectory/04-s4-2-rules-and-diagnosis.md)。

## 本批交付边界

`app/trajectory/rules.py` 提供四类规则的严格参数与纯测量函数，复用 `EvaluatorBinding`、`build_result()` 和 `TypedEvaluationResult`。规则只读取本地制品加载器已验证的轨迹与引用，入口再次验证 schema、contract digest 和完整轨迹 digest，防止消费方修改深层容器后继续使用旧引用。业务 `output.tool_calls` 不参与测量。

本批尚未注册正式 EvaluatorVersion，也未接入 executor 的 TypedResult Adapter。它不改变 Launch 的规则执行集合或 Snapshot/Baseline/Gate 资格。后续分别交付正式冻结身份与执行接入、required evidence/新资格 discriminator、API/Console 诊断。四规则核心通过不表示 S4.2 或 Issue #129 全部验收。

## 参数与冻结身份

公共解析入口 `parse_rule_params()` 使用 `rule` discriminator，拒绝未知字段及类型强制转换。`max_count` 为非负整数，布尔值、浮点数、数字字符串均非法。

| 参数 | 语义 |
|---|---|
| `rule` | `require_tool`、`forbid_tool`、`max_tool_calls` 或 `tool_order` |
| `scope` | 默认 `ROOT_SUBTREE`，可显式冻结 `WORKFLOW` |
| `workflow_scope_id` | 仅 `WORKFLOW` 时必填；未观察到该工作流返回 UNKNOWN |
| `tool_identity` | 前三类规则的标准工具身份 |
| `phase` | 前三类规则默认 `EXECUTED`，可显式冻结 `REQUESTED` |
| `max_count` | 仅 `max_tool_calls` 必填，不合并请求与执行计数 |
| `before_tool_identity` / `after_tool_identity` | `tool_order` 的 A / B 标准身份 |
| `mode` / `prerequisite_status` | `tool_order` 固定为 `EVERY_AFTER` / `SUCCEEDED` |

核心仅接受 Manifest `1.3` 的 item scope、boolean Binding，且 `normalization_rule` 必须为空。现有通用 freezer 默认给 boolean 添加数值归一化，这种 Binding 在本核心入口被拒绝；下一批正式冻结轨迹版本时须显式冻结无归一化合同，不能在测量后篡改已冻结规则。`binding.params` 必须等于校验后补齐默认值的完整参数；`binding.evaluator_id` 是 `trajectory.` 加 `rule`。

冻结 `input_contract` 必须精确匹配以下结构，不接受缺字段、额外字段或另一算法：

```json
{
  "input_kind": "trajectory",
  "trajectory_schema_version": "1.0",
  "algorithm_version": "tool-rules-1.0",
  "rule_id": "trajectory.tool_order",
  "required_capabilities": ["ordering", "tool_executions", "tool_requests"]
}
```

其余规则声明所选 phase 对应的一个 capability。后续正式注册的版本与 artifact 必须覆盖整个规则模块及 helper；不能仅冻结 dispatcher 的函数体来冒充完整算法身份。测试中的 Binding 与 evidence ref 是合成身份，不能作真实注册或来源资格证据。

## 测量与安全证据

| 规则 | true | false | no_result / UNKNOWN |
|---|---|---|---|
| `require_tool` | 观察到所选请求，或成功的执行 | 对应 phase 覆盖完整但无合格 Step | 覆盖不足或匹配执行状态未知 |
| `forbid_tool` | phase 覆盖完整且未观察到禁止动作 | 观察到禁止动作，包括 PARTIAL 中的明确动作 | 未观察到违规但覆盖不足 |
| `max_tool_calls` | distinct Step 数量未超限且 phase 覆盖完整 | 已观察数量超限，PARTIAL 也保留 | 未超限但覆盖不足 |
| `tool_order` | 每笔 B 请求都有同工作流成功 A 的可信因果完成先行证据；无 B 仅在请求覆盖完整时为 true | 某笔 B 的先决范围完整，且证实无合格 A | 缺覆盖、缺关联或因果完成顺序无法证明 |

`BLOCKED` 不算 `EXECUTED`，先前的 `REQUESTED` 仍可被显式禁止尝试规则命中。数量只作为 boolean 测量的安全证据，不投影成 legacy numeric score。

排序使用显式 `causal_predecessor_ids` 的拓扑闭包，最多 1000 Step，迭代计算而不递归。parent、数组位置、展示 index 和时间戳不创建因果边；时间还必须支持 A 完成先于 B 请求。缺引用、成环、跨工作流边和时钟冲突阻断受影响节点，无关有效组件仍可判定。全局 phase 覆盖不完整时不推断局部工具缺席。

每笔 B 都检查后才归结，false 优先于 UNKNOWN。`B → A → B` 保留首笔违规；仅见较晚 A、但可能遗漏更早 A 时仍 UNKNOWN。成功排序返回确定选中的 A / B Step 引用，多个可用 A 时按 Step identity 选一个，展示顺序不影响结论。

指定 `WORKFLOW` 时，归属不明且可能相关的请求/执行使对应 scope 的缺席覆盖降为 PARTIAL；不能因过滤移除这些 Step 而判定缺席、无 B 或计数上界。已有成功工具、明确禁止动作、超限和已证实的某笔 B 违规仍保留。每笔已观察 B 的 A 缺席以执行范围完整性证明；请求覆盖 PARTIAL 会阻止整规则 PASS，却不抹掉该笔确定违规，请求能力 NONE 仍不能证明违规。

`succeeded + false` 是成功测量的违规值。无证据走 `no_result`，非法参数、Binding 或输入走有限错误码的 `failed`；不把 Pydantic 原始异常、工具参数或业务响应写入结果。安全证据仅含 `reason_code`、去重排序的 `evidence_step_ids`、`evidence_ref`，计数规则另含 `observed_count`。

## 验收与复现

自动测试覆盖响应谎报、phase 区分、失败/未知执行、部分覆盖与明确违规、EVERY_AFTER、无 B、跨工作流、并行/缺边/时钟/缺引用/环、未知优先级、引用/摘要损坏、严格参数、冻结身份错配、制品只读与 1000 Step 因果链。

```bash
python -m pytest -q tests/test_trajectory_rules.py
python -m pytest -q tests/test_trajectory_rules.py tests/test_issue_80_evaluator_versioning.py tests/test_issue_81_frozen_execution_identity.py tests/test_issue_82_typed_results.py tests/test_trajectory_assembler.py
make validate
```

RED：新模块缺失时测试收集失败；补充通过结果的证明 Step 测试实际失败于空 `evidence_step_ids`，随后补齐证明引用。最初夹具仅改显示名而未改来源工具字段，已修正；其错误不计作业务实现缺陷。

本批不修改 Observation 采集协议或冻结输入链路；来源协议沿用 [PR #178 冻结输入验证](../s4-1/frozen-input.md)。上述测试为纯规则/契约验证，不能代替后续正式 Evaluator 执行接入的真实来源消费者验收。

2026-10-14 最终核验：

| 门禁 | 结果 |
|---|---|
| 四规则核心 | 68 项，包含审阅后工作流归属不明、已知违规保留及布尔归一化拒绝；均在最终定向门禁通过 |
| PostgreSQL 关联回归 | 1066 PASS，JUnit errors/failures/skipped=0，42.05s；独立 loopback PostgreSQL、逐测试 schema，核验 owner 后清理 |
| `make validate` | 8 步 PASS：1765 Python、847 Console；Python 66.70s；Token lint、契约快照无 drift、Compose 通过 |
| 独立审阅 | 整批一次只读审阅 Critical 0、Required 2、Optional 0；7 项审阅回归及 1 项能力 NONE 回归实际 RED→GREEN，一轮修复，无二审 |

最终日志/JUnit 以 `t3-rules-final-reviewed-pg-targeted`、`t3-rules-final-reviewed-validate` 前缀保存在交付执行记录。普通门禁明确 deselect 67 项真实集成测试，未把其计作本批真实采集或规则接入验收。纯规则增量不改 UI，未重复 Console E2E；既有 datetime 警告保留。Cloud 仍由 PR #162 任务负责，不计占位检查为实测通过。
