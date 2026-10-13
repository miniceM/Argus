# S4 统一验收矩阵与证据要求

状态：提议 / 待实施。本文映射分期验收到测试、任务和 issue 关闭条件；不是测试通过报告。公共契约见 [01](01-technical-design.md)，执行顺序见 [08](08-implementation-plan.md)。分期验收 ID 的完整定义保留在对应 02–07 文档，以免复制后口径漂移。

## 1. 四类证据必须分开

| 类型 | 能证明什么 | 不能替代什么 |
|---|---|---|
| Unit / deterministic fixture | 规则真值、组装算法、digest、参数边界、错误映射 | 真实 Agent instrumentation 或 Langfuse 可查询性 |
| Contract / DB integration | 冻结身份、持久化、revision/CAS、Snapshot/Gate 完整链 | OTel exporter ingestion 的时延和字段 |
| Real local integration | 本地真实 Runner→Agent→Langfuse、trace/root/item/attempt、字段/延迟 | 生产平台完整性或所有未埋点业务动作 |
| Console E2E | 三态、固定快照、Step 定位、回链、可访问交互 | 后端真实采集或正式发布安全证据 |

Mock 只允许标 Unit/Contract；PR #133 的 CI 通过可证明该提交现有检查通过，不能改标为 S4.0 真实链路通过。服务未配置、字段缺失、测试跳过必须记录阻塞。真实本地验收是 #127 的硬门槛。

## 2. 分期验收到任务与测试的映射

所有 `S40-*`、`S41-*`、`S42-*`、`S43-*`、`S44-*`、`S45-*` 必须有独立证据记录。下表按风险组映射，不允许用一次 `make validate` 摘要代替各项行为断言。

| 验收范围 / 权威定义 | 任务 | 测试层级与拟新增文件 | 必须保留的证据 |
|---|---|---|---|
| [S40：隔离服务与真实链路](02-s4-0-observation-validation.md) | T0 | Real local；`tests/test_trajectory_observation_integration.py` | 两框架真实 Observation、parent/root/item/attempt 关联、loopback 地址与版本矩阵；不含凭据 |
| [S40：字段、延迟、分页、HTTP-only](02-s4-0-observation-validation.md) | T0 | Real local + Unit | 字段presence/phase/status、参数可见性、HUMAN来源、显式duration、迟到时间线、关闭条件、HTTP-only答案 |
| [S41：Schema、Normalizer、作用域](03-s4-1-acquisition-and-schema.md) | T1 | Unit/Contract；`test_trajectory_schema.py / test_trajectory_assembler.py / test_trajectory_profile_selection.py` | 两框架同profile_set合同，profile持久化/冻结/spec_digest及旧空值兼容，Trace不可覆盖；兄弟Item/其它Attempt排除；terminal inventory缺号不能COMPLETE |
| [S41：分页、并发、限额、digest](03-s4-1-acquisition-and-schema.md) | T1 | Unit + Real local；`test_trajectory_acquisition.py` | deadline/页/step/字节、cursor环；固定输入稳定digest，Step.duration变化新digest，采集/组装耗时排除 |
| [S41：Artifact、恢复、历史](03-s4-1-acquisition-and-schema.md) | T2 | PostgreSQL/Contract；`test_trajectory_evidence.py` | insert-only/FK/唯一键、Decimal canonical TEXT精确往返、原deadline、完整性校验、旧generation fencing、Agent调用计数不增 |
| [S42：require/forbid/order/count](04-s4-2-rules-and-diagnosis.md) | T3 | Unit/Contract；`test_trajectory_rules.py` | 自报绕过反例、B→A→B、phase区别、PARTIAL已证违规、并行与UNKNOWN |
| [S42：required/optional与Snapshot](04-s4-2-rules-and-diagnosis.md) | T4 | DB/Contract；`test_trajectory_required_evidence.py` | 保留optional诊断；required缺失/状态/值/provenance矩阵；完整false仍COMPLETE但Gate拒绝；资格字段/discriminator原子写入、故障整体回滚、半契约Reader拒绝 |
| [S42：API、Case诊断和旧历史](04-s4-2-rules-and-diagnosis.md) | T5 | API/Vitest/Playwright；`test_trajectory_api.py` | 跨身份404、旧无证据原因、固定query key、三态截图、规则定位Step和键盘交互 |
| [S43：安全参数和剩余规则](05-s4-3-arguments-and-redaction.md) | T6 | Unit/Contract；`test_trajectory_arguments.py` | JSONPath与类型边界、可观测缺字段FAIL/不可见UNKNOWN、重复参数语义、HUMAN来源 |
| [S43：脱敏、digest、投影](05-s4-3-arguments-and-redaction.md) | T6 | Unit/DB/API/Projection；`test_trajectory_redaction.py` | Snapshot/API/日志/Score泄漏扫描；Mapping不变而Redaction/Measurement变化；threshold仅Policy变化 |
| [S44：冻结Judge、故障、成本](06-s4-4-judge.md) | T8 | Unit/DB/Contract；`test_trajectory_judge.py` | Prompt/Model身份、prepared_results校验、prepare→await→CAS且无DB锁、取消/heartbeat、结构化输出、防注入、timeout无假0；两Worker原子预算、重复claim不重复发送、未知usage保守计费；恢复不invoke Agent |
| [S44：校准、critical、覆盖](06-s4-4-judge.md) | T8 | Deterministic math + labelled holdout；`test_trajectory_judge_calibration.py` | 风险slice/样本数、单侧95% Wilson上界(z=1.644854)、repeat分歧率、联合误放行、单来源拒绝、冻结CalibrationPolicy |
| [S45：确定性Gate、Baseline、比较](07-s4-5-release-gate.md) | T7 | DB/API/Console；`test_trajectory_release_gate.py` | UNKNOWN/critical阻断、COMPLETE但FAIL、DIAGNOSTIC拒绝、三层原因、实例digest不同仍可比 |
| [S45：Judge、投影、完整回归](07-s4-5-release-gate.md) | T7/T9 | Real local/DB/Console/CI | 覆盖不一致withheld；generation不变的新Snapshot独立Score/任务，旧投影保留；当前状态/Run evidence过滤、历史保护、原Demo与完整门禁 |

## 3. 母 issue #5 的整体验收

| ID | 场景与输入 | 预期结果 | 负责 / 证据 |
|---|---|---|---|
| M5-01 | 同一测试AgentDefinition的原生OTel/Pydantic AI两个不可变版本完成同一任务 | 同EvaluatorVersion、profile_set和Mapping；实际profile provenance可不同，合同/规则语义一致 | T0/T1/T3/T7；真实Agent.run及依赖闭包、版本、Trace/Step refs、typed results和合同digest |
| M5-02 | 独立slice要求查询客户→查询余额，故意执行禁止转账 | forbidden规则FAIL并定位违规Step；order规则按每个B和因果作用域判断 | T3/T5；Case UI、规则原因、Step和冻结结果 |
| M5-03 | Agent响应谎报tool_calls，完整观测实际无工具 | observed require_tool为false；旧声明规则版本不变 | T3；响应/轨迹隔离的反例测试 |
| M5-04 | HTTP-only Agent只产生Remote HTTP Root | 最终答案仍评；required轨迹缺失UNKNOWN且不可发布；optional诊断缺失不污染必需资格 | T4/T5；两种Policy的Snapshot/Gate链 |
| M5-05 | 参数已全脱敏；另一Case具有获准但错误参数 | 前者UNKNOWN；后者按明确匹配约束FAIL；所有持久化和界面无敏感原文 | T6；泄漏测试、visibility与typed结果 |
| M5-06 | 在Case中选择规则并打开原始Trace | 正确定位Step、展示获准摘要；原始链接真实且标在线内容；快照结论保持冻结 | T5/T9；Playwright操作与身份断言 |
| M5-07 | 同一Case重评、迟到Span、旧generation和Langfuse断连 | Agent不重调用；旧Snapshot不变；新证据新revision；新Snapshot的Score/任务独立、同revision重投幂等 | T2/T7/T9；调用计数、revision/digest、CAS与Score ID |
| M5-08 | 改规则/Assembler/Redaction、只改阈值、Judge覆盖不匹配 | 分别移动正确合同身份，给出MEASUREMENT_CHANGED/QUALITY_POLICY_CHANGED/覆盖原因；不误报正式Agent回归 | T6/T8/T9；分层比较与withheld证据 |
| M5-09 | Critical Gate使用单Judge或缺必需证据 | 创建时拒绝单来源；运行缺证据UNKNOWN不可发布；不被平均分稀释 | T8/T9；稳定错误码与Gate结果 |
| M5-10 | 原Demo v1/v2和旧Launch/Manifest/Snapshot | v1=2/6、v2=6/6；历史只读兼容、不补造observed evidence | 全阶段；现有测试原样回归 |

关闭 #5 需要 S40–S45、M5-01–M5-10 全部有实际证据，不能只关闭后端或前端部分。

## 4. 证据记录格式

每验收项一条记录；可合并运行命令，但每条行为断言仍分别标注。报告建议作为实施 PR 附件或提交到 `docs/validation/s4/`（该目录在实施时建立），全部原始敏感数据留在获授权的原始系统。

| 字段 | 约束 |
|---|---|
| `acceptance_id / issue / task` | 明确S4或M5 ID与T任务 |
| `result` | PASS / FAIL / BLOCKED / NOT_RUN；只有实际行为一致才PASS |
| `git_commit / runner_build_id` | 实际固定提交/制品身份，不能latest/dev |
| `dataset_version / agent_version / evaluator_binding / policy_digest` | 正式验收四维身份与冻结策略 |
| `source_profile / langfuse_server / sdk / framework_versions` | Real local必须有；版本锁或digest可查 |
| `command / exit_code / executed_at / duration` | 原生命令、实际输出摘要和时间；退出码单独不能证明行为 |
| `expected / observed / assertion_refs` | 可比较的预期、实际与测试断言位置 |
| `trace_id / root_observation_id / item_id / attempt_id` | 真实集成必填，只有获准ID；不得包含凭据或raw业务payload |
| `artifact_ref / trajectory_digest / contract_digests / snapshot_id` | 根据阶段填写实际身份，用于复现和历史审计 |
| `limitations / blocked_conditions / recovery` | 明确环境、未测范围及解除条件；不要把SKIP归为PASS |

RED记录需证明测试表达需求且因目标缺口失败；缺依赖/端口/密钥导致的失败是环境阻塞，不是行为RED。GREEN记录包含相同行为断言及目标回归。数学校准的确定性测试与实际人标/模型校准报告分别记录，不能用合成输出宣称生产模型已校准。

## 5. 错误与质量状态验收

| 条件 | Typed / 质量 | 对外行为 |
|---|---|---|
| 完整证据明确违反规则 | succeeded + false / FAIL | 违规Step及机器原因；critical可一票否决 |
| PARTIAL但独立可信证据已证明禁止调用 | succeeded + false / FAIL | 保留已知违规和完整性warning |
| 缺所需观测、无法证明顺序或参数不可见 | no_result / UNKNOWN（required） | 原因与缺能力；不造0、不画绿PASS |
| 冻结optional trigger未命中 | skipped / optional UNKNOWN | 诊断可见，必需完成度不受影响 |
| Provider/解析/实现失败 | failed / UNKNOWN（required） | 稳定安全原因，不泄漏上游文本 |
| Artifact缺失/损坏/身份不符 | 恢复受阻 / UNKNOWN | 不重调Agent、不用最新Trace偷换 |
| 旧Snapshot没有trajectory | HISTORICAL_TRAJECTORY_UNAVAILABLE | 历史最终答案可读，轨迹显式不可用 |
| 映射/脱敏/算法变更 | MEASUREMENT_CHANGED | 正式比较withheld，保留诊断 |
| 只改threshold/required/critical | QUALITY_POLICY_CHANGED | 不误报正式Agent回归 |

基础错误码由各期方案定义；公共码包括 `TOOL_MAPPING_UNRESOLVED`、`CRITICAL_JUDGE_SINGLE_SOURCE`、`HISTORICAL_TRAJECTORY_UNAVAILABLE`。实施时在类型契约与错误目录中集中声明，不能只在中文字符串里编码语义。

## 6. 性能与故障验证

以 1/100/1000 Steps、1/10/20 页、8 MiB 输入上界、2 MiB Artifact上界作为可复现容量样本。证明每个Case的多规则共享一次已验证轨迹；Reader并发不超过4；cursor环、取消、timeout都收敛；Judge调用符合冻结覆盖合同。记录Assembly p50/p95、采集deadline命中率、降级率、step数量、读取字节、模型调用/token/cost。

这些尺寸是方案初始配置与基准样本，不是生产性能SLA。S4.0测量超出deadline的Profile应降低资格或创建校准后的新合同，不能为通过测试放宽所有Profile完整性。截断与故障必须引发相应UNKNOWN或保留已证违规，不得静默缩小分母。

## 7. 回归门禁与 issue 关闭

实施后从仓库根目录执行；新增测试文件在对应任务中创建。先目标测试，再兼容回归，最后完整门禁：

```bash
python -m pytest -q tests/test_expected_pass_rate.py
python -m pytest -q tests/test_issue_80_evaluator_versioning.py tests/test_issue_81_frozen_execution_identity.py tests/test_issue_82_typed_results.py tests/test_issue_83_quality_policy.py tests/test_issue_84_eval_only_retry.py tests/test_issue_85_snapshot_revision.py tests/test_issue_86_comparability.py tests/test_issue_87_langfuse_projection.py
python scripts/export_openapi.py
pnpm --dir services/console api:generate
pnpm --dir services/console lint:tokens
make console-test
ARGUS_E2E_API_PORT=28080 ARGUS_E2E_CONSOLE_PORT=28083 make console-e2e
make validate
```

API快照与生成类型必须无drift。真实本地integration单独执行并记录；现有 `make validate` 不自动证明该新增场景。CI按仓库当前五项要求执行；受条件控制未创建的job明确说明，不能写成已通过。只读文档方案本身豁免行为RED，但必须做文档、链接、契约一致性和diff范围验证。

| issue | 关闭条件 |
|---|---|
| #127 | S40全部实测矩阵/关联/隔离证据；不能仅有PR133 Mock |
| #128 | S41全部+T1/T2，Profile已验证、Artifact可恢复、无质量漂移 |
| #129 | S42全部+T3/T4/T5，API与Console闭环、三态和optional资格完整链 |
| #130 | S43全部+T6，安全参数与剩余规则、脱敏/比较/投影验收 |
| #131 | S44全部+T8，冻结/校准/覆盖/critical准入和模型错误验收 |
| #132 | S45全部，确定性A与Judge B都完成，通用Gate回归未破坏 |
| #5 | 六期全部关闭条件+M5全部真实跨框架Demo与门禁证据 |

文档PR只用关联引用；实现PR可以在其全部验收完成时使用对应关闭引用。若某期未达门槛，保留issue开放并交付明确阻塞报告。合并文档、CI绿色或任务勾选都不单独构成产品完成证据。
