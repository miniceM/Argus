# T8 Judge 默认 QualityPolicy 与冻结模式

日期：2026-10-14。基线 `72470b5`，关联 #131。实现依据
[S4.4 输出合同与执行流程](../../plans/2026-10-13-s4-trajectory/06-s4-4-judge.md)。

## 实现边界

默认策略对 `trajectory_judge` 使用 categorical `== "PASS"`：
冻结 `release` 模式为 required，`diagnostic` 为 optional。
默认策略和显式策略冻结均重新验证完整 Judge Binding，不能仅凭一段 mode 配置改变规则身份。
diagnostic 配成 required 时拒绝，错误码 `JUDGE_DIAGNOSTIC_RULE_REQUIRED`。
不具备必要规则的纯 diagnostic 任务仍由既有 `QUALITY_POLICY_NO_REQUIRED_RULE` 拒绝；
没有缩小正式 coverage 分母，也没有为诊断任务制造可发布结论。

`QualityRule.critical` 的冻结边界拒绝尚未获准的 Judge，错误码
`CRITICAL_JUDGE_SINGLE_SOURCE`，附中文说明；不能仅改变规则标志绕过 Binding 的保护。
完整的同风险双来源验证与联合校准尚待实现，当前不宣称 critical Judge 准入完成。
Judge 的 `release_eligible=false` / `JUDGE_ADMISSION_UNAVAILABLE` 保持不变：
规则判定通过不等于具备正式发布资格。

旧 Manifest 的策略反序列化、历史结果与旧 Evaluator 策略行为未改写。
新增策略检查只作用于新的策略冻结，不回写历史合同。
含 Judge 的默认策略使用 `default-typed-required` 与准确模式说明；
不含 Judge 的策略保留原 ID 和描述，避免改变既有指标策略身份。

## 验证

- 八项新回归确认 RED：**8 failed**（0.34 秒），覆盖默认 PASS、两种模式、
  纯 release/diagnostic、显式 required/critical 旁路和 Binding 身份变化。
- 初步三个模块 **92 passed**（10.94 秒）。
- 后续补充两种模式下 PASS/FAIL/failed/no_result/skipped 的判定矩阵，
  以及实际准备结果经默认策略汇总的 PASS/FAIL。
- 独立审阅无确认准入绕过，指出旧默认策略描述不符合 optional 模式；
  元数据回归 **2 failed / 16 deselected**，修复后最终三模块 **104 passed**（14.22 秒）。
- 最终 `make validate` 全部 8 步通过：Python **2627 passed / 0 skipped /
  86 real deselected**（195.05 秒），Console **864 passed**，
  契约快照、类型检查、构建与 Compose 校验通过。
  真实 ingestion 为独立必需门禁，86 项未运行不计通过。

```bash
pytest -q tests/test_trajectory_judge_quality_policy.py \
  tests/test_trajectory_judge_execution.py tests/test_issue_83_quality_policy.py
make validate
```

本次使用隔离 PostgreSQL 实例配置 `TEST_POSTGRES_URL`，跳过不能计通过。
真实 Provider/模型与 Token 上界、正式 Writer、Worker/recovery 及独立留出集校准仍未完成。
后续工作不等待 GitHub Actions；最终验收仍须核验适用 CI。
