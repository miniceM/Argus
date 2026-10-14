# S4.3：轨迹参数规则、重复调用与脱敏

状态：**T6 本地实施与阶段门禁通过，既有证据保留在 PR #181；不代表完整 S4 验收。** 参数治理、可信因果子序列、重复调用、签名 HUMAN、真实来源消费者和安全出口的既有实测与独立复核按原版本解释。后续生产数据与发布验收交管理员，见[交接说明](../../validation/s4-final/administrator-validation-handoff.md)，不等待 GitHub Actions 才交付材料。

最新证据：[T6 逐项验收记录](../../validation/s4-3/t6-acceptance.md)、[最终稳定回执](../../validation/s4-3/final-stable-receipt.json)。两种来源共 60 份治理校准及四类扩展规则的正式 Worker/Snapshot/API/重评通过；完整真实门禁 86 passed / 0 skipped，隔离 PostgreSQL 下 `make validate` 为 2186 passed / 0 skipped，Console 单测 858 passed、E2E 128 passed。下文保留各次历史结果，最新回执以稳定源码 SHA-256 为准；发布 CI 尚待核验。

关联：[父 Issue #5](https://github.com/miniceM/Argus/issues/5)、[Issue #130](https://github.com/miniceM/Argus/issues/130)。共同契约见 [01-technical-design.md](01-technical-design.md) §3、§5–§8。

核验基线：2026-10-13，`main@4b043739c63357ecc10131e1ba54bc1bc53d862a`。

## 1. 目标与 Issue 修订

本期在 S4.1 的受控轨迹采集和 S4.2 的 Typed Evaluator Adapter 上扩展 `tool_argument_match`、`max_same_tool_repeats`、`required_step_sequence`、`require_human_escalation`。

参数证据按“可见且符合、可见且不符合、无法可靠观察”三种情况决策：明确不符合可 FAIL；参数未采集、被脱敏或无法解析必须 UNKNOWN。

Issue #130 当前表述把脱敏版本放在 S4.3 才开始；修订为 **S4.1 已在轨迹进入冻结制品、API、日志和投影前实施最小脱敏，S4.3 扩展参数规则和策略覆盖**。不能把完整安全边界推迟到本期。

Issue 当前要求“脱敏版本变化导致 `tool_mapping_digest` 变化”；修订为 Mapping 与 Redaction 两个独立身份：Redaction 变化不改 Mapping digest，但改变 `redaction_policy_digest` 和综合 Measurement identity。

比较的是冻结的测量配置，不要求 Baseline 与 Candidate 每个 Case 的 `trajectory_digest` 相同；每次轨迹本来就可能不同。

只改 QualityPolicy 阈值、required 或 critical 设定时，Measurement digest 保持不变，QualityPolicy digest 改变；改变参数提取器、JSONPath、红线配置或脱敏策略时，Measurement digest 改变。

原 Issue 的 Langfuse 投影要求继续成立：boolean 可投影为 1/0；有序 category 仅用冻结映射；text 和无序 category 为 `NOT_APPLICABLE` 并带原因码，不转成数字。

## 2. 当前代码边界与新增安全责任

本基线尚无通用 `StandardTrajectory`；实施依赖 01 定义的 `arguments_redacted`、`argument_visibility`、冻结制品及受控 Reader。

现有 [`EvaluatorExecutor`](https://github.com/miniceM/Argus/blob/4b043739c63357ecc10131e1ba54bc1bc53d862a/services/eval-runner/app/evaluator_binding.py#L507) 只收 `output/expected_output`；本期复用 01 的 `EvaluationContext` 和冻结 Binding Adapter。

现有 [`_result_evidence()`](https://github.com/miniceM/Argus/blob/4b043739c63357ecc10131e1ba54bc1bc53d862a/services/eval-runner/app/evaluator_binding.py) 会复制 Evaluator metadata；轨迹规则必须显式构造仅含 reason code、step id 和可审计摘要的 evidence，禁止把原始参数放进 metadata。

现有 [`langfuse_projection.py`](https://github.com/miniceM/Argus/blob/4b043739c63357ecc10131e1ba54bc1bc53d862a/services/eval-runner/app/langfuse_projection.py#L65) 的投影 evidence 会保留 typed result `original_value`；所以参数规则只输出 boolean 或无敏感 count，不可把原始参数当 Evaluator value。

Snapshot 通过 [`result_snapshots.py`](https://github.com/miniceM/Argus/blob/4b043739c63357ecc10131e1ba54bc1bc53d862a/services/eval-runner/app/result_snapshots.py) 冻结 Case 结果；`worker.py` 的既有 Outbox 和日志路径也必须按最小数据原则验证，不能只测 UI 上显示了掩码。

## 3. `tool_argument_match` 的受控输入

Evaluator Binding 只冻结工具身份、phase、参数路径、期望值、匹配操作符和规则算法版本；`QualityPolicy` 冻结 required/optional、critical 及决策边界。

拟用现有通用接口，不增新结果类型：

```python
def evaluate_tool_argument_match(
    context: EvaluationContext,
    params: ToolArgumentMatchParams,
    *,
    binding: EvaluatorBinding,
    manifest_schema_version: str,
) -> TypedEvaluationResult: ...
```

返回 boolean TypedEvaluationResult：全部适用调用符合为 `true`，观察到任一确定不符合为 `false`；随后由现有 QualityPolicy 给出 PASS/FAIL。

不可见、redacted、invalid 或能力未覆盖走 `no_result`，不能返回 false；所以它不会把“没数据”误当成“参数违规”。

拟支持的 JSONPath v1 语法仅为 `$` 根、`.identifier` 对象成员和 `[unsigned_integer]` 数组下标，可串接，例如 `$.transfer.amount`、`$.items[0].currency`。

`identifier` 限定 `[A-Za-z_][A-Za-z0-9_]*`；键名大小写敏感，路径解析不访问原型属性、对象方法或运行时代码。

拒绝 `*`、`..`、过滤器、递归、切片、函数、正则、脚本表达式、带引号动态键和任何未列入语法的字符；不得调用 `eval/exec` 或引入可执行表达式引擎。

路径必须在 Binding 冻结时由 parser 编译成 token tuple 并校验版本；运行时只按 token 逐层读取已校验 JSON。

操作符固定为 `eq`、`ne`、`one_of`、`gt`、`gte`、`lt`、`lte`、`contains`。`one_of` 只接受同类型 JSON scalar 集合；排序操作只支持数字；`contains` 只支持 string 子串或同类型数组元素。

不支持 regex、任意对象谓词、路径通配、类型转换链和用户定义函数；超出集合的规则在冻结阶段返回稳定 `TRAJECTORY_RULE_OPERATOR_UNSUPPORTED`。

JSON 类型不做隐式 coercion：boolean 与 number 分开；`"5"` 不转 `5`；`"true"` 不转 `true`；null 不转缺失。

整数和小数按十进制值精确比较；JSON解码保留Decimal语义、禁止先转float再恢复。canonical encoder把Decimal写为规范JSON number：无多余零、负零归0，不写成字符串，数字最多64位。Artifact用canonical JSON TEXT精确往返，API采用局部encoder，Console使用服务端step_argument_display_json字符串显示，禁止浏览器重算规则；验收包含大于2^53的整数和高精度小数。路径最多32段、字符串最多4096 bytes、JSON深度最多32，均随01的contract冻结；超限显式降级。NaN/Infinity不进入轨迹。

`eq/ne/one_of` 要求操作数类型一致；类型不同是**已观测值不符合**，产生 false。结构损坏、值解析失败或超出冻结类型才是 `no_result`。

Sensitive path 不允许作为参数匹配字段。规则冻结时必须通过该 `source_profile + redaction_policy` 的 allowlist 与 sensitivity 校验；敏感目标返回 `TRAJECTORY_SENSITIVE_MATCH_FIELD`。

敏感 expected literal 也禁止写进 Evaluator params / Manifest；不以 SHA-256 或低熵裸 hash 绕过这一限制。

## 4. 可见性与 FAIL / UNKNOWN 真值表

01 定义的 `argument_visibility` 语义是实现前置：`AVAILABLE` 表示该路径有原值；`MISSING` 表示完整参数对象已采集且路径确实不存在；`REDACTED` 表示值已被策略隐藏；`INVALID` 表示来源内容无法可靠解析。

如果整个参数对象未采集，降低 `tool_arguments` capability coverage；不得把它表示成已证实的 MISSING。

| 观察情况 | Typed result | 质量含义 |
|---|---|---|
| 完整对象中路径存在且满足预期 | succeeded / `true` | PASS 候选 |
| 完整对象中路径存在但值或类型不符合冻结条件 | succeeded / `false` | 明确违规，FAIL |
| 完整对象可见、路径明确缺失，而规则要求该字段 | succeeded / `false` | 明确缺字段，FAIL |
| 整段参数未采集、路径 REDACTED 或 profile 不支持 | `no_result` | UNKNOWN |
| Observation 结构非法、身份或 provenance 不匹配 | `no_result` 或 Binding failed | UNKNOWN / 运行诊断 |

多次匹配调用的聚合为 FAIL 优先：发现任何一笔确定违规即返回 `false`，即便另一笔的参数不可见；没有违规但存在未知调用则 UNKNOWN；全部调用可见且合规则 true。

默认对 `REQUESTED` 阶段验证实际发送意图；Binding 可冻结其它单一 phase，但同一规则不跨 phase 混合值。

调用缺失时：若冻结规则 `require_call=true` 且请求覆盖完整，输出 false；覆盖不完整则 no_result。该字段避免“没调用”被误读成参数正确。

## 5. 其余三条规则

### `max_same_tool_repeats`

规则统计同一 causal chain 上连续、重复的逻辑调用，冻结 max_repeats 业务约束，输出 boolean：可信连续重复已超限为 false，覆盖充分且未超限为 true，否则 no_result。安全 observed_repeat_count 仅作 evidence；max_repeats 变化属于 Measurement，QualityPolicy 对 boolean 的 operator/expected_value 另行冻结，不将该证据冒充 numeric result。

调用 identity 为冻结 `tool_identity`、冻结 phase（默认 `EXECUTED`）、`workflow_scope_id` 与获准 identity paths 的规范化值；不含 raw span 名，不跨 Launch/Case/Attempt 拼接。相同工具、相同获准参数且有可信连续因果边才算重复；金额或其他获准身份值不同的调用不合并。

参数身份路径必须已被 Redaction allowlist 批准且可见；任何 identity path 被隐藏时，涉及的重复计数为 UNKNOWN，不能把不确定调用拆成不同身份来低报次数。

一个真实调用只计一次：按选定 phase 取 distinct Observation Step；请求和执行事件不同时计数，重复 Observation 先由 Assembler 去重。UI/API evidence 只保存命中的 Step IDs 和 count，不保存参数值或参数 hash。

只计算同一 workflow 上有可信直接相邻关系的同身份调用；若序号有缺口或可能遗漏中间的不同调用，不能推定连续。并行调用不伪装为连续重复。可能改变上限结果的顺序/参数关系无法证明时返回 no_result；已独立证明连续重复超限仍保留 false。

### `required_step_sequence`

序列定义冻结为 Step descriptor 列表（`type/tool_identity/phase`），不允许用户提供任意表达式。

从 `causal_predecessor_ids` 构成的偏序图中寻找同一 `workflow_scope_id` 下的有序子序列；仅已验证因果边可满足先后条件。

完整覆盖下无匹配序列返回 succeeded/false；存在一条完整可信序列即可返回 succeeded/true；缺少能力、部分采集或候选顺序并行不明且结果可能变化时返回 no_result。

### `require_human_escalation`

只有来源 profile 已验证、在冻结映射中显式映射成 `StepType=HUMAN` 的事件才算人工证据。

普通 TOOL span、名为 `escalate` 的工具、LLM 文本中的“已转人工”都不构成人工事件。

事件必须属于相同冻结 workflow scope 且有受信 source event id；只保留事件类型、结果码、时间/Step 引用，不保存员工姓名、账号、对话、备注或自由文本。

观察到所需 HUMAN 事件为 true；human_events 覆盖完整却没有事件为 false；集成未覆盖、人类事件来源不明或映射冲突为 no_result。

## 6. 脱敏管线与数据落点

最小策略在 #128 阶段启用：原始 Observation 仅存在于有界的本地采集缓冲区；完成 scope/schema 校验后立即过滤并构建脱敏对象，再参与 digest、Snapshot 和 API 序列化。

参数 v1 使用 source profile 的显式字段 allowlist；未列入允许集合的字段不写入 `arguments_redacted`。禁止把完整原始 `args` 复制到 `result_summary`、metadata 或 exception。

敏感 key 名比较大小写不敏感并递归剔除，包括 `authorization`、`cookie`、`set-cookie`、`token`、`secret`、密码、卡号和身份号等字段族；匹配策略本身版本化。

常见卡号须有格式与 Luhn 测试；身份号策略按明确支持的格式和 profile 声明测试。任意自由文本不作为参数白名单来源；无法证明安全的字段一律 REDACTED。

字段值采用 allowlist 后仍执行冻结的内容检查和最大长度截断；命中敏感模式时整值剔除，不只遮住中间字符后继续比较。规则所需值被截断后标 REDACTED/INVALID 并返回 UNKNOWN，不对截断字符串继续作等值真判。此边界同样覆盖 raw_name、workflow_scope_id、input/output 和 result_summary；未获准自由文本不能借这些字段出站。

规范化轨迹只携带获准且脱敏的 `arguments_redacted`；不得保存原始 Observation JSON、HTTP Header、cookie、Token、完整银行卡、身份号码或人工文本。

Typed result `evidence` 只允许 `reason_code`、`evidence_step_ids`、路径标识、匹配阶段和安全摘要；不得放 raw argument、expected literal 或可逆编码。

Snapshot 的轨迹制品和 API 响应必须仅包含该脱敏 StandardTrajectory；历史快照不回源补取线上原始参数。

日志仅记录 Launch/Item/Attempt identity、稳定 reason code、策略版本和 digest；异常文案不得拼入上游参数或整个 SDK 返回对象。

Langfuse Outbox 继续走既有 projector：boolean 结果写 1/0，`original_value` 仅为 true/false；重复次数只进入受控 evidence；text/无序 category 写 `NOT_APPLICABLE` 与固定原因码。

Score 的 trace/root 关联沿用既有身份字段；Outbox payload 不含参数值、参数 JSON、路径值或 redaction 前的 evidence。

## 7. Digest 与可比较性

| Digest | 内容 | 变化影响 |
|---|---|---|
| `tool_mapping_digest` | Provider 原名到规范 `tool_identity` 的映射 | 仅工具语义映射变化时改变 |
| `redaction_policy_digest` | allowlist、敏感字段族、转换/截断和策略版本 | 脱敏策略变化时改变 |
| `trajectory_contract_digest` | Schema、Assembler、profile、Mapping、Redaction、完整性协议和限额 | 任一测量口径变化时改变 |
| `measurement_digest` | Evaluator 实现/版本/输入契约/params 与 trajectory contract | 规则算法、路径、allowlist或红线变化时改变 |
| `quality_policy_digest` | required、optional、critical、operator、阈值和 UNKNOWN 处置 | 只改门槛时改变 |
| `trajectory_digest` | 单个 Case/Attempt 的脱敏规范化轨迹 | 每个具体输入单独计算，不要求两侧相等 |

`redaction_policy_digest` 与 `tool_mapping_digest` 不互相派生；只改脱敏不能冒充工具映射变化。

Baseline/Candidate 可有不同轨迹内容；Formal Comparison 核对相同冻结 Measurement、QualityPolicy 与 Aggregation/Comparison 配置，并验证各自 evidence 完整，不比较 `trajectory_digest` 相等。

若只收紧 QualityPolicy threshold，Measurement 仍相同，正式比较按既有分层契约报告策略口径变化并暂缓质量回归判断；不得将阈值变化标成 Agent regression。

## 8. 拟改/新增文件

以下清单表示后续实现建议；本方案 PR 不写产品实现。

现有模块拟改：`services/eval-runner/app/trajectory/schema.py`（若 #128 已建则增量扩展）、`redaction.py`、`rules.py`、`evidence.py`、`evaluator_binding.py`、`langfuse_projection.py`。

Manifest/Identity 接入拟改：`services/eval-runner/app/manifest.py`、`comparison_contracts.py`；策略阈值沿用 `quality_policy.py`，不另建政策层。

持久化与出口拟核对/修改：`services/eval-runner/app/result_snapshots.py`、`api_results.py` 或 01 约定的 trajectory API 模块、Outbox projector/sync、`worker.py` 的结构化日志边界。

拟新增测试：`tests/test_trajectory_arguments.py`、`tests/test_trajectory_redaction.py`；复用 #128 的 `tests/trajectory_fixtures.py`。

拟扩展集成测试：`tests/test_trajectory_required_evidence.py`、`tests/test_trajectory_api.py`；它们应使用同一 fixture 断言从 typed result 到 Snapshot/API/Outbox 的完整数据最小化。

所有 Python 文件均为后续提议；如果 #128/#129 已新增同名模块，只扩展已有职责，不复制第二套 parser、redactor、evaluator 或 result model。

## 9. RED → GREEN 示例与命令

测试应先以带唯一敏感哨兵的 fixture 证明泄漏路径，再实现 allowlist/Redaction；哨兵仅为合成数据，不使用真实 PII。

示例：请求 `{"currency":"USD","card_number":"4111111111111111"}`；只允许冻结的 `currency`，Snapshot/API/日志/Outbox 不得出现 `card_number` key 或卡号哨兵。

示例：完整对象缺少 `$.currency` 得到明确 false；整段参数未采集或 `$.currency` 为 REDACTED 得到 no_result；`{"currency":5}` 对预期字符串 `"5"` 不发生 coercion，按观察到的类型不符返回 false。

`RED`：`pytest -q tests/test_trajectory_arguments.py tests/test_trajectory_redaction.py`；新增测试先断言上述三态、路径拒绝和所有出口不含哨兵，预期现实现不满足。

`RED`：`pytest -q tests/test_trajectory_required_evidence.py tests/test_trajectory_api.py`；预期先失败于冻结脱敏制品与 Snapshot/API 边界。

`GREEN`：重复运行以上 Python 测试；实现后再运行 `pytest -q tests/test_issue_82_typed_results.py tests/test_issue_87_langfuse_projection.py` 验证 Typed result 和投影兼容。

`GREEN`：执行 `pytest -q tests/test_trajectory_arguments.py tests/test_trajectory_redaction.py tests/test_trajectory_required_evidence.py tests/test_trajectory_api.py`。

最后执行 `make validate`。以上是预期实施命令；本方案文档撰写未运行这些新测试，也不声称通过。

## 10. 逐项验收

- [ ] **S43-01**：JSONPath parser 只接受列明语法；任意 eval、wildcard/filter/recursive path 被拒绝，拒绝码稳定。
- [ ] **S43-02**：JSON 类型不隐式转换；boolean/number 分离，小数精确；明显不匹配 FAIL，REDACTED/unavailable/invalid UNKNOWN。
- [ ] **S43-03**：完整参数对象缺少 required path 为明确违规；未采集、REDACTED 与解析错误不得变成 FAIL。
- [ ] **S43-04**：重复次数按 tool identity、冻结 phase、scope 与获准参数身份计算；并行或 identity 不足时 UNKNOWN；原始参数不进入 evidence。
- [ ] **S43-05**：Step sequence 使用可信因果边；已验证来源且同 scope 的 HUMAN 事件为 true，完整覆盖下无事件为 false，来源未验证/缺 scope 为 no_result；工具名或文本自报不能证明人工事件，正例必须有 #127 来源矩阵。
- [ ] **S43-06**：只变 Redaction 时 tool_mapping_digest 不变，redaction_policy_digest、trajectory_contract_digest、Measurement 均改变；只改 QualityPolicy threshold 时仅该 Policy digest 改变，Measurement/轨迹合同保持。
- [ ] **S43-07**：Baseline/Candidate 的轨迹内容不同不自动判不可比；比较身份只核对冻结配置和既有完整性合同。
- [ ] **S43-08**：Snapshot、API、结构化日志和 Outbox 全链路都不含敏感哨兵、raw args、Authorization/Cookie、卡号和受支持身份号字段。
- [ ] **S43-09**：boolean 投影仅保留布尔 `original_value`；text/无序 category 为 `NOT_APPLICABLE`；legacy 数值映射无漂移。
- [ ] **S43-10**：旧 Manifest/Snapshot 继续旧解析路径；新 Manifest `1.3` 与 `evidence_contract_version="trajectory-1.0"` 显式解析，不做字符串大小比较。
- [ ] **S43-11**：本期 API/OpenAPI/Console schema 同步（若本期改 API），单元/集成测试和 `make validate` 通过，既有 Demo 基线保持原值。

## 11. 依赖、回退与风险

依赖 #127 的真实 Observation 字段、参数可见性和延迟/profile 证据；依赖 #128 在写冻结制品前接入最小 allowlist/脱敏、capability coverage、source provenance 与 digest。

依赖 #129 已交付的 Typed Evaluator Adapter、冻结 required 集合、Step 引用、`no_result/skipped` 语义和 Snapshot schema discriminator。

如果敏感字段不能可靠分类，安全默认是拒绝该 JSONPath 并返回 UNKNOWN；不能通过 `hash(value)` 绕过，尤其不对低熵秘密生成可猜测摘要。

如果一条规则需要比对账户、卡号或身份号等敏感值，S4.3 v1 不支持该字段；需单独设计密钥管理与受控比较契约，不能把明文期望值塞进 Evaluator Manifest。

新规则以 Manifest `1.3` 的 opt-in Binding 开启；移除新 Binding 停止后续 Launch 采集。已有 Snapshot 不回写、不重算、不在线补值。

发现策略缺陷后，冻结新的 redaction/evaluator version 并创建新 revision；已有制品只含通过旧策略获准的数据。若审计发现历史制品泄露，暂停新轨迹 Launch 并按仓库安全流程评估受影响制品，不能静默覆盖证据。

保留风险：通用 PII 检测不能证明任意自由文本安全；因此字段 allowlist 是主要边界，内容检测只是纵深措施，不把它描述成完整 PII 识别器。

## 12. T6 实施记录（2026-10-14，阶段未验收）

已实施的参数规则使用 `expected_json` 字符串冻结精确 JSON literal，再由 canonical codec 解析为 JSON scalar 或 `one_of` 同类型数组。该字段保留十进制精度，避免 Manifest 普通 JSON 编码先经过 float；运行时仍按 JSON 类型比较，不将字符串与数值互转。新增算法身份为 `tool-arguments-1.0`，既有四规则继续使用 `tool-rules-1.0`。

所有新建轨迹 Launch（包括 capture-only）必须固定 `pii-v1` 内容治理策略，不能因只执行非参数规则而跳过。相同幂等请求读取既有记录及历史冻结合同加载不回写原策略；新建入口在外部 Dataset 解析和持久化前拒绝缺策略配置。参数 Binding 还必须通过冻结 Redaction contract：`content_policy_version="pii-v1"`、获准 argument path，以及需要时的静态 `string_policies`。当前字符串语义仅支持 `identifier` 和三个大写字母的 `currency` 格式，不宣称后者验证了完整币种目录；未声明语义的字符串不能作为安全参数来源。卡号、受支持身份号、格式化 SSN、凭据模式及敏感字段族命中时隐藏整值；敏感字段（包括 `social_security_number` 及 camelCase 变体）不得进入获准路径。字符串内容分类 `identifier` 允许 `[A-Za-z0-9_][A-Za-z0-9_.:-]{0,255}`，与第 3 节的 JSONPath 成员键语法是不同合同；内容检查仍优先于字符串格式，不能用安全别名绕过脱敏。旧合同省略新增字段，维持原有序列化与 digest；新参数规则不得绕过治理合同。

`required_step_sequence` 使用最多 32 个冻结 descriptor（`type`、`tool_identity`、`phase`），算法身份为 `step-sequence-1.0`。它复用有界拓扑 bitset 因果索引，允许因果链中存在其他事件；可信严格链上的同一 Step 不可满足两次 occurrence。存在完整可信子序列即可通过，完整覆盖且候选不可能满足时为 false；并行、归属不明、缺时钟或不完整覆盖保留 `no_result`。证据仅含有限 reason、Step IDs 和冻结引用。

验证范围：参数治理与子序列、旧四规则、冻结执行/Manifest、Normalizer、typed results 和 legacy projection 的定向回归为 **434 passed / 1 PostgreSQL skipped**，Ruff 通过。子序列核心先确认有效 RED **15 failed / 6 passed**，实现后相应测试通过。新增冻结执行测试只使用合成来源，明确断言它不能形成 required `COMPLETE` 资格，不作为真实 Reader 准入证据。

`max_same_tool_repeats` 固定 `tool-repeats-1.0`，以同一工具、phase、workflow 和 1–32 个获准 `identity_paths` 的规范值识别逻辑调用。`max_repeats` 表示连续调用总数，包含首个调用；值为 0 时禁止观察到任何具有可见身份的该调用。路径在冻结时排序、去重校验和静态编译，敏感路径及未批准 allowlist 被拒绝。身份规范值只在本次有界测量内比较，不持久化原值或参数 hash。

重复规则复核冻结终止清单，验证每个中间事件的连续序号和可信直接因果边，再在选定 phase 的逻辑调用上统计最长连续段。请求与执行不双计，已知不同工具或不同身份打断连续段；仅有序号不能证明连续，缺口、并行、缺时钟或隐藏身份保持 UNKNOWN。清单局部完整且已经证明超限时，其他缺失或不可见调用不能抹去 false；不完整清单不能产生全量合规结论。证据只保存 `observed_repeat_count`、命中 Step IDs、有限 reason 与冻结引用。

重复核心有效 RED 为 **20 failed / 6 passed**；随后完整度/叶值类型边界实际 RED **2 failed / 26 passed**，FAIL 优先边界实际 RED **1 failed / 28 passed**，均修复后通过。包含重复核心的最新定向回归为 **463 passed / 1 PostgreSQL skipped**，新增冻结 dispatcher 测试依旧不计作正式来源资格。

上述记录不勾选 §10 整期验收。仍需完成 HUMAN 签名正例来源矩阵与 Reader 校准，验证同一冻结制品在 Snapshot/API/日志/Outbox 的数据最小化，补齐隔离 PostgreSQL、真实来源集成，再按实施计划完成整期独立审阅和验收。

### HUMAN authority 验证细化

T0 的 [真实来源矩阵](../../validation/s4-0/README.md) 已记录原生 OTel 与 Pydantic AI 的 `approved` 正例，各有一个由隔离审批进程产生的 HUMAN 事件；零事件场景有审批系统关闭 scope 的证明。这些记录只证明夹具来源可以审核，不证明实际人员审批。`workflow.source`、`service.name` 和工具名仍是可由 Agent 构造的属性，单独匹配它们不能完成可信 authority 准入。

为兑现 §5 的可信来源要求，新增 `human-authority-1.0` 签名回执协议：冻结 authority ID、算法 `Ed25519` 与 32-byte 公钥；64-byte 签名与公钥都使用严格 canonical Base64。authority 私钥不进入 Agent、Manifest、日志或回执。使用仓库已有 `cryptography>=46,<47` 的 [官方 Ed25519 API](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ed25519/)，不新增依赖或实现密码算法；2026-10-14 本地 `cryptography 46.0.7` 的 sign/verify 已实测可用。

签名消息为固定域 `Argus/HumanAuthority/1.0` 后跟 NUL，再拼接 canonical JSON body。body 仅允许协议版本、authority ID、kind、Trace ID、workflow scope、Observation ID、Runner Root ID，以及事件 ID/结果码或关闭后的事件 ID 清单。`ESCALATION` 包含一个事件 ID 与 `APPROVED/DECLINED`，`INVENTORY` 包含最多 1000 个唯一事件 ID 和严格 `scope_closed=true`；两个 kind 的字段不可混用。不携带员工身份、账号、备注、对话或任意 metadata。Trace/workflow/event ID 必须为非零 32 位十六进制，当前实测 Observation/Root ID 为非零 16 位十六进制；协议只支持该已核验格式。

消费方必须按冻结公钥验签，并与当前 Trace、workflow、Observation 和 Runner Root 核对。回执不能提供替换公钥；替换 key、改结果码/事件 ID、跨 Root/Trace/workflow 重放、额外敏感字段与超容量输入都返回有限错误码，不输出上游原文。签名只证明获准 authority 发布了该事实；生产准入还必须审核 authority 自身的人员授权与事件语义，不能把自动化合成审批夹具当成真实人员介入证据。

当前工作树已接入 SourceProfile 冻结公钥、Normalizer 验签、Assembler 完整度、`require_human_escalation` 和含 HUMAN 的 sequence。现有 `type_map` 禁止直接映为 HUMAN 的约束保持有效；HUMAN 只能通过获准签名回执产生。冻结 Evaluator 执行从所选来源 profile 读取公钥，不回退当前配置。已证明的事件即使缺 inventory 仍可证明存在；完整的零事件结论必须同时满足基础采集完整度与同 Root/workflow 的唯一签名关闭清单一致性。损坏、缺失或冲突清单保持 UNKNOWN。

独立审批 fixture 已实现该签名协议，私钥仅保存在审批进程内存，readiness 只公开公钥。业务 Agent 不导入签名器或评测 SDK。fixture 的独立依赖锁追加 `cryptography==46.0.7` 及其依赖，原有 30 个包版本保留；Runner 继续使用已有密码依赖。签发端全零事件 ID 的回归测试经历 **1 failed / 4 passed → 5 passed**；签发端、回执和 HUMAN 消费链路定向回归共 **61 passed**。这些证据不替代真实 Langfuse 来源验证、Reader 准入或整期验收。

真实采集消费验证进一步复现了制品存储缺口：原 validator 未识别签名回执，拒绝合法来源为 `TRAJECTORY_EVIDENCE_REDACTION_INVALID`。正、零事件的存储回归初始 **2 failed**。存储层现按冻结公钥重验协议字段、签名与 scope，不将任意 metadata 加入业务投影；签名关闭清单从业务事件库存中排除，`human_events=COMPLETE` 还需重验各 workflow 的关闭清单覆盖。签名、额外字段、名称、因果边与 scope 篡改均拒绝。签发、HUMAN 与制品回归为 **71 passed / 1 PostgreSQL skipped**。

本地完整门禁（2026-10-14）为 **2015 Python passed / 127 PostgreSQL skipped / 84 real deselected**，`make validate` 通过；这不能证明 PostgreSQL 或真实来源整期验收。审批 HTTP 父节点须按精确 SPAN 名称与已核验 business instrumentation 来源冻结为结构节点，不能按自由名称或时间推断；真实诊断链路使用分页 Reader、Acquisition quiet 证明及不可变制品，正式 Reader 准入仍需独立校准。

签名真实消费定向验证（2026-10-14）为 **4 passed / 0 skipped / 61 deselected**：原生 OTel 与 Pydantic AI 的已签事件为 true，原生 OTel 的 `serial` 和工具 `escalate` 场景经已签空关闭清单为 false；后者不能冒充 HUMAN。验证真实 Langfuse 回执的 Trace/workflow/Observation/Root 绑定、跨 Root 重放拒绝，以及分页 Acquisition → 保存 → 重新加载 → 冻结规则执行。具体命令及工作树内容摘要见 [签名来源消费记录](../../validation/s4-3/human-signed-source.md)。该记录不代表正式 Reader 校准、真实人员审批或整期验收。

回执基础的初始 RED 为 **35 failed**（验证模块尚不存在）；完成后首轮 35 项通过。Root 绑定扩展的 RED 为协议/API 尚不支持新增 Root 字段，不能将它描述为已经复现了实际运行态重放。关闭标记的严格类型边界另有有效 RED **1 failed / 36 passed**：将合法已签回执的 `true` 改为整数 `1` 后，原模型发生隐式转换而仍接受；已修复为只允许严格 boolean true。最新回执与既有 T6 定向回归为 **500 passed / 1 PostgreSQL skipped**，其中回执 37 项。所有签名密钥只在测试内存生成，没有写入私钥文件；这些测试不作为 Reader 准入或 HUMAN 真实验收。


整期独立审阅发现并修复的敏感字段与格式化 SSN 边界，见 [修复记录](../../validation/s4-3/privacy-review-remediation.md)。记录中的定向回归不替代整期真实消费者及最终门禁验收。
