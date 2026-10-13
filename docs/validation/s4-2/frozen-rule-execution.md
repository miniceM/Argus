# S4.2 正式规则冻结与执行接入

核验日期：2026-10-14。关联 #5、#128、#129；基线为 PR #179 的[四规则核心](tool-rules.md)，继续实施 [T3](../../plans/2026-10-13-s4-trajectory/08-implementation-plan.md#7-t3独立-observed-binding-与首批四规则)。

## 创建与冻结

四个正式 `EvaluatorDefinition/EvaluatorVersion` 使用 `trajectory.require_tool`、`trajectory.forbid_tool`、`trajectory.max_tool_calls`、`trajectory.tool_order`，版本 `1.0.0`，结果为 boolean，均为 opt-in。原四个 Output 指标及默认选择保持原身份。

创建接口新增可选 `trajectory_contract_json`，用有大小限制的 JSON 字符串承载完整合同，避免普通 JSON 小数解析丢失精度。服务端使用现有 canonical codec 与严格模型校验，再由不可变 AgentVersion 声明选来源分支；未知字段、非法合同返回有限 `TRAJECTORY_MANIFEST_CONTRACT_INVALID`，不返回原始配置异常。未准入来源仍由 QualifiedReaderFactory 拒绝，不因客户端提供合同而自动批准。

`evaluator_selections[].params` 冻结各规则参数。省略默认值与显式默认值归一为同一参数；未知字段、类型强制转换和规则身份错配被拒绝。轨迹规则没有显式合同不得创建；新公共请求仅提供合同、未选择轨迹 Binding 时返回 `TRAJECTORY_RULE_BINDING_REQUIRED`，拒绝发生在创建记录前。旧 Output 规则不接受非空新参数。

内部受控采集校准可显式调用 `LaunchService.create_launch(capture_only=True)`，用于 Reader 准入前验证合同与来源。公共 DTO 不提供此参数，注入同名 JSON 字段不能绕过校验。已冻结的历史 1.3 记录继续执行原有采集与恢复协议；本批不迁移历史记录。

以下是已有创建 API 的新字段示意，`trajectory_contract_json` 必须替换为完整有效合同：

```json
{
  "agent_id": "your-agent",
  "agent_version": "your-immutable-version",
  "dataset_name": "your-dataset",
  "trajectory_contract_json": "<完整 canonical TrajectoryContract JSON>",
  "evaluator_selections": [{
    "id": "trajectory.max_tool_calls",
    "version": "1.0.0",
    "params": {"rule": "max_tool_calls", "tool_identity": "banking.balance.read", "max_count": 1}
  }]
}
```

这是字段结构说明，不是可直接运行的示例凭据或已准入的生产合同。合同配置沿用 [S4.1 合同](../s4-1/contract.md)与[Reader 准入](../s4-1/qualified-reader.md)。

参数进入 Binding identity、Measurement 和创建幂等摘要；改变上限、phase 或 scope 会改变测量身份，不能用同一幂等键静默替换。没有新轨迹规则的旧创建请求保持原摘要结构。新 boolean 规则显式冻结 `normalization_rule=None`；旧 boolean 规则的归一化保持不变。默认 QualityPolicy 对新规则冻结 `== true` 的 required 条件，用户仍可显式配置独立策略；测量与决策摘要不混合。

## 实现制品与 TypedResult

沿用现有 `builtin_python` 执行器，不新增注册表、Executor 类型或结果 DTO。正式规则使用独立 implementation ref，制品除 dispatcher 还覆盖 `trajectory.bindings/rules/schema/codec/assembler/paths` 与 `evaluator_results` 七个实际模块源码摘要。冻结与恢复验证使用同一依赖计算；helper 或 JSONPath 编译器变化使旧制品失配，保持 fail closed。旧函数的源码制品计算结果不变。

共享入口仅对正式工具规则增加 Binding、schema、合同摘要输入；其他显式 Context 消费方继续收到原有 context，旧 Output 函数继续只接收 `output/expected_output`。

执行前要求 Context 的轨迹合同摘要与 Launch 的冻结合同一致。TypedResult Adapter 校验原 Binding provenance、boolean/value/status、无数值归一化，以及有限原因、Step 集合和原 evidence ref；保留 `succeeded + false` 与 `no_result`，不重建成成功或造分数。外来 provenance、未知原因、原文错误消息与越界证据返回有限 `TRAJECTORY_RULE_RESULT_INVALID`。

规则无证据时 required 结论 UNKNOWN；现有 `eval_status` 汇总仍可能为 `failed`，且没有执行异常消息。它不等于业务调用失败，也不抹掉逐规则 `no_result`。本批不修改该汇总行为。

## 验收与复现

```bash
python -m pytest -q tests/test_trajectory_rule_execution.py tests/test_trajectory_rules.py
python scripts/export_openapi.py
pnpm --dir services/console api:generate
make validate
python -m pytest -q tests/test_trajectory_rule_integration.py -m trajectory_local
make trajectory-test
```

真实门禁需已准备的隔离环境，按 [S4.0](../s4-0/README.md)执行；`make trajectory-test` 已加入正式规则消费者文件。不得把普通门禁的真实测试 deselect 或 PostgreSQL skip 当成通过。

真实消费者覆盖原生 OTel、Pydantic AI、HTTP-only、8s 迟到及未准入来源，分别验证正常执行、采集前中断和 READY 后中断。测试使用四个正式版本：require/order 在完整串行轨迹中为 true，forbid 与零上限 count 为已证 false；退化来源为 no_result/UNKNOWN。eval-only 禁止再调用业务 Agent 或 Reader，重评保持原 ref 与测量值；清理 Checkpoint 后历史 Snapshot 仍读取原制品。

TDD 覆盖注册缺失、参数/默认值/幂等、phase 能力、合同错配、版本声明、helper 制品变化、TypedResult provenance/错误文本、安全证据和公共 HTTP 创建精度。首轮夹具错误单列，不计作生产实现 RED。

单轮 Luna 审查：0 Critical、2 Required、1 Optional。两项 Required 以 4 个实际 RED 用例修复；Optional 为 Adapter 对 count 成功结果额外强制 `observed_count`，当前规则核心已始终输出该字段，留待后续适配器强化。

最终修复后实测（2026-10-14）：

| 门禁 | 结果 |
|---|---|
| 隔离 PostgreSQL 定向回归 | 1092 passed，0 failure/error/skip，44.95s |
| `make validate` | 8 步 PASS；Python 1791 passed，Console 847 passed；类型、Token、构建、两快照与 Compose 均通过 |
| 正式规则真实消费者 | 15 passed，0 failure/error/skip，102.52s |

普通门禁独立 deselect 82 项真实测试；本批真实消费者的 15 项单独运行，不将 deselect 计作通过。该真实矩阵为五种来源/准入条件 × 三个恢复阶段，9 项冻结结论为 FAIL、6 项退化为 UNKNOWN，均符合预期。原生 OTel `1.45.1`、Pydantic AI `2.55.0` 与本地 Langfuse `4.38.0`；未连接 Cloud。JUnit、15 项字段矩阵和完整日志已保存到任务工作记录。本批不重复宣称此前 Reader 资格矩阵是新实测。

## 交付边界

本批打通 T3 的正式冻结与执行链路，不启用 `required_evidence_status` 或新 Snapshot 资格 discriminator。T4 将原子实现 required/optional、聚合、Baseline 与 Gate 资格；T5 继续 CaseDetail 诊断和配置交互。Issue #129 仍需 T4/T5 全部验收后才能关闭。Cloud 工作流不在本批修改范围。
