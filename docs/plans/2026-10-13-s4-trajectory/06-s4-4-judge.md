# S4.4 Trajectory Judge 实施方案

状态：**PR #136 已批准方案；T8 正在 PR #181 实施。已实现严格输出解析与校准统计，异步执行、真实校准与准入尚未交付。**
核验基线：2026-10-13，`main@4b043739c63357ecc10131e1ba54bc1bc53d862a`。
关联：[Issue #5](https://github.com/miniceM/Argus/issues/5)、[#131](https://github.com/miniceM/Argus/issues/131)。
共同契约：[01 技术设计](01-technical-design.md)；阶段顺序见[完整实施计划](08-implementation-plan.md)，验收追踪见[验收矩阵](09-acceptance-matrix.md)。

## 1. 目标和现状

Judge 只补足确定性规则无法判断的语义问题，并把每次调用冻结为可审计的测量。
允许回答的问题限定为：真完成、计划合理性、浪费阶段、复杂政策符合度。
能由工具身份、状态、顺序、参数等确定性事实判定的条件应由规则处理。

当前已有冻结 Evaluator Binding：包含 `params`、`definition_digest`、实现制品摘要、执行器类型和 `binding_digest`。
2026-10-14 当前 `BuiltinPythonExecutor` 仍是唯一注册执行器；执行入口已有 keyword-only `context`，轨迹规则在受控 builtin 路径消费冻结制品，但尚无 Judge 异步准备与执行器。[实现](../../../services/eval-runner/app/evaluator_binding.py)
当前 typed result 支持 numeric、boolean、categorical、text；值缺失会成为 `no_result`，不会补零。
当前 QualityPolicy 已有 required/optional、critical、三态结论和 `unknown_not_releasable`。[策略实现](../../../services/eval-runner/app/quality_policy.py)
当前 evaluator 绑定可冻结实现制品；新增 Judge 输出验证器与留出集统计，但尚未接入 LLM Provider、Resolved Model 和正式执行器。[初始契约证据](../../validation/s4-4/contract-progress.md)
当前 optional 评测异常保留在 `eval_status`；T4 已独立计算 required 证据与 Snapshot 资格，T7 已在此基础上接通确定性门禁和 Snapshot 投影，Judge 阶段 B 仍待本期补验。当前接入点与 RED 顺序见 [T8 集成预检](../../validation/s4-4/preflight.md)。

Issue #131 当前只要求模型、Prompt、参数摘要冻结，校准报告和低置信分流，并在全量通过时不调用 Judge。
实施前应把本文 S44 验收项补入 Issue，明确诊断采样与正式发布覆盖不是同一口径。
Issue 所称“自动进人审队列”在本方案中落为 `UNKNOWN` 和原因码，不新增人工审核产品或工作流。

## 2. 边界与实施产物

新增路径 `services/eval-runner/app/trajectory/judge.py`，只负责 Judge 契约、调用适配、结构验证和校准统计。
新增 `services/eval-runner/app/trajectory/judge_provider.py`，只定义 Provider 协议和首个获准 Provider Adapter。
沿用现有 Evaluator Registry、Binding、Typed Result、QualityPolicy、Snapshot 与 Release Gate。
轨迹采集和规范化由 #127/#128 提供；Judge 不自行查询 Langfuse，也不重复组装 Observation。
Judge 对同一 Item/Attempt 复用冻结 `TrajectoryEvidenceRef`；同一制品可供多个 Judge 规则共享。
输入仅包含脱敏 `StandardTrajectory`、预先裁定的 expected criteria 和冻结 Step 引用。
不得把 Authorization、Cookie、原始 Header、未脱敏参数或完整任意 Trace 内容交给模型。
不得记录模型思维链；只保留受长度限制的结论说明、稳定原因码和 Step 引用。
不负责确定性轨迹规则、采集 deadline、Evaluator API、人工标注 UI 或人审任务队列。

## 3. 冻结配置与身份

每个 Judge Definition 必须在创建 Binding 前通过严格 Schema 验证；未知字段拒绝。

| 配置组 | 冻结字段 | 验证要求 |
|---|---|---|
| 任务身份 | `judge_kind`、`risk_id`、`version` | kind 仅允许四类；`risk_id` 非空且区分业务风险 |
| Provider | `provider_id`、`provider_api_version`、`resolved_model_id`、`resolved_model_artifact_digest` | ID 与模型制品摘要共同冻结；Provider 准入仍须证明实际解析身份，Schema 不证明目录名不可变。禁止 `latest`、别名或静默 fallback |
| 指令 | `prompt_text`、`rubric_text`、各自 SHA-256 | 原文进入版本制品；改任一文本即新 `definition_digest` 和新 Binding |
| 执行制品 | `implementation_ref`、`implementation_artifact.digest`、依赖锁摘要 | 运行时必须验证制品；制品不匹配时拒绝执行 |
| 解码预算 | `temperature`、`top_p`、`max_output_tokens`、`request_timeout_seconds`、`max_attempts` | 数值有限且在 Provider 支持范围；首版 `max_attempts=1` |
| 输入预算 | `max_steps`、`max_input_bytes`、`max_evidence_refs` | 超限产出 UNKNOWN 与原因码，不截断成可通过结论 |
| Launch预算 | max_total_calls、max_total_input_tokens、max_total_output_tokens | 固定Case×required Binding最坏预算先校验；不足时拒绝启动或UNKNOWN，不缩小覆盖分母 |
| 触发 | `mode`、`eligible_when`、`trigger_confidence_threshold`、`output_policy.min_confidence`、`sample_rate`、`sampling_seed` | 确定性触发阈值与 Judge 输出置信阈值分开冻结；表达式受限且确定，配置参加 digest |
| 覆盖 | `required_case_policy`、`required_case_ids_digest` | diagnostic 可采样；release 对必需 Case 全量覆盖 |
| 校准 | `calibration_set_id/version/digest`、`calibration_policy` | 标签、切分和阈值均绑定不可变版本 |

`definition_digest` 使用 canonical JSON + SHA-256，覆盖 Provider、Resolved Model、Prompt/Rubric、解码和输入预算、触发/覆盖、输出 Schema、实现制品及校准策略身份。
`binding_digest` 继续由现有 `EvaluatorBinding.binding_payload()` 计算，保留 quality threshold/critical 等策略字段分层。
Provider 返回的实际模型 ID 必须与冻结 `resolved_model_id` 相同；不相同产出 `JUDGE_MODEL_IDENTITY_MISMATCH`。
Provider 无法报告 Resolved Model 或不支持冻结的 JSON Schema/预算时，创建 Binding 失败，不以兼容模式猜测。
校准阈值属于业务策略版本；不得从环境变量、在线 Provider 默认值或当前注册表补值。

## 4. 请求、输出与异常语义

Judge 接收固定 EvaluationContext，但只读取 trajectory 内的脱敏 input/output、获准的 expected criteria 和 evidence ref；不得把为旧规则保留的原始 context.output/input_payload/expected_output 直接出站。expected criteria 在请求构造前通过同一 allowlist、内容与长度检查，安全摘要参加调用身份。Step 的因果/scope 字段只作为冻结上下文。
Adapter 的入口与规则输入保持同构，并显式接收 Runner 已验证的 Binding 与 Manifest schema：

```python
async def evaluate_trajectory_judge(
    context: EvaluationContext,
    params: JudgeParams,
    *,
    binding: EvaluatorBinding,
    manifest_schema_version: str,
    client: JudgeProvider,
) -> TypedEvaluationResult: ...
```

结果中的 evaluator/version/binding/definition digest、implementation provenance 均由 Adapter 从冻结 `binding` 构造；Provider 回复不能声明或覆盖 provenance。

```json
{
  "schema_version": "judge-output-v1",
  "verdict": "PASS",
  "confidence": 0.95,
  "reason_code": "TASK_COMPLETE",
  "evidence_step_ids": ["obs-123"],
  "rationale": "不超过冻结长度的证据摘要"
}
```

解析必须使用严格 JSON Schema：拒绝额外键、缺字段、NaN/Infinity、超范围置信度、超长文本和未知枚举。
`evidence_step_ids` 必须是当前冻结轨迹中存在且属于当前 Item/Attempt 的 ID；空引用只在 rubric 明确允许时通过。
Judge 不能自行生成 Observation ID；不存在、越界、重复或引用不完整时记录 `JUDGE_EVIDENCE_REF_INVALID` 并判 UNKNOWN。
Step 的 `arguments_redacted/result_summary` 作为不可信数据区传入；系统指令与用户提供的轨迹内容分隔编码。
Prompt 明确声明轨迹文本、工具输出和参数值都是数据，不可更改 rubric、输出格式或系统约束。
对 Provider 使用结构化输出约束；即使 Provider 宣称已约束，Runner 仍在本地再次校验 Schema 和 Step 引用。
`PASS`/`FAIL` 且通过 confidence、风险和引用校验，才构造 succeeded categorical typed result。
输出契约为 categorical，合法确定值仅 PASS/FAIL；QualityPolicy 默认 operator="=="、expected_value="PASS"。有序投影如需启用，显式冻结 FAIL→0、PASS→1；UNCERTAIN 不加入数字映射，而是 no_result。confidence 是模型自报量，不能当成已校准概率；准入仍依赖独立人标留出集。
`UNCERTAIN`、低于冻结 `min_confidence`、缺少必要 Step 证据统一形成 `no_result`，QualityPolicy 得到 UNKNOWN。
超时、网络错误、额度不足、模型身份变化、Schema 无效或 Provider 错误形成 failed typed result 和稳定错误码。
所有失败均不生成 `0`、`FAIL`、均值样本或空引用；失败详情限制长度并去除响应中的潜在敏感字段。
低置信度不是质量失败；它表示当前证据不足，Release Gate 按 UNKNOWN 阻止正式发布。

## 5. 执行与缓存流程

保留现有同步 EvaluatorExecutor 和 evaluate_frozen_item，异步网络调用放在 Worker 的受控准备阶段，不在同步函数里 asyncio.run。新增冻结 executor_type=trajectory_judge：resolve/validate 复用绑定和制品校验，execute 只消费已验证 prepared_results，缺失则 failed，不能自行发请求或回退模型。T3 已使 executor outcome 可保留 TypedEvaluationResult；本期增加 keyword-only prepared_results，按 binding_id 查找并再次校验 scope、input/criteria digest、provenance 和 evidence_ref，随后仍由同一 summarize_typed_results/QualityPolicy 汇总。

Worker 先 resolve_execution_plan，再 await Judge Adapter 得到绑定到当前 Item 的 Mapping[binding_id, TypedEvaluationResult]，然后沿既有 to_thread 执行同步汇总/持久化。Recovery 将现有流程拆成同步 prepare_recovery（claim lease、验证 checkpoint、选 needed bindings）、异步 Judge、同步 finalize_recovery（合并既有成功值并 CAS）三段；全程复用现有 EvaluationAttempt，Provider await 期间不持有 DB transaction/锁。旧 recover_evaluation 保持无 Judge 路径兼容，新 recover_evaluation_async 用于含 Judge 的队列 Worker；所有调用方逐一测试，历史 /experiments/run 继续固定旧 Binding，不自动启用 Judge。

Provider 用异步 HTTP client，在 asyncio.timeout 的冻结总预算内执行非流式单次请求，connect/read/write/pool 也分别限时；限制响应字节与 JSON 深度。首版并发 semaphore 为 2，配置、总预算及输入/输出限制随 Judge execution profile 冻结。CancelledError 必须向上传播并关闭 response、释放 semaphore；超时转换为 failed。任务取消/lease 丢失后即使 Provider 迟到也不能写结果，所有持久化继续 CAS。测试同时证明 event loop 心跳不中断、取消实际关闭请求、超时和恢复不重调 Agent。

1. Launch Manifest 1.3 冻结 Judge Binding、QualityPolicy 和 trajectory contract；旧 Manifest 走原 Reader。
2. Preflight 验证实现制品、Provider 能力、Resolved Model、Prompt/Rubric digest 和 required coverage。
3. 复用 #128 已冻结的同一 `TrajectoryEvidenceRef`；校验 Attempt、Contract Digest、Trajectory Digest 和 artifact 所属关系。
4. 对每个 Judge 计算 deterministic trigger；diagnostic 模式用冻结 seed 与 Case ID 决定采样。
5. release 模式逐一覆盖声明的 required Case；样本被采样掉、额度不足或 Provider 故障都不能降低 coverage 要求。
6. 以受限结构调用 Provider，严格解析；本地验证 response、confidence、风险和证据 Step refs。
7. 保存 Typed Result、`definition_digest`、trajectory evidence ref、duration、token/cost usage 和稳定错误码。
8. 走现有 `evaluate_quality_policy`；Snapshot 冻结结论与具体 Step 引用，后续重评读取同一输入制品。

Diagnostic 的触发条件默认为确定性规则 FAIL 或规则 confidence 低于冻结阈值；全 PASS 且高置信用例不调用。
Diagnostic `sample_rate` 使用 `SHA256(sampling_seed || dataset_item_id || judge_definition_digest)` 的定值区间判断，确保重跑同一 revision 可重现。
首版Release required_case_policy只允许ALL_DATASET_CASES，required_case_ids_digest必须等于冻结Dataset cohort；覆盖固定100%，不应用diagnostic eligible_when或sample_rate。不得把诊断样本均值用作正式发布值，也不得通过另选Case缩小分母。
每次调用前原子预留call/input/output token预算，实际usage回填；缺可靠token计数时用获准tokenizer的保守上界。诊断预算不足可skipped并保留原因，required预算不足为UNKNOWN。不能把费用统计当限额控制；若另配货币预算，须冻结计价profile并由实际Provider能力验证，本文不预写价格。

通过追加迁移在既有ExperimentLaunchRecord增加nullable judge_budget_state_json，旧Launch为NULL；它是可变运行账，不写入不可变Manifest。短事务锁Launch行，验证冻结Budget digest，以(item_id,evaluation_generation,binding_id)形成幂等reservation key、检查余额后预留，提交释放锁后才await Provider。JSON只含计数/状态/受控ID，key数量不得超过冻结max_total_calls。未发请求的取消可释放；已发送但usage未知按最坏预留计费，不因丢lease或丢响应退回预算。迟到结果不能写质量，但对应已发生调用仍计入账。测试用两Worker争抢最后预算、重复claim及发送前后取消验证不超支；不依赖进程内semaphore充当分布式预算。

同reservation已发送而无可靠结果时，重复delivery不得再调用Provider，返回JUDGE_CALL_OUTCOME_UNKNOWN；显式eval-only重评须新evaluation_generation且仍受原冻结Launch预算限制。Provider rationale先经同一脱敏/内容限制再保存，无法证明安全的自由文本省略，仅保留reason/Step引用，不直接进日志/Score。
仅允许同一进程、同一冻结 scope/Artifact 内复用 (artifact_id, binding_digest, expected_criteria_digest) 的已验证结果；禁止跨 Case/Binding 复用或引用别的 Item Step。运行 usage 是诊断，不改输入 digest；实际输出随 Result revision 保存，不承诺模型逐次输出相同。
一次调用失败后不自动重试；若未来开放 retry，必须计入冻结 `max_attempts`、费用和幂等策略并形成新版本。
Worker 取消与恢复复用现有 generation/lease/CAS；旧 generation 响应不能覆盖新结果。

## 6. Critical Judge 准入与校准

Critical 结论必须有两路适配同一 `risk_id` 的证据：同风险确定性规则 + Judge，或两个不同冻结评判身份的 Judge。
判定身份按 evaluator ID、Provider、Resolved Model、Prompt/Rubric digest、实现制品 digest 合成；仅改名称不能构成第二来源。
双 Judge 必须以联合输出在留出集校准；两次调用相同 Model/Prompt 的重复采样不视作独立来源。
缺少上述任一条件时，在 QualityPolicy/ReleasePolicy 冻结边界拒绝配置，稳定错误码 `CRITICAL_JUDGE_SINGLE_SOURCE`，附中文提示。
critical 规则不得采用 diagnostic 抽样；其 required Case 覆盖在 release 中必须全量。

校准数据由业务离线提供已标注 Case，按 `risk_id` 保存固定样本 ID、truth label、轨迹 digest 和隔离字段。
Prompt 调优只能读取 development split；验收指标只能从独立 holdout split 计算，Case/近重复不得跨 split。
每个 critical risk slice 以真实违规为分母：`false_accept_rate = false_accepts / ground_truth_violations`。
报告同时记录样本数、false accepts、false accepts rate、单侧 95% Wilson 上置信界、false rejects、重复运行分歧和输入覆盖。
冻结的 CalibrationPolicy 必须声明 max_false_accept_rate、min_samples_per_critical_slice、confidence_level、max_disagreement_rate、repeat_runs、切片定义和 holdout digest。分歧率=同一冻结输入的重复 verdict 不一致 Case 数/重复评判 Case 总数，repeat_runs 至少 2；不将模型自报 confidence 用作该统计。
其中 `confidence_level` 首版取 0.95；业务 `max_false_accept_rate` 与最小样本数必须由具体策略版本明确设置。

单侧 Wilson 上界采用 `z=1.644854`（95%）：

```text
U = (p + z²/(2n) + z × sqrt(p(1-p)/n + z²/(4n²))) / (1 + z²/n)
p = false_accepts / n；n = holdout 中该 risk slice 的真实违规数
```

每个 critical slice 的 `n < min_samples_per_critical_slice`、缺少违规标签或 `U > max_false_accept_rate` 均判校准不合格。
双 Judge 的准入必须对联合输出计算上述 U；分别通过的两个 Judge 不自动证明联合系统安全。
Demo 策略样例固定写为 max_false_accept_rate=0.10、min_samples_per_critical_slice=30、confidence_level=0.95、max_disagreement_rate=0.05、repeat_runs=2。
该样例是可重复的 CI 教学夹具，不是 Argus 的通用安全阈值；生产策略须由业务风险所有者另行固定并留痕。
30 个违规样本且 0 个误放行时 Wilson 单侧上界约为 0.0827；发生 1 次误放行时上界约为 0.136，样例应判失败。
校准不足、Wilson 上界超阈值或重复 verdict 分歧率超过冻结 max_disagreement_rate 时，不开人审队列；Judge 结论记 UNKNOWN，critical Gate 不可通过。

## 7. 具体改动文件

| 文件 | 实施内容 |
|---|---|
| `services/eval-runner/app/trajectory/judge.py` | Judge 配置、request/response schema、入口 Adapter、触发、输出验证、校准报告和 Wilson 计算 |
| `services/eval-runner/app/trajectory/judge_provider.py` | async Provider protocol、单一首版 Adapter、timeout/预算、Resolved Model 核验 |
| `services/eval-runner/app/evaluator_binding.py` | 注册 trajectory_judge、校验 prepared_results 并复用同步 typed 汇总；旧调用保持兼容 |
| `services/eval-runner/app/evaluators.py` | 四类 Judge Definition 的版本注册与唯一输出契约 |
| `services/eval-runner/app/evaluator_results.py` | 保存 Judge typed status、稳定错误码、evidence refs 与 provenance |
| `services/eval-runner/app/quality_policy.py` | critical 来源关系和 required/diagnostic 约束的冻结前验证 |
| `services/eval-runner/app/worker.py`、`evaluation_recovery.py` | await Judge 后汇总；恢复拆 prepare/await/finalize，网络期间无 DB 锁，保持 generation/CAS |
| `services/eval-runner/app/db_models.py`、预算预留入口与追加迁移 | Launch运行预算JSON/行锁幂等预留，不改不可变Manifest，不新建费用系统 |
| `services/eval-runner/app/manifest.py`、`evaluator_binding.py` | 明确 1.3 Reader/Writer 分支并冻结 Judge 与校准身份 |
| `tests/test_trajectory_judge.py` | Digest、注入输入、结构化输出、证据引用、UNKNOWN 和预算行为 |
| `tests/test_trajectory_judge_calibration.py` | Wilson 边界、分层样本资格、双 Judge 联合误放行拒绝 |
| `tests/trajectory_fixtures.py` | 复用 #128 提供的轨迹工厂，不另造 Observation schema |
| `TECHNICAL_DESIGN.md` | 增加冻结 Judge、校准准入和 UNKNOWN 的设计说明 |

不在本 issue 增加 Judge 管理 UI、新模型目录、长期请求队列或人工复核服务。

## 8. TDD 命令与验证次序

以下命令是实施者之后执行的验收路径；本方案 PR 只写文档，没有创建测试或执行它们。

RED：先新增测试和固定 fake Provider，确认缺少行为时按预期失败，不调用外网模型：

```bash
pytest -q tests/test_trajectory_judge.py
pytest -q tests/test_trajectory_judge_calibration.py
```

覆盖 Prompt 变更、alias 拒绝、结构无效、注入字符串、伪造 Step ID、低置信、Provider 异常均以非 PASS 结束。
覆盖 diagnostic 全 PASS 零调用、失败/低置信采样确定性、release required Case 每例一次调用及费用预算。
GREEN：实现最小 Adapter 后重跑两个新测试，再核对旧冻结执行和 QualityPolicy 契约：

```bash
pytest -q tests/test_trajectory_judge.py tests/test_trajectory_judge_calibration.py
pytest -q tests/test_issue_80_evaluator_versioning.py tests/test_issue_81_frozen_execution_identity.py tests/test_issue_82_typed_results.py tests/test_issue_83_quality_policy.py
```

最终仓库门禁由整套实施 PR 执行 `make validate`；本方案提交不宣称该命令已运行。

## 9. 验收标准

| ID | 验收项 | 通过证据 |
|---|---|---|
| S44-01 | 更换 Prompt 或 Rubric 文本 | `definition_digest` 与新 Binding 改变；旧 Snapshot 仍保存旧 digest 和结论 |
| S44-02 | Model alias 解析与运行 | Manifest 冻结 Resolved Model；运行实际身份不符时报稳定错误，不 fallback |
| S44-03 | 参数冻结 | Provider、预算、解码、输入限制、触发、覆盖和 calibration policy 都进入 binding identity |
| S44-04 | 输出校验 | 缺字段、额外字段、无效枚举/置信度、越界 Step ref 全部为 UNKNOWN 且有错误码 |
| S44-05 | 防注入 | 轨迹中包含“忽略规则”等文本仍只作为数据；输出需满足本地 Schema 和证据校验 |
| S44-06 | 不补零与取消 | 错误/超时/无效 JSON/低置信不生成伪值；并发≤2、event loop 心跳持续、取消关闭 response/释放许可，迟到不写入 |
| S44-07 | 证据定位 | 每个确定 verdict 的 `evidence_step_ids` 均属于冻结轨迹且可在 Case Detail 精确定位 |
| S44-08 | 成本保护 | diagnostic全通过零调用、采样稳定；两Worker竞争/重复claim/发送后取消不超预算，未知usage保持最坏预留 |
| S44-09 | 发布覆盖 | ALL_DATASET_CASES、IDs digest与冻结cohort相等；每required Judge调用数=Case总数，不套diagnostic触发/采样；漏评不能COMPLETE |
| S44-10 | 单来源拒绝 | Critical single Judge 创建/冻结失败，错误码为 `CRITICAL_JUDGE_SINGLE_SOURCE` 且提示中文 |
| S44-11 | 校准报告 | 输出 risk slice 样本、误放行率、单侧95% Wilson上界(z=1.644854)、按冻结 repeat_runs 计算的分歧率和 holdout digest |
| S44-12 | 样本资格 | 不足最小样本或 UCB 超阈值时校准不合格，critical conclusion 为 UNKNOWN |
| S44-13 | 联合校准 | 同风险规则+Judge 或双身份 Judge 才可准入；双身份需联合 holdout 校准 |
| S44-14 | 旧版本可读 | 显式支持 Manifest 1.0/1.1 legacy、1.2 与旧 Snapshot；1.3 Judge 走 await→prepared result；未知版本拒绝；恢复与同步旧调用均有测试 |

## 10. 依赖、风险与回退

前置：#127 证明真实 Observation 关联，#128 提供标准轨迹、脱敏策略、冻结制品和 Step ref，#129 提供规则三态及 required evidence。
Judge 不要求 #132 已经先完成；完整计划在 T6 的参数/脱敏合同稳定后执行 T8，再由 #132-B 接入正式 Gate。
最大风险是 Provider alias 漂移、提示注入、holdout 泄漏、额度/超时造成 required coverage 缺口和校准样本偏差。
缓解依次为冻结 resolved identity、数据与指令隔离、本地结构/Step 校验、单次有界调用、校准集分离和 fail-closed。
回退通过新 Launch 禁用 Judge Binding；不删除旧 Binding、Evidence Artifact 或 Snapshot，不重写历史结论。
Provider 不可用时保留 failed/no_result 和 UNKNOWN；禁止临时改用另一个模型后沿用旧 digest。
