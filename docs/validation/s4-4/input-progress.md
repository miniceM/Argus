# T8 固定证据与安全 Judge 请求

日期：2026-10-14。关联 #131；依据批准的 [S4.4 §3–§4](../../plans/2026-10-13-s4-trajectory/06-s4-4-judge.md)。依赖[冻结定义与触发](definition-progress.md)。本切片提供正式准备入口，尚未接 Provider、Worker 或恢复执行器，不表示 Judge 完成。

## 实现与来源边界

`JudgeCriteriaPolicy` 冻结投影版本、有限路径、string policy 与 `pii-v1`，复用现有 RedactionPolicy 的路径和敏感键校验；拒绝根路径和重复路径。criteria 先投影再摘要，未获准字段不出站、不参加安全 criteria 摘要；获准字段缺失、未观察或无法证明安全时不可用。

实际入口 `prepare_judge_request` 在短事务内调用原 `context_for_item`，验证完整 Manifest、Checkpoint 原 Dataset input/expected_output、Item/Attempt scope、不可变 Artifact、合同和制品摘要。只有完成这些校验后，才对经过验证的 expected_output 预裁定。调用者提供的 criteria 必须通过 scope/policy/摘要重验，并与来自冻结 Dataset 的安全投影一致；同 Case 替换为另一个合法条件也拒绝为 `JUDGE_CRITERIA_SOURCE_MISMATCH`。这避免只验证调用者自己生成的哈希。缺失/损坏制品不以在线 Trace 或原始业务响应补齐。

纯 `build_judge_request` 不访问任意业务 Context 的 output/input_payload/expected_output，只读取固定轨迹、证据引用和安全 criteria；复用证据层验证脱敏叶、映射、库存、来源和完整度。system 仅含冻结 Prompt/Rubric 和不可信数据约束，user 数据区只含最小脱敏轨迹与 criteria。排除 raw_name、任意 Header 和原始 Trace，保留本地 Step 与因果；签名/终结清单已在证据层验证，不作为任意业务 result 发给模型。

`required_capabilities` 随定义冻结，声明项必须 COMPLETE，release 另外要求 OBSERVED_COMPLETE。能力根据 rubric 的实际证据需要声明；例如无用动作可以依据动作与因果判断，不硬编码为必须 timing。超出 max_steps 或整份 UTF-8 messages 的 max_input_bytes 时拒绝，不截断为可判通过请求。

调用身份含固定 scope、evidence_ref、definition_digest、criteria_digest、input schema 与完整 messages 摘要；结果元数据不带消息原文。准备阶段无网络调用，返回离线请求后后续异步等待必须退出 DB 事务；本切片不证明实际 Provider 的超时、取消或持久预算。

## 验证

- 初始域测试实际 RED 17 failed（请求入口缺失），GREEN 覆盖 system/data 分离、Forbidden 原始 Context 字段、确定性摘要、敏感内容拒绝和未知字段丢弃、证据 scope/ref/合同异常、预算与能力边界。
- 原 Checkpoint/Artifact 事务准备测试初始 RED 6 failed（真实准备入口缺失），补入口后 SQLite/PostgreSQL 的有效、删除与损坏制品均验证。
- 来源绑定语义 RED 4 failed：两种数据库都接受了同 Case 替换条件与冻结 Dataset 中的敏感 expected。补 authoritative 预裁定后拒绝；另覆盖冻结 expected 缺失，不泄漏 synthetic secret。
- 语义浪费动作无需 timing 的回归实际 RED 1 failed，删除不符合方案的硬编码 kind→能力下限；仍验证显式声明能力缺失时不生成请求。
- 五个相关模块最终 **216 passed / 0 skipped**（21.81s），包括输出、定义、请求、证据和冻结输入。ruff 通过。

```bash
python -m pytest -q tests/test_trajectory_judge_input.py tests/test_trajectory_judge_definition.py tests/test_trajectory_judge.py tests/test_trajectory_evidence.py tests/test_trajectory_inputs.py
make validate
```

最终源码 `make validate` 8 步通过：隔离 PostgreSQL 的 Python **2455 passed / 0 skipped / 86 real deselected**（162.13s），Console **864 passed**；OpenAPI/schema 同步、Token lint、typecheck、build 和 Compose 校验通过。独立只读源码复审未发现确认的阻断缺陷，并复核了权威 criteria 来源绑定；复审未独立运行测试，测试证据来自上述实际运行。图谱未覆盖新增文件，已直接核对当前源文件。阶段测试不替代真实 Provider / 模型身份、独立 holdout、跨 Worker 预算或 89 项整体验收。

## 下一步

获准异步 Provider/模型解析、持久 Launch 预算、正式执行器与 Worker/recovery、critical 联合来源、真实独立 holdout 和最终版本审计仍未完成。继续在 PR #181 交付。
