# S4.0 观测链路验证：#127

状态：提议，待方案 PR 评审；以下为实施和验收设计，不表示测试或修复已经完成。核验基线：2026-10-13，main@4b043739c63357ecc10131e1ba54bc1bc53d862a。共同契约见 [01 技术契约](01-technical-design.md)。

## 目标、现状与 Issue 修订

S4.0 的退出条件是用本地真实 Langfuse、两个真实 Agent 实现和一个 HTTP-only Agent，测出 Runner 创建的 Observation 与 Agent 内部 Observation 如何关联、哪些字段稳定可读、Trace 何时可查询。只有这些证据可以支持 S4.1 冻结来源映射和采集策略。S4.0 不实现 StandardTrajectory、Assembler、规则或质量判断。

GitHub 上 #127 当前为 Open，正文要求 Runner 透传 W3C traceparent、比较两种实现、输出字段矩阵和最小复现；验收还要求复现脚本进入 tests/ 且不依赖线上服务。#5 将 S4.0 作为 S4.1 的 P0 技术门槛。核验时 #127 没有 GitHub parent/sub-issue 关系或关联分支；建议将 #127 设为 #5 子项，#128 须等 S40-01 至 S40-12 全部通过。

PR #133 的 head 2330576b8c6c6b80c0815d97e808a537f24c2d56 是本方案评审背景，不是 main 的实现状态。其 MockTransport、模拟 Agent 与手写 Observation 字典能检验 traceparent 字符串、审计代码和测试辅助函数，但不能证明真实 Langfuse ingestion/query 的字段形状、异步可见性或真实父 Observation 关系；只比较 trace ID 也不足以证明 parent/root/item/attempt 关联。因此 PR #133 可作为测试原语的起点，其现有 Mock 结果不能单独满足 S40-02、S40-03 或 S40-05。

现有代码有两条入口：队列 Worker 在 remote-agent-http Observation 内执行 inject；同步 execution.py 在创建该 Observation 前注入。这个差异是待验证假设，不预先认定为缺陷。测试必须走真实生产入口并记录结果；仅当实测证明父链不正确时，才在对应入口最小调整注入时机。

建议将 #127 标题改为“[S4.0] 本地真实 Langfuse 链路、字段矩阵与采集延迟验证”，并修订范围为：

- 用项目固定版本的本地 Langfuse 服务验证 Runner → HTTP Agent → OTel spans → Langfuse API 的写入、查询和父链。
- 用原生OTel Agent与实际Pydantic AI Agent运行相同工具调用场景；另用HTTP-only Agent验证Root-only退化边界。
- 记录真实 Observation 字段、分页和延迟矩阵；提交可重复运行的本地测试与可审查报告产物。
- 保持 Agent 零 Evaluation SDK；不改 services/demo-agent 及其 v1 2/6、v2 6/6 基线。
- 不冻结 schema、不新增 Assembler、不新增规则、不改变 QualityPolicy 或 Snapshot。

建议维护者将 #127–#132 设置为 #5 的直接子项，阶段依赖另行记录；#128 依赖 #127，不应因此误设为 #127 的子项。本方案 PR 用 Refs 关联，不宣称已修改原生父子关系。#128 不得以 Mock 字段或 PR #133 的 header-only 结果替代 S4.0 真实链路报告。

## 测试隔离与真实链路

新增tests/test_trajectory_observation_integration.py，测试Agent夹具放入tests/fixtures/trajectory_agents/。夹具包含native_otel、pydantic_ai与http_only三个独立进程入口；Pydantic AI与OTel依赖写入专用锁定清单，锁住完整依赖闭包，不加入services/demo-agent运行依赖。Agent端只使用普通业务HTTP、OpenTelemetry SDK/exporter和实际框架，不安装Langfuse、Argus或其它Evaluation SDK。

#127的LangGraph是两种实现的示例。本方案选pydantic-ai-slim==2.55.0（不加extras、不安装pydantic-ai meta包）：2026-10-13核对的[发布记录](https://pypi.org/project/pydantic-ai-slim/2.55.0/)及[该版依赖声明](https://github.com/pydantic/pydantic-ai/blob/7db7a4d8c4b7b7b85b8636cf0b6e3305bafb5167/pydantic_ai_slim/pyproject.toml)将evals/logfire列为可选项。实施时在Agent独立环境审计完整锁文件及已安装distribution，禁止pydantic-evals/langfuse/langsmith/Argus Eval SDK，也不安装logfire SDK；pydantic-graph附带的logfire-api是无SDK时no-op的API shim，不能把它当真实观测来源。依赖闭包或本地无网运行不满足时，先修夹具依赖，不绕过零SDK门禁。

测试启动独立 Compose project 名称与专用临时卷，运行仓库已固定版本的本地 Langfuse 及其依赖。通过测试专用监听端口连接，不连接 Langfuse Cloud、不读取开发者现有 .env、不复用 .env.poc 的 Langfuse 凭据。项目 ID、public/secret key 和随机端口仅在测试进程中生成；输出日志只记录 trace/observation ID 与字段名称，不打印输入、参数、Header、密钥或完整 Observation。

Agent 进程绑定随机 localhost 端口。Runner 使用隔离的测试数据库/队列 fixture 创建一个 Launch、DatasetItem 和 Attempt，沿 Worker 的生产执行路径发起请求；同步 execution.py 路径另加一项兼容验证，确认其注入位置对 remote-agent-http 父链的实际影响。所有服务 base URL 在 fixture 启动时断言是 loopback/隔离 Compose 网络；不满足即测试失败，不回退到外网默认地址。

两个带OTel的Agent使用相同确定性业务场景和不同实现：原生Python function-call编排执行两个本地工具函数；Pydantic AI调用真实await agent.run(...)和注册业务工具。使用该版本地FunctionModel按消息历史编排三轮：第一轮仅请求customer_lookup；检查其ToolReturnPart已返回后，第二轮才请求balance_read；第三轮返回业务结果。物理工具名由profile映射到共同identity，不调用外部LLM。Agent从请求traceparent提取上下文，再以普通OTel tracer创建Agent、工具请求与执行spans并导出到隔离Langfuse，业务结果不依赖这些spans。[该版测试指南](https://github.com/pydantic/pydantic-ai/blob/7db7a4d8c4b7b7b85b8636cf0b6e3305bafb5167/docs/testing.md)说明TestModel默认会一次调用所有工具，因此本串行测试使用FunctionModel；这证明框架工具执行链，不宣称证明真实模型规划质量。

工具步骤通过正常业务OTel附带可审核的workflow scope与事件sequence，终结事件带最终event sequence/count；均来自业务编排状态，不加入DTO、不用Evaluation SDK。实测terminal已到但前序span迟到/缺号时仍不完整，不能以HTTP响应结束替代完结协议。事件全局递增只核对覆盖；严格串行依赖或显式dependency link才可能映射因果，普通OTel link也必须验证业务语义。两种Agent作为同一测试AgentDefinition的独立不可变版本，后续共用包含两profile的同一合同；不改现有Demo版本。

同一夹具必须覆盖 REQUESTED、EXECUTED、BLOCKED 及 SUCCEEDED、FAILED、CANCELLED 的业务事件：请求和执行分别埋点，拒绝后的请求保留 REQUESTED，阻断事件为 BLOCKED。记录语义属性的物理键、产生位置、类型、presence 和请求→执行关联，不凭 tool 类型猜 phase/status。工作流先后属性须表达成功完成→后续请求，不能只记录开始序号。参数使用合成非敏感数据，验证完整对象、缺字段、策略隐藏、未采集四种路径可见性；报告保留分类和路径，不保留原值。duration_ms 若有显式来源也纳入矩阵，没有则 timing=NONE/PARTIAL。

HUMAN 正例需来自隔离审批系统测试事件，经实际 OTel→Langfuse 查询证明来源、事件 ID 与 workflow scope 的映射；这是合成审批测试记录，不声称真实人员已审批。另测完整覆盖但无事件，以及普通名为 escalate 的工具。没有获准审批来源的 profile 声明 human_events=NONE/PARTIAL，后续人工规则只能 UNKNOWN，不能用工具改名伪装人工事件。

HTTP-only 夹具只实现正常业务 HTTP API，不创建内部 OTel span。它必须返回与另两种 Agent 相同形状的业务响应。该用例检查只创建了 Runner 的 remote-agent-http Observation，且最终答案评测继续成功；报告只证明“在本次冻结采集窗口内没有观察到内部 Span”，不声称业务中绝对没有隐藏动作。

测试对 Langfuse 使用实际部署版本的只读 Observation API/SDK 查询，并以真实返回对象生成矩阵；S4.0 只在测试辅助代码中读取，不提前引入生产 ObservationReader 接口。采集查询按 API 支持的页参数强制形成多页；测试逐页收集后以 Observation ID 去重，断言页游标终止、结果集合与 Langfuse UI/API 单次完整查询一致。字段别名和页参数以本地部署的 OpenAPI/SDK 实测为准，报告保留版本号。

为测量迟到可见性，Agent fixture 在业务响应后以受控延迟导出一个仍携带原始 parent context 的子 span。测试先查询并记录 Root-only 快照，再轮询本地 Langfuse，验证迟到 span 出现后同一 Trace 和 parent ID 可读。测试使用单调时钟记录延迟分布；超出本次测试 deadline 的事件只记为超时，不把“首次没有子 Span”解释为“没有工具调用”。S4.1 的 quiet window 与 deadline 数值须由这份分布和 S4.1 acquisition tests 再校准。

真实 Observation 字段矩阵按来源 profile、span 类别和重复运行列出：observation_id、trace_id、parent_id、started_at、ended_at、type、name、input、output。每项记录返回键名/别名、存在次数/总次数、类型、空值与缺失次数、是否需映射、受影响能力，以及能否支持父链/时间/参数判断。矩阵另列因果来源：API 是否保留显式 span link、是否保留 workflow_scope_id 和单调 sequence 属性、序列在每个 scope 内是否无重复/倒退，以及能否据此建立 causal_predecessor_ids。矩阵只统计字段元数据，不存储真实输入或输出值；将 Agent 自埋点的语义与 Langfuse API 的物理字段分列。

矩阵按 profile 分开报告，不把三种 Agent 的统计合并平均。每个字段行都附样本量和 span 类别；必填身份字段的缺失样本逐条追踪原因。顺序证据单独报告“明确因果 link”“同 workflow 的可信单调序列”“仅 parent 边”“仅时间戳”“无顺序证据”五种观察结果。前两类是后续可能的 causal evidence，后三类不能单独证明兄弟步骤 happens-before。

## 文件责任与执行算法

| 路径 | S4.0 责任 |
|---|---|
| tests/test_trajectory_observation_integration.py | 真实本地端到端场景、链路断言、分页/迟到查询、矩阵生成 |
| tests/fixtures/trajectory_agents/native_otel/ | 原生 OTel Agent 与本地工具 spans |
| tests/fixtures/trajectory_agents/pydantic_ai/ | slim锁定依赖、真实Agent.run/FunctionModel与同语义spans |
| tests/fixtures/trajectory_agents/http_only/ | 无内部 instrumentation 的业务 HTTP Agent |
| tests/fixtures/trajectory_agents/compose.yaml | 独立、本地、临时数据卷与测试端口；不改仓库生产 Compose |
| services/eval-runner/app/worker.py | 仅在真实集成失败时，按 S40-02 证据修正当前 attempt 的 context 注入边界 |
| services/eval-runner/app/execution.py | 仅在同步路径实测失败时修正 inject 与 Observation 的最小相对顺序 |

测试不得把 Observation mock 注入到真实链路验收；Mock 只保留给纯解析/异常分支的单元测试。不得增加生产 Assembler、Trajectory Schema、Evaluator/Rule、DB migration、Console 或 Agent SDK 依赖。

端到端步骤：

    1. 创建隔离 Langfuse project、Runner test DB/queue 和三种 Agent 进程。
    2. 发起一个 Launch Item，读取真实 item_execution_id、dispatch_generation 和 ExecutionAttemptRecord.id。
    3. 等待 Agent 完成；从 Runner 持久记录取 trace_id、remote-agent-http observation_id 与 final_attempt_id。
    4. 以 Langfuse 只读 API 分页读取 trace_id 对应 Observations，轮询迟到的 Agent spans。
    5. 用实际返回对象建立字段矩阵；通过 parentObservationId/等价实测字段逐层校验 root 到 Agent spans 的父链。
    6. 比较 scope 中的 Launch/Item/Attempt/Generation/Trace/Root 身份；不以时间窗口或工具名推断归属。
    7. 对 HTTP-only 场景记录 Root-only 观察结果，并确认最终答案 Evaluation 仍成功。
    8. 退出前停止 Agent 与 Compose project、移除本次临时卷；清理只作用于测试随机命名空间。

每次报告必须并列展示 Runner 记录、API 原始字段名、规范化候选映射和关联判定。断言 parent 时检查 Agent 根 span 的 parent ID 是 Runner 的 remote-agent-http Observation ID，或者由 Langfuse 真实字段证明的等价直接关系；若关系落在 item execution 外层 Observation，报告两层 ID 和链路，不能只因 trace ID 相同而判通过。

父链核验使用逐边 ID 连接，而不是将 Trace 中所有 Observations 都视为当前 Case。报告需展示 runner item_execution_id 与 dataset_item_id 的 DB 关联、当前 Attempt ID/generation、item 上保存的 trace_id/root observation_id，以及真实查询中的每个 parent ID。重试用例让第一次 Agent 调用发出一个 span 后返回可重试错误，第二次调用成功；两个 Attempt 的 trace/root/parent 集合分别核验，第一 Attempt 的内部 span 不得进入成功 Attempt 的证据范围。

对同步路径，使用同一 Agent fixture 发起一次有结果的业务调用，专门核验 inject 在 Observation 进入上下文前后的真实影响。若这条入口不能承诺完整执行语义，应将验证限定为当前暴露的调用路径，并在报告中注明；不可把 Worker 路径通过推广为所有入口都通过。

字段矩阵生成阶段按以下步骤执行：

    1. 对每个 Agent profile 分别运行串行、并行、迟到、分页和 HTTP-only 场景。
    2. 保存 Langfuse API 响应中的字段名称、JSON 类型、null/absent 计数，不保存字段值。
    3. 把 trace/root/parent 关联解析成 observation_id 图，记录 parent ID 缺失及孤儿比例。
    4. 检查 started_at/ended_at 的时区、精度、单调性和跨服务偏移；时间字段存在不等于顺序可靠。
    5. 检查 workflow_scope_id 与序列属性是否覆盖所有预期工具 span，是否作用域内唯一且单调。
    6. 对照两种 Agent 同一业务事件，逐字段记录物理命名差异和需由 S4.1 profile 完成的映射。
    7. 输出脱敏 Markdown 矩阵与机器可读汇总；汇总中只留样本数、presence 率、版本和 ID 关系。

S4.0 不为任何字段预设“总是存在”。实际矩阵中“必有、偶缺、缺失、需映射”由重复运行结果决定。trace_id 与 observation_id 是关联验收的硬条件；parent、时间、input/output 或顺序属性缺失时，应说明受影响能力，不能把框架宣称支持 OTel 当作数据可见性证明。

矩阵至少包含以下行；“API 来源键名”和“存在率”留给真实运行生成，不在方案里预写实测结论：

| 规范观察项 | 必须测量的事实 | 若缺失对后续的影响 |
|---|---|---|
| observation_id | 实际 ID key、是否唯一、跨页是否稳定 | 缺 ID 的行不可回链，不能作为 Step |
| trace_id | trace key、是否与 Runner trace_id 一致 | 缺失或不等无法放入当前 Trace scope |
| parent_id | root 与每个子 span 的 parent key 和 nullable 语义 | 缺失时不猜 parent；闭包/能力降级 |
| started_at / ended_at | key、时区、精度、null、结束早于开始的比例 | 缺失只禁用时间判断；不得补值 |
| type / name | 物理键、枚举/自由文本、框架名称差异 | 进入 S4.1 显式 source profile 映射 |
| input / output | 字段是否存在、类型、截断/空值语义 | 决定参数/摘要能力，不保存原值 |
| explicit span links | API 是否保留 OTel link 和目标 ID | 保留时可形成 profile 验证因果边 |
| workflow_scope_id | Agent 是否在业务 OTel 属性中携带可信作用域 | 缺失则不能跨兄弟 span 组合单调序列 |
| workflow sequence | 属性来源、重复/倒退、与实际工具执行关系 | 仅经验证的单调业务序列可作 ordering 输入 |
| phase / status | 请求、执行、阻断及成功/失败/取消的属性键与可信来源 | 缺失则相应工具能力降级，不从名称猜状态 |
| arguments visibility | 合成参数对象完整性、选定路径存在/隐藏/未采集 | 支持 MISSING/REDACTED/AVAILABLE 区分；不保存值 |
| duration_ms | 显式时长字段、单位、类型、有效/缺失频率 | 缺失为 null，timing 降级，不补造耗时 |
| HUMAN event source | 审批来源、事件 ID、scope 与 Observation 的实际关联 | 未验证则 human_events 不完整，不以工具名代替 |

同一行可以同时有 provider 级字段和 Agent OTel 属性；矩阵必须标注每个值由 Langfuse 服务生成、Runner 生成还是 Agent 普通业务 instrumentation 生成。若 API 只返回 parent ID 与时间、不返回 span link/业务序列，矩阵应明确结论为“可展示图结构，但无法证明兄弟调用顺序”。

矩阵报告不把 OTel 属性原值当业务结果存档。测试仅保留属性是否存在、类型、顺序单调性、其对应 observation_id 关系和 profile 版本；workflow_scope_id 使用测试内临时值，序列数只作为统计。源代码中的属性提取映射由 S4.1 profile 验收，不由 S4.0 直接输出 StandardTrajectory。

真实链路中的顺序场景必须包含一个明确串行序列和一个并行分支。串行场景验证两个 Agent 的业务 OTel 顺序属性可映射到相同的 predecessor 关系；并行场景验证两个同父 span 不因 index 或 started_at 被误写为先后。若某个框架未暴露受信序列，矩阵应记录排序能力不足，不通过改名或测试期补造数据伪装支持。

## 验证场景与证据留存

每个场景都运行独立 launch/item/attempt，避免不同测试共享同一个 Trace 或由数据残留凑成通过。场景名使用短随机后缀，清理按本次 project/volume/launch 精确执行。

| 场景 | 真实操作 | 必须保存的检查证据 |
|---|---|---|
| native serial | 原生编排按客户查找、余额读取的业务顺序执行 | 两个工具 Observation、父链 ID、工作流序列关系 |
| Pydantic AI serial | 真实Agent.run通过FunctionModel逐轮请求工具 | 工具返回回流、真实工具spans、业务序列关系、不同物理命名 |
| parallel branch | 同一父节点并行启动两个独立只读工具 | 相同 trace/root、并行 parent、无虚构 happens-before |
| late span | Agent 返回后延迟导出继承 parent context 的 span | 首次查询时刻、出现时刻、轮询次数和 parent ID |
| paginated trace | 在一个 Trace 内产生多于测试页大小的 Observation | 每页 cursor、收集 ID 集合、重复/缺失数量 |
| retry attempt | 第一次响应可重试失败且已发 span，第二次成功 | 两个 attempt/root/trace 分离及成功 Attempt 证据 |
| HTTP-only | 完成正常业务响应但不埋内部 OTel | Runner Root 可查、内部 span 未观察、答案 Eval 成功 |

矩阵报告至少包括：

    - 本地 Langfuse server、Python SDK、Agent framework 和 OTel exporter 的实际版本。
    - 每种 Agent 的样本总数、Span 总数、字段 presence 计数及缺失类别。
    - Runner trace/root/attempt 与 API observation/parent ID 的可复核脱敏关系。
    - 延迟分布的最小值、中位数、P95 和最大值，并区分服务可见延迟与测试轮询间隔。
    - 分页前后唯一 ID 集合、查询耗时和是否因测试上限截断。
    - 因果来源类型，以及哪类顺序证据经真实 API 保留、哪类不可用。

这些报告只支持当前 profile 的实施决策，不是 Langfuse SLA。若字段 presence 取决于 span 类型、Agent 框架或 instrumentation 配置，必须按对应 profile 拆表，不能用合并后的总体百分比掩盖某个 profile 的缺失。

本地启动流程由实现 PR 随测试夹具提交的 Compose 文件定义，测试命令示例：

    S4_TEST_PROJECT="argus-s4-$(python -c 'import uuid; print(uuid.uuid4().hex[:12])')"
    docker compose -p "$S4_TEST_PROJECT" -f tests/fixtures/trajectory_agents/compose.yaml up -d --wait
    python -m pytest -q tests/test_trajectory_observation_integration.py
    docker compose -p "$S4_TEST_PROJECT" -f tests/fixtures/trajectory_agents/compose.yaml down -v

第二条命令仅在 Agent、Langfuse healthcheck 和 Runner test fixture 均通过隔离地址探测后执行。CI 使用相同 Compose 文件与一次性 runner；不挂载宿主机 .env，不传 Cloud secret，不允许测试遇到连接失败时跳过断言或自动改用云端服务。

## RED/GREEN 执行与验收标准

实施先添加 S40 用例并在本地真实 Langfuse 上运行，输出当前 main 的实测基线。若 parent 链或同步路径断言失败，保留失败输出，最小修复后重跑对应用例；若原代码已经通过，则将测试作为回归保护，不为制造 RED 而改代码。此阶段不把测试计划写成已运行证据。

RED 基线命令（需先启动本地隔离 Compose，不访问线上服务）：

    python -m pytest -q tests/test_trajectory_observation_integration.py -k "native_otel or pydantic_ai"

针对实测失败的生产路径修改后：

    python -m pytest -q tests/test_trajectory_observation_integration.py
    ruff check tests/test_trajectory_observation_integration.py services/eval-runner/app/execution.py services/eval-runner/app/worker.py

完成 #127 后应在 CI 增加可复现的本地 Langfuse Job；其 Compose project、凭据、数据库和端口与 Langfuse Cloud E2E 独立。常规测试与本地集成测试分别运行，不能让 CI 通过跳过集成测试来满足 S4.0。

| 验收 ID | 可审查结果 |
|---|---|
| S40-01 | 镜像/依赖预先准备后，测试实际连到隔离本地 Langfuse；运行期间无外网/线上凭据仍可执行；失败时不会访问外部默认 URL |
| S40-02 | Worker 路径中 Runner trace_id 与 Agent spans 相同，remote-agent-http root ID 与 Agent parent ID 关系经真实 API 验证 |
| S40-03 | 原生OTel与Pydantic AI真实Agent.run两组完整运行都产生可查询Observation，真实字段矩阵逐差异说明 |
| S40-04 | 每条链路可从 DB 的 Launch、item_execution、dispatch_generation、Attempt、trace_id、root observation_id 追溯；重试 Attempt 不与另一条 Trace 混合 |
| S40-05 | 矩阵记录九个必测字段及可选因果来源的键名、空/缺失频率、映射和能力影响；报告附本地 Langfuse 与 SDK 版本 |
| S40-06 | 延迟 span 测试先观察到 Root，再在有界轮询内查询到子 span；强制多页查询无丢失、重复或游标环 |
| S40-07 | HTTP-only Agent 在冻结窗口内明确报告 Root-only/无内部 span，业务响应和最终答案评测仍成功 |
| S40-08 | Agent独立环境锁文件与实际distribution闭包无Evaluation SDK；slim无evals/logfire/provider extras，实际无网运行；services/demo-agent及v1 2/6、v2 6/6基线未改 |
| S40-09 | 测试报告与日志不包含 Header、凭据、原始 input/output 或未经脱敏参数 |
| S40-10 | 完成本地回归与 CI 命令，保留失败/通过摘要、矩阵报告和可重复启动说明；此项通过后才允许 #128 冻结 profile |
| S40-11 | 串行与并行场景证明 workflow_scope_id/受信 sequence 或显式 link 的真实可见性；只有明确映射的因果来源可支持 predecessor，parent/time/index 单独不能判 happens-before |
| S40-12 | phase/status、请求→执行关联、参数可见性、duration、HUMAN来源与terminal inventory矩阵完整；terminal先到但前序缺号仍不完整；递增序号不串行化并行；未覆盖能力NONE/PARTIAL、人工正例来源可审核 |

退出条件是 S40-01 至 S40-12 都有本地实测证据，并由维护者接受字段矩阵及异步可见性结果。若 Langfuse API 无法提供必要 parent/root 关联，#127 以阻塞结论交还 #5，不允许 S4.1 按猜测冻结映射。

## 依赖、回退与风险

前置条件为固定版本的本地 Langfuse Compose、Python 3.12、Docker Compose、两种真实 Agent 依赖锁和测试数据库/队列 fixture。不得使用 Cloud E2E deployment 作为唯一链路证据；可将其作为补充，但不能取代 loopback 验收。

若字段缺失或上下文链路错误，只改具体 Runner 边界或 Agent fixture；不借机调整 Demo、追踪产品行为或增加通用 Trace 服务。修正不能通过时间近邻、name 匹配或猜测 parent ID 伪造关联。失败时先回退本阶段新增的最小 Runner 变更，保留测试和报告用于 S4.1 修订；临时卷由唯一 Compose project 名清理。历史 Launch 和 Snapshot 不重写。

实测数字只适用于报告记录的 Langfuse/SDK/Framework 版本。服务升级后重跑矩阵；旧报告不能当作新版本的 API 保证。
