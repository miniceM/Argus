# T8 冻结 Judge 执行器与准备结果桥接

日期：2026-10-14。关联 #131，依据 [S4.4 §5](../../plans/2026-10-13-s4-trajectory/06-s4-4-judge.md)。
本切片接入既有 Registry/version、Binding、预算 Adapter 和 typed 汇总；
**正式配置 Writer、Provider/Token 上界准入、Worker/recovery 接线、真实校准仍未完成**。
Judge 版本不默认选中，`release_eligible=false`，原因 `JUDGE_ADMISSION_UNAVAILABLE`。

## 实现与身份

`register_judge_version` 显式注册四种获准 Judge kind 的不可变版本。
完整 FrozenJudgeDefinition 使用 `params.judge_definition_json` 的规范 JSON 文本保存；
只将声明的 JSON 整数 Decimal 字段精确读回，不转换 bool/string，不经过 float 解码。
Prompt、Model、预算、输出合同、策略或校准身份变化会改变版本摘要；同 id/version 的修改被拒绝，
新版本产生不同 Binding。分类值仅 PASS/FAIL，无隐式 numeric 投影。

Judge 的实现制品为实际同步消费函数与关键准备/校验/传输/预算/存储源码摘要；
Definition 声明必须与实际制品相符。旧 Manifest、缺少 `pii-v1` 的合同不能准入。
冻结 Binding 的完整内容和辅助资格字段在发送前再核验，防止只复用 params 来领取另一身份的预算。
当前 critical 单来源配置保持拒绝；尚未实现符合真实准入与联合校准的两路 critical 配置。

## 异步准备与同步消费

`prepare_judge_evaluation` 只接受实际 DB/source 准备入口签发的请求 seal 和
`BudgetedJudgeProvider`，沿已提交的预算生命周期调用 Provider，重验响应后签发
`PreparedJudgeEvaluation`。封套固定 Binding digest、Item/Attempt scope、evidence ref、
input/criteria/Definition digest、evaluation generation 和 typed result。
凭证仅在同一 Runner 进程内供 await 后的同步汇总消费，不存入 Manifest、Snapshot 或 DB。

`evaluate_frozen_item` 新增 keyword-only `prepared_results` 和 `evaluation_generation`，
旧调用方式不变。消费阶段从当前固定 Context 重建最小请求，核验 scope、引用、criteria 和输入；
统一入口再次验证预期结果，执行器替换返回值不能绕过验证。
缺失/无效结果保持 UNKNOWN；超时、低置信和失败不造分数。
只保存受控 reason、当前 Step/ref、精确 confidence JSON 和实际 usage；不保存自由 rationale。
同步执行器不请求网络。等待 Provider 时的短事务与跨 Worker 限额由已实现的 Budget Store 负责，
最终 generation/lease CAS 仍须在后续 Worker 接线保留。

## RED / GREEN

- 初始缺模块 RED：**2 failed / 21 errors**；初步实现后 **23 passed**。
- Binding/资格篡改 RED：**5 failed / 30 deselected**；发送前完整重验已补齐。
- 旧 Manifest 准入 RED：**1 failed / 38 deselected**；在统一计划验证时拒绝。
- 替换准备值与异常原文 RED：**2 failed / 39 deselected**；统一消费验证及受控错误已补齐。
- 直接低层消费的嵌套 BaseModel 异常 RED：**1 failed / 41 deselected**；任意普通异常归一为受控失败。
- 独立审阅确认摘要遗漏证据校验链。源码替换 RED：**6 failed / 42 deselected**，覆盖
  evidence、trajectory checkpoint、execution checkpoint、db_models、db、config；
  补依赖后同时断言摘要变化、旧 Binding 报 `EVALUATOR_ARTIFACT_DIGEST_MISMATCH`。
- 最终八模块 **318 passed / 0 skipped**（28.81 秒），其中新执行器模块 **48 项**。
  覆盖实际本地 Checkpoint/Artifact 准备、跨 scope/criteria/generation、空目标集合、
  PASS/FAIL 经冻结 categorical QualityPolicy 的结论、旧执行器与实际 PostgreSQL 预算回归。
  Provider/Store 在新桥接单元测试中受控替代，不属于真实模型或整体验收证据。
- Ruff 与 `git diff --check` 通过。独立复审确认前述依赖漏洞已闭环。
- 补齐依赖后的最终 `make validate` 通过：Python **2607 passed / 0 skipped /
  86 deselected**（191.18 秒），Console **864 passed**，类型检查、构建、契约快照及
  Docker Compose 校验通过。86 项真实 ingestion 属于独立门禁，未计入本次通过数；
  此前未补依赖版本的 2601 项门禁不作为最终源码验证。

```bash
pytest -q tests/test_trajectory_judge_execution.py tests/test_trajectory_judge_definition.py \
  tests/test_trajectory_judge_input.py tests/test_trajectory_judge_provider.py \
  tests/test_trajectory_judge_budget.py tests/test_issue_80_evaluator_versioning.py \
  tests/test_issue_81_frozen_execution_identity.py tests/test_issue_82_typed_results.py
make validate
```

本次 PostgreSQL 回归使用隔离测试实例的 `TEST_POSTGRES_URL`；未配置时的 skip 不计通过。
既有真实 Reader 与 Langfuse 记录保留原 SHA，不重写为本切片证据。
GitHub Actions 后台运行，后续开发不等待 CI；最终验收仍须检查适用结果。
