# S4.1 Acquisition、StandardTrajectory 与只读 Assembler：#128

状态：提议，待方案 PR 评审；本文件是实现蓝图，不表示功能已经交付。核验基线：2026-10-13，main@4b043739c63357ecc10131e1ba54bc1bc53d862a。字段、枚举、函数签名和 digest 语义统一服从 [01 技术契约](01-technical-design.md)；不得在本文件另起接口或枚举。

## 目标、现状与 Issue 修订

S4.1 接收 S4.0 的真实 Langfuse 字段/延迟矩阵，完成按 Attempt 限界的只读 Acquisition、v1 Normalizer/Assembler、最小脱敏证据制品和恢复校验。它产出可重复读取的 StandardTrajectory 与 TrajectoryEvidenceRef，但不运行规则、不计算质量结论、不改 Release Gate。

截至核验日，#128 为 Open，无 GitHub parent/sub-issue 关系和开发分支。原 issue 把完整性写为 FULL / PARTIAL / DEGRADED_ONLY_ROOT，把去重只按 observation_id，把排序固定在 started_at，并把同一 Trace 多次组装产出同一 digest 当作验收。这些表述需要依赖 S4.0 的真实字段矩阵修订，并以 01 中的 AcquisitionState、Completeness、CapabilityCoverage、scope 闭包和 canonical digest 契约为准。

建议将 #128 标题改为“[S4.1] 有界 Trace Acquisition、StandardTrajectory v1、冻结证据与只读 Assembler”，并更新 issue：

- 明确 #127 为前置；S4.0 未出具本地真实链路矩阵前，不冻结 source profile 和时间限额。
- 将 acquisition.py 纳入交付范围；只写 assembler 而没有采集状态、分页、迟到重查，会把 Root-only 首次查询误判为内部 span 不存在。
- 完整性采用 01 的双轴：采集 state 与证据 completeness/capabilities 分离；删除 FULL / DEGRADED_ONLY_ROOT 旧拼法。
- digest 验收改成“相同冻结规范化输入和相同 contract 稳定；输入变化使 trajectory_digest 变化”。在线 Trace 可因迟到 Span 改变，不能以 Trace ID 不变推定输入相同。
- 轨迹 scope 必须由 Runner 冻结执行记录提供并包含 01 中的 Launch / Item / DatasetItem / Attempt / generation / Trace / root 身份；禁止按时间、name 或浏览器传入 ID 猜关联。
- 只读 Assembler 负责报告结构和完整度，不做 PASS/FAIL/UNKNOWN、规则、Judge、Console、Release 或 Langfuse Score。

本方案记录 #128 属于 #5 范围、依赖 #127；维护者后续可设置原生父子/依赖关系，文档 PR 的 Refs 不等于关系已修改。#129 可复用本阶段制品；规则输入 Adapter 属后续冻结执行工作，不在 #128 偷加规则入口。

## 模块责任与数据边界

实现目录固定为 services/eval-runner/app/trajectory/，全部类型和公共签名以 01 第 2–6 节为准：

| 文件/入口 | 责任与禁止事项 |
|---|---|
| trajectory/schema.py | 定义并校验 01 的现有类型、版本与必要约束；包含可选 causal_predecessor_ids/workflow_scope_id；不创建并行 DTO |
| trajectory/acquisition.py | 唯一 Langfuse v2 只读分页、限时重查与状态收敛入口；不调用 Agent、不判规则 |
| trajectory/normalizer.py | 按 S4.0 已验证的 source profile 将 API 字段映射到 StandardTrajectory 使用的通用字段 |
| trajectory/assembler.py | 从 root 闭包建图、去重、识别异常、产生稳定展示序列和 completeness |
| trajectory/redaction.py | 仅保留冻结规则允许的脱敏 input/output/arguments/result；raw Observation 不出 Adapter |
| trajectory/evidence.py | 对冻结 StandardTrajectory canonicalize、计算 digest、插入或读取不可变制品 |
| app/manifest.py 与 evaluator binding freeze path | 提供 trajectory Manifest 1.3 序列化与合同冻结；旧 Manifest 不迁移；正式 Binding 接入在 #129 |
| app/result_snapshots.py | 提供 EvidenceRef 序列化/校验；本期不写资格 discriminator，T4 与 required_evidence_status 原子启用 |
| app/evaluation_recovery.py / app/execution_checkpoint.py | 复用冻结 acquisition/artifact，不重新调用 Agent，不用在线最新 Trace 替换旧证据 |
| tests/trajectory_fixtures.py | #128 提供 schema/reader/observation 工厂；后续单测共用此处，不手写重复字典 |
| tests/test_trajectory_schema.py | 字段、枚举、空值、版本拒绝与序列化契约 |
| tests/test_trajectory_acquisition.py | 分页、延迟、截止、错误分类、字节/并发预算与恢复 |
| tests/test_trajectory_assembler.py | scope 闭包、重复/冲突、父链图、并行、时钟不确定和截断 |
| tests/test_trajectory_evidence.py | digest 稳定性、insert-only、唯一身份和 artifact 校验 |
| migrations/017_trajectory_evidence.sql | 追加制品表及 checkpoint Acquisition 扩展；实施时复核迁移号 |

生产数据只来自 Runner 的冻结 Attempt 与 Langfuse 只读 API。Agent 返回的 output.tool_calls 不进入 observed trajectory 证据；Agent 自埋 OTel 也不是不可篡改业务流水。Langfuse 保持 Trace/Observation 的权威来源，Argus 仅保存规则所需的最小、已脱敏、可复现证据。

Langfuse API 的物理字段名仅存在 Normalizer source profile。最初的 profile 必须由 S4.0 报告确认服务/API/SDK 版本与别名；不匹配 profile 或 provider major version 时明确返回 UNAVAILABLE/PARTIAL，而不是在每个业务规则中堆叠 provider 分支。

按01冻结同一source_profile_set与选择算法，两个框架共用该合同；实际profile从冻结AgentVersion.trajectory_source_profile_id选取并保存provenance，不由Trace自报覆盖。新增nullable顶层AgentVersion数据库列及Spec/DTO字段；非空值进入spec_digest，空值保持旧digest，不能放入当前不参与spec_digest的metadata。注册/Manifest冻结测试覆盖变更创建新版本、禁止原地改旧版本；同步OpenAPI与Console生成类型。profile分支不同不单独改变Measurement，获准profile集合/算法变更才改变合同。

## Acquisition 与 Normalizer 算法

生产调用严格采用 01 中签名：

    async def acquire_trajectory(scope, contract, *, client: ObservationReader) -> TrajectoryAcquisition
    def normalize_observations(rows, contract, *, scope) -> NormalizedObservations
    def assemble_trajectory(scope, observations, contract) -> StandardTrajectory

调用方只从完成中的 ExperimentItemExecutionRecord、Attempt 和冻结 Manifest 构造 scope/contract。不得另加 trace_id、attempt_id、profile、cursor 或 timeout 的旁路 API 参数；分页状态属于 acquisition 内部，reader 仅由依赖注入提供。

Normalizer 先校验原始字段类型，再依据冻结 source profile 做一对一映射：Langfuse observation ID → observation_id；trace ID → scope.trace_id；parent observation ID → parent_step_id；开始/结束时间 → started_at/ended_at；provider type/name → type/raw_name；获准的 input/output → redacted arguments/result 和轨迹 input/output。最终别名以 S4.0 实测为准。别名同时存在且值冲突时不挑一个赢家，丢弃冲突记录并降低完整度。

字段缺失按能力处理：无 ID 或 trace ID 的行不可形成可回链 Step；无 parent 不能推定为 root；缺时间只影响 ordering；缺 input/output 只影响相应参数/内容能力；未知工具名保留 raw_name、无 tool_identity 命中且由既定 warning 表示。任何映射不得根据 tool name 猜 phase、状态、父链或业务身份。

MISSING 的语义必须精确：只有完整参数对象已被成功采集、可解析，且指定 JSONPath 在该对象中确实不存在，才能把该路径标为 MISSING。整个 arguments/input 未采集、被截断、空指针或无法解码时，不能把所有路径都填成 MISSING；应将 tool_arguments capability 降为 PARTIAL/NONE，并通过已有 ArgumentVisibility 状态区分 REDACTED、MISSING、INVALID、AVAILABLE。S4.1 只准备正确证据，不执行参数规则。

可选因果字段依照 01 约束处理。Normalizer 只从 S4.0 实测且冻结的 profile 提取显式 span link，或同 workflow_scope_id 内具有可信单调语义的业务 OTel 序列关系，再把其 predecessor 映射到当前 scope 内的 observation_id/step_id。不得仅从 parent_step_id、started_at、数组顺序或原始 name 推导 causal_predecessor_ids。workflow_scope_id 仅保留明确 allowlist 的非敏感业务关联值；不满足准入条件时不填可选字段并降低 ordering coverage。

如果同 workflow 的序列不连续、重复、倒退或跨 scope 混入，相关步骤不建立隐式前驱边。显式 link 指向闭包之外/未知 observation 时也不自动拉入该节点；记录异常并将 ordering 设为 PARTIAL/NONE。并行步骤即使都有同一个 parent，也保持并行；只有经 profile 验证的业务因果来源能增加先后关系。

started_at/ended_at 必须带时区并转换为 UTC 微秒精度；无效值保持 null 并记 warning，不能补当前时间。duration_ms 仅从 S4.0 验证并冻结的显式时长字段映射；若来源不提供或值无效，保留 null 并降低 timing capability，不得用 started_at/ended_at 相减补造，以免把跨进程 clock skew 写成 Step 执行语义。该缺失不降低无关工具身份能力。status 只从 source profile 明确字段映射；phase 必须来自明确的语义属性/已验证映射，不能把“span 类型是 tool”自动等价为已执行。若 profile 无法给出必填 Step 语义，该行不参与确定性规则证据，并使相关 capability 降级。

input/output/arguments 进入 StandardTrajectory 前先运行统一 redaction policy 和长度/字节限制。禁止保存 Authorization、Cookie、令牌、环境变量、完整 SDK 对象、任意 metadata 或 raw Observation dump。脱敏结果及可见性按 01 的 ArgumentVisibility 表达；只留能支持后续已冻结规则的最小证据。

采集算法：

    1. 从 scope/contract 确定 trace、root、Attempt、版本和 deadline；使用单调时钟计运行预算。
    2. 在 contract 的 max_inflight_acquisitions semaphore 下查询 Langfuse v2 ObservationReader；每次请求受 request_timeout_seconds 约束。
    3. 按冻结 page_size 拉取至游标耗尽；维护 cursor 集合检测循环，达到 max_pages、max_source_bytes 或页面错误时保留已读页并标明不完整。
    4. 每轮对记录做 Normalizer 和 scope 校验；以稳定规范化集合签名比较完整分页结果，记录迟到新增/变更。
    5. root/分页穷尽后，核对profile的业务terminal事件与最终event sequence/count，全部对应事件可读且无缺口，再要求quiet window稳定、能力满足；仅HTTP Root结束或静默不足以COMPLETE。terminal安全计数保留在Step.result_summary，支持冻结后复算。
    6. Root-only 需等待到契约截止，届时返回 READY + ROOT_ONLY；有子节点但仍有缺页/能力缺失则 READY + PARTIAL。尚未到 deadline 的暂态返回 PENDING。
    7. 无 root、永久鉴权/契约错误或 deadline 后仍不可查询时返回 UNAVAILABLE；reason_code 与 retryable 表达故障，不重调 Agent。
    8. READY 轨迹经 Assembler、脱敏和大小校验后写 insert-only artifact，再将 TrajectoryEvidenceRef 与最终 TrajectoryAcquisition 写入 checkpoint 扩展。

跨页同 ID 记录按 provider+observation_id 去重。canonical 内容相同的重复行保留一份；同一键的 trace、parent、时间或证据字段冲突时排除该节点、保留冲突 warning、标 PARTIAL。分页游标回环、超过页/字节/step 上限一律不能被解释为“未观察到更多调用”。如已存在独立可信违规 Step，冲突记录不得抹去其它可证实的违规事实。

## 图闭包、时序与 digest

实现按中等规模增量拆分：内部 `trajectory/graph.py` 的 `assemble_root_graph` 先输出绑定 scope/合同/来源的 `RootObservationGraph`，只负责身份闭包、去重、稳定展示顺序和 Step 预算；后续 `assemble_trajectory` 消费它完成因果、terminal inventory、能力/完整度、最终制品预算和 digest。内部图不替代 `StandardTrajectory` 或完成协议。实现与验证见 [Root 图验收](../../validation/s4-1/root-graph.md)。

Assembler 先确认 scope.root_observation_id 对应的唯一 root，随后只沿真实 parent_step_id 递归收集其后代；父边之外的同 Trace 兄弟树不进入本 Attempt。每条记录的 trace_id 必须等于冻结 scope.trace_id。root 的 Launch/Item/Attempt/generation 来源是 Runner 数据库身份，不从 Trace metadata 或请求 Header 自报覆盖。其它 Attempt 即使 Trace ID 相同也不并入当前闭包。

使用 observation_id 建索引，验证父引用存在性、唯一性、同 Trace 和无环。缺 parent、跨 Trace 子节点、重复冲突、孤立节点、父环均保留稳定 warning。Assembler 可按 01 规则断开无效边以形成可展示子图，但相关工具执行/时序能力降级；不能虚构 parent。成环节点不能参与因果判定。

并行 span 保留其真实 parent 图。index 只服务稳定展示，按 UTC started_at、父深度、observation_id 排序；缺时间使用显式 null 排序位。时间戳只用于 UI 排序和显示，不是兄弟步骤的因果凭证。重叠区间、跨服务 clock skew、没有可证因果边时不强行串行。父 Observation 只证明结构上的 parentage，不证明两个兄弟 span 的 happens-before。只有 profile 验证的显式因果 link 或同工作流可信单调序列才可以生成 causal_predecessor_ids。S4.0 负责校准字段与序列属性；无可靠证据时 ordering 必须是 NONE/PARTIAL，未来 tool_order 返回 UNKNOWN。S4.1 不提前运行该规则。

Assembler 校验因果边时要求 predecessor 与当前 Step 都在同一个冻结 scope/Attempt 内，且 predecessor ID 唯一存在、没有自环或环路。因果边与已验证工作流序列冲突时，不选一方作为真值；剔除冲突关系、记录 warning，并降低 ordering coverage。display index 与因果图是两个并存的数据面，任何下游测量不得把 index 当作 predecessor 列表。

内部 `trajectory/causal.py` 的 `validate_causal_links` 消费同一 scope/批次/合同并调用 Root 图阶段，校验显式 event 引用、同 workflow 的事件/序号唯一性、原始回环和时间关系，返回已验证 observation 前驱及唯一 request/action 配对。它不生成隐式全序，不传递参数、不作完整度结论；最终 `assemble_trajectory` 消费这些映射。terminal 虽不作工具顺序节点，仍参与事件命名空间冲突检查。实现、参数来源状态修复和实测见 [显式因果验收](../../validation/s4-1/causal-links.md)。

上限采用 01 的起始提议：1000 steps、100 条/页且最多 20 页、最多 8 MiB 源数据、最多 2 MiB artifact、每请求 5 秒、采集总期限 60 秒、静默观察 2 秒、同时最多 4 个 acquisition。它们不是已承诺 SLA：S4.0 的真实延迟/体积矩阵与 S4.1 的本地负载测试必须校准，所有结果相关限额纳入冻结 TrajectoryContract/digest；调参应生成新 contract 身份。

canonical digest 使用 UTF-8、键排序、紧凑 JSON、UTC 微秒、拒绝 NaN/Infinity、按稳定规则排序 Step 与 warnings，再计算 SHA-256。摘要主体按 01 中 StandardTrajectory 的冻结 scope、脱敏输入/输出、completeness、steps 组成；计算时排除 provenance 内自身的 trajectory_digest，避免循环。source profile、schema、assembler、mapping 与 redaction 的配置身份由 trajectory_contract_digest 固定。

Step.duration_ms 表达 Step 执行语义；若来源提供且 profile 验证该值，则它属于 StandardTrajectory 的语义字段，必须进入 trajectory_digest，值变化必须改变摘要。字段缺失保持 null 并降低 timing capability，不得用采集时间推算。仅排除采集/组装运行耗时、采集时刻、临时 Worker ID 和不稳定错误文本。完全相同的冻结规范化输入必须字节级得到相同 digest；增加/删除/修改一个证据 Step 或其已提供的 duration_ms 必须改变摘要。

## Artifact、checkpoint 与恢复

新增trajectory_evidence_artifacts表按01的字段清单保存一份最小StandardTrajectory JSON和所属身份：id、launch_id、item_execution_id、attempt_id、trajectory_contract_digest、trajectory_digest、schema_version、payload_json、created_at。payload_json用TEXT存canonical UTF-8 JSON，局部Decimal decoder/encoder完成精确往返；不改变ORM全局JSON序列化。外键复用既有Launch/Item/Attempt身份；唯一键为item_execution_id + attempt_id + trajectory_contract_digest + trajectory_digest。payload不超过冻结max_artifact_bytes，数据库以octet_length约束字节上限；仅保存脱敏内容。写入使用insert-only：命中唯一键时读取并校验既有记录，不更新旧payload。读取时重算digest，并验证Launch/Item/Attempt/Schema/contract/ref相互一致。

现有execution_checkpoints按item_execution_id + dispatch_generation唯一，保存Agent输出及其冻结Manifest provenance。迁移增加可空JSON字段trajectory_acquisition_json，值只序列化01的TrajectoryAcquisition，不保存Observations或第二份完整Trace。T1统一拥有AgentVersion profile列、artifact表及checkpoint扩展的Schema迁移，T2复用它接入持久化；本次基线最高016，候选文件migrations/017_trajectory_evidence.sql。真正实施时必须重新查看最新main和并行迁移，按序号追加；不得改已发布迁移。

执行顺序保持不重调 Agent：成功收到业务响应后，先写现有 execution checkpoint；随后完成有界 Acquisition；READY 时写 evidence artifact，再以当前 generation/lease/CAS 更新 checkpoint acquisition ref；最后才交由后续阶段的冻结 Evaluator 消费。UNAVAILABLE 表达采集不可用；READY 可携带 ROOT_ONLY 或 PARTIAL completeness。它们只表达证据状态，不回滚 Agent 成功响应，也不在 #128 判断规则或质量。S4.1 不改变旧 Manifest 的行为。

本期提供 trajectory contract 的 Manifest 1.3 序列化与 checkpoint/artifact；Manifest 1.2 及其它已注册历史版本按原逻辑执行。Snapshot 引用的 serializer/validator 可在本期实现，但不提前写 evidence_contract_version=trajectory-1.0。T4 同时接入 required_evidence_status、新聚合合同与该 discriminator 后，才生成新资格快照，避免出现缺字段的半契约 Snapshot。

S4.1 不添加 rule_results、不改质量状态、不改变 Baseline/Gate 资格；Context Adapter 与正式 Binding 启用在 #129。测试证明旧 Snapshot 字节语义兼容、EvidenceRef 可校验以及本期不会提前激活资格分支。不按字符串排序比较版本，不回填历史 Snapshot。

恢复读取 checkpoint 时，校验 Agent output digest、冻结 Binding provenance、trajectory contract digest 和 acquisition ref。已有 READY artifact 必须从 Argus 的不可变制品恢复，不读当前 Langfuse Trace；PENDING 状态仅可在原 deadline 尚未过期时续查同一 scope/contract，恢复不能重置 deadline；deadline 已过则固化可用的 ROOT_ONLY/PARTIAL/UNAVAILABLE 结果。READY artifact 的保留跟随引用它的 Result Snapshot，不继承 execution checkpoint TTL，也不另设自动 TTL/清理任务。Artifact 因授权保留/删除策略、损坏或身份不符而不可用时，必须明确记录 unavailable 原因，后续消费降为 UNKNOWN。所有路径均禁止静默改用最新 Trace 或重调 Agent。

采集在 Runner 中使用独立的有界 semaphore；等待时释放 Agent HTTP 并发许可。每个 async task 必须在取消时释放 semaphore/HTTP response；网络请求有 timeout；总运行时间、游标数、页面、原始字节、step 数和保存字节均硬限。发生 lease/generation 变化时，旧 Worker 不得更新 checkpoint 或覆盖新结果；写入前沿用现有 Attempt/lease/CAS 核验。所有原始响应在离开 acquisition 调用栈后释放。

读取范围以 root ID 与 trace ID 的交集收窄；每页之后立即进行身份过滤和大小核算，不先把整个 Trace 反序列化进内存。单行 input/output 也必须在递归解析前施加深度、字符串长度和总字节限制，避免嵌套 JSON 或超长错误值绕过 max_source_bytes。

取消与超时路径按阶段处理：取消只结束本次 Acquisition 并释放资源；Langfuse 暂时 429/5xx/timeout 在原 deadline 内可重试；鉴权/版本/参数错误标为不可重试；页上限/字节/Step 上限是证据截断，不是服务不可用。所有状态都保留已验证的 root/scope 身份，不能把传输故障转换为“无工具”。

Source profile 在 S4.0 后按版本注册，并通过本地 repeatable fixture 校准：

    1. 对native_otel、Pydantic AI、HTTP-only分别运行至少10次，分开统计工具执行、请求、迟到span和并行场景。
    2. 对每个 profile 记录观察总数、九项 Observation 字段 presence、link/workflow sequence 可用率和父链完整率。
    3. observation_id 与 trace_id 作为回链必需字段；其缺失样本必须解释来源，不能通过“多数样本有值”忽略。
    4. 只有在相应场景每次都可验证的能力才声明 COMPLETE；其它能力按证据标记 PARTIAL/NONE。
    5. 用迟到查询的 P95 与最大可重复观察值选择 deadline/quiet window；做一轮边界测试验证 deadline 前后状态转换。
    6. 用单 Trace 大小与多页查询压测评估 page_size、max_pages、max_steps、source/artifact byte cap 和并发 semaphore。
    7. 将最终 source_profile_version、所有结果影响限额及映射一起冻结进 TrajectoryContract；重新校准必须产生新 contract identity。

十次是本地校准的最低建议样本量，不等于生产 SLA 或统计保证。若 S4.0 矩阵展示某字段/顺序证据仅在特定 Agent instrumentation 下存在，必须分成不同 source profile；不得用更宽松的 profile 把 HTTP-only 的 NONE 覆盖成 COMPLETE。

状态收敛断言表：

| 注入/实测条件 | 应断言的采集结果 | 允许的证据结论 |
|---|---|---|
| 暂无查询结果且未到 deadline | PENDING | 当前尚不能判完整/缺失 |
| 查到 root，采集尚在 quiet window | PENDING | 只报告当前观测，不冻结否定结论 |
| deadline 后只有有效 root | READY | ROOT_ONLY |
| 有子步骤但分页/能力/父图仍有缺口 | READY | PARTIAL |
| root、必需能力、页与 quiet window 均收敛 | READY | OBSERVED_COMPLETE |
| 永久 API 错误、无 root 到期或来源契约不兼容 | UNAVAILABLE | 不能判规则通过 |

completeness 与能力覆盖是当前已采集证据的描述，不替代 required/optional 质量策略；后续规则仍须按自己的能力要求产出 typed result。S4.1 只保证这些状态确定、保存在 checkpoint 和 artifact 引用中。

## RED/GREEN 与验收标准

先为 schema、Acquisition、Assembler 和 evidence 写测试，再按契约实现。测试工厂集中在 tests/trajectory_fixtures.py；所有 reader 返回只用于边界/错误注入，真实 API 字段与父链已由 S4.0 的本地集成测试验证。不得将 Mock 测试计为真实 Langfuse 验收。

测试按风险切片，而不是只用一条 happy path：

| 测试文件 | 必须覆盖的样例 |
|---|---|
| test_trajectory_schema.py | 必填 ID、合法/null 时间与 duration_ms、未知版本、超大 payload、ArgumentVisibility 含义 |
| test_trajectory_acquisition.py | 空首查、迟到页、限时重查、429/5xx、永久 4xx、重复 cursor、page/byte/deadline cap |
| test_trajectory_assembler.py | Root 闭包、兄弟树排除、不同 Attempt、同 ID 同内容/冲突、orphan、parent cycle |
| test_trajectory_assembler.py | 并行 span、缺时间、倒退/重叠时间、显式 link、可信 workflow sequence、错误 scope |
| test_trajectory_profile_selection.py / test_launch_manifest_freeze.py | 顶层profile持久化及冻结、非空spec_digest变化、旧空值digest不变、未获准profile拒绝、Trace自报不覆盖 |
| test_trajectory_evidence.py | canonical key order、UTC microseconds、NaN拒绝、Decimal往返及digest不变、duration_ms变化、缺失duration_ms保持null、insert-only和唯一键竞争 |
| PostgreSQL migration test | 新迁移、外键/唯一键、nullable checkpoint extension、Manifest/Snapshot 新旧版本读写 |
| recovery regression | READY ref 重用、PENDING deadline 连续、checkpoint TTL 到期不删除 Snapshot 引用的 artifact、经授权删除导致证据 unavailable/UNKNOWN、损坏/身份不符显式失败、Agent invocation count 不增加 |
| integration regression | 接入 #127 本地真实 Langfuse；profile 与实际物理字段/父链一致 |

用例对 causal_predecessor_ids 单独断言：相同 parent 的 sibling 加上时间先后，结果仍没有新因果边；没有可靠序列时 ordering 只能为 NONE/PARTIAL；单调序列必须限定同一 workflow_scope_id，且 predecessor 均可解析为本 scope 的 observation/step。错误 workflow scope、重复序号、序号倒退、跨 Attempt link 均不能通过降序排列后继续用作证据。

ArgumentVisibility 的边界至少覆盖：完整参数对象含目标路径 → AVAILABLE；完整参数对象不含目标路径 → MISSING；字段存在但按策略隐藏 → REDACTED；采集到非 JSON/非法形状 → INVALID；整个参数对象不存在或采集失败 → 不伪造每个路径的 MISSING，降低 tool_arguments capability。

RED 阶段应先运行：

    python -m pytest -q tests/test_trajectory_schema.py tests/test_trajectory_acquisition.py tests/test_trajectory_assembler.py tests/test_trajectory_evidence.py

确认新测试对尚未实现的 package/schema/行为失败后，按 schema → Normalizer/Assembler → Acquisition → artifact/recovery 顺序实现。每组 GREEN 命令分别重跑对应文件；全阶段完成后运行：

    python -m pytest -q tests/test_trajectory_schema.py tests/test_trajectory_acquisition.py tests/test_trajectory_assembler.py tests/test_trajectory_evidence.py tests/test_trajectory_observation_integration.py
    ruff check services/eval-runner/app/trajectory tests/test_trajectory_schema.py tests/test_trajectory_acquisition.py tests/test_trajectory_assembler.py tests/test_trajectory_evidence.py tests/test_trajectory_observation_integration.py
    python -m pytest -q tests

涉及数据库的测试使用 PostgreSQL 测迁移与 JSON/唯一键语义；SQLite 单测不能代替 migration integration。按仓库门禁完成 make validate。命令是未来实施验收计划，本方案 PR 不运行或宣称通过这些测试。

| 验收 ID | 可审查结果 |
|---|---|
| S41-01 | Schema/Scope/Step/Completeness/Provenance/EvidenceRef/Acquisition与01一致；profile_set合同固定，两AgentVersion选不同获准分支仍同Measurement；未获准分支拒绝，实际profile provenance可复现 |
| S41-02 | acquire_trajectory 只读使用冻结 scope/contract；不得调用 Agent；PENDING/READY/UNAVAILABLE 与 ROOT_ONLY/PARTIAL/OBSERVED_COMPLETE 轴分离 |
| S41-03 | S4.0 的每个 provider profile 字段都有唯一映射和缺失处理；别名冲突、未知版本、缺 ID/parent/time/input/output 不猜测、不伪造 |
| S41-04 | 同 trace 的兄弟 Item、不同 Attempt、跨 Trace 行均被排除；仅从指定 root 的真实 parent 闭包组装 |
| S41-05 | 相同重复 observation 合并；冲突 duplicate、orphan、parent 缺失/环、跨 trace 和时钟异常都落 warning/PARTIAL；独立可信违规证据不会被冲突抹掉 |
| S41-06 | 并行调用不被 index 强制成因果序；parent/time 单独不生成因果关系；profile 验证的显式 link/可信 workflow 序列可产生可回链 predecessor，否则 ordering 为 NONE/PARTIAL |
| S41-06a | 参数路径仅在完整 arguments 对象已采集且确实缺键时为 MISSING；整个参数未采、脱敏、截断、解析失败分别降低能力或按已有 visibility 区分 |
| S41-07 | 分页/环/迟到/页字节step/deadline/永久错误均有资源上界；terminal inventory缺口不能被quiet window覆盖，冻结安全计数可重算完整性 |
| S41-08 | 同冻结规范化输入/contract 得到相同 digest；改一项证据或已提供的 Step.duration_ms 得到不同 trajectory_digest；字段缺失保留 null 并降低 timing capability；仅采集/组装耗时与采集时刻不污染摘要 |
| S41-09 | artifact insert-only、唯一键/外键正确、最大字节受限、读取重算 digest；PostgreSQL migration 往返测试通过；artifact 与引用它的 Result Snapshot 同生命周期保留，不受 execution checkpoint TTL 影响 |
| S41-10 | checkpoint 恢复复用已冻结 EvidenceRef；PENDING 沿用原 deadline；授权删除、损坏或身份不符导致 artifact unavailable 时结果显式降为 UNKNOWN；恢复不重调 Agent、不以线上最新 Trace 替换历史输入 |
| S41-11 | 全部原始参数/响应先过 redaction；产物、API、日志和异常文本均无未授权 PII、Authorization、Cookie 或密钥 |
| S41-12 | 提供Manifest 1.3与EvidenceRef序列化；checkpoint/artifact引用一致；本期不写Snapshot资格discriminator，不产出半契约快照；旧Manifest/Snapshot可读且不回填；Registry可选字段及OpenAPI同步，无新诊断路由/Console交互/规则Gate行为，Demo 2/6、6/6保持 |
| S41-13 | 本地真实集成、目标单测、迁移测试和 make validate 全通过，所有实测命令与 Langfuse profile 版本归档 |

## 依赖、退出条件与回退

依赖 #127 通过 S40-01 至 S40-12，冻结本地 Langfuse/SDK/Agent 来源profile和实际字段矩阵；依赖#5的01技术契约被方案PR接受。若S4.0证明父关系或API字段不足，先修订契约与验收，再编码，不能在Normalizer中猜测。

退出条件为S41-01至S41-13（包括S41-06a）全部有测试/实测证据；采集状态、完整度、能力覆盖相互区分；artifact可按原引用恢复且保留边界符合Result Snapshot生命周期；授权删除造成证据不可用时下游fail closed为UNKNOWN；因果关系只来自已验证profile；参数MISSING不与整段未采集混淆；没有规则判断或已有质量结论漂移。达到此条件后，#129可以基于StandardTrajectory编写确定性规则。

回退通过关闭新trajectory Binding/新采集配置，使新Launch继续旧最终答案路径。迁移只追加nullable AgentVersion profile/checkpoint字段和新evidence表；不删除或改写现有结果、Snapshot或迁移。若发现制品错误，停止新Binding、保留原制品供排查，不清理已提交轨迹数据；读者不兼容时显式失败，不静默退化为在线Trace。

v1 脱敏后的不可变 trajectory artifact 与引用它的 Result Snapshot 同生命周期保留，不继承 execution checkpoint TTL；不增加独立自动 TTL 或清理任务。01 表结构没有 expires_at，实施不得擅加该字段。部署方明确授权的 Snapshot 保留/删除策略可以令 EvidenceRef 指向的证据 unavailable；读取方必须将后续结论降为 UNKNOWN，并保留 unavailable 原因，绝不从最新 Langfuse Trace 替换历史证据。#6 的数据治理深化属于后续工作，不阻塞满足本方案脱敏、不可变和 fail-closed 约束的 S4 v1 生产持久化。
