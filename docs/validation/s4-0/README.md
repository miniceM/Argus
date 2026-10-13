# S4.0 本地真实观测验收记录

核验日期：2026-10-13。方案基线：main@fd1e13333b2c50bf48dbe5ed310e5c0b2fdfb73f（PR #136）。关联 [#5](https://github.com/miniceM/Argus/issues/5)、[#127](https://github.com/miniceM/Argus/issues/127)、[#133](https://github.com/miniceM/Argus/pull/133)。

本记录是 T0 的实现与本地证据。S4 整体仍在实施，CI 和维护者验收状态以实施 PR 为准。此阶段不引入 Trajectory Schema、Assembler、轨迹规则、迁移或 Console 功能。

## 结论与修复

原生 OTel 和 Pydantic AI 的业务 spans 经实际 OTLP 写入隔离 Langfuse，再从部署实例的 v2 Observation API 查询。两个框架都执行客户查找、余额读取；Pydantic AI 使用真实 `Agent.run`、注册工具和 FunctionModel 三轮执行，已断言第一工具返回后才请求第二工具。并行场景由同一框架真实执行两个工具。

Worker 的父链验证通过。同步 `_execute_single_item` 的初始真实回归失败：业务 span 与 HTTP span 处于同一个 Trace，但其父 ID 指向外层 envelope。进入 `remote-agent-http` 上下文后重新 `inject(headers)` 修复了直接父节点；修复后的同一真实断言通过。同步测试验证当前暴露的单用例调用路径，不把它推广成 Worker 的重试归属保证。

HTTP-only Agent 没有内部 OTel 依赖调用；本次采集窗口只观察到 Runner 外层 chain 和 HTTP Observation。业务响应与冻结的 `intent_match@1.0.0` 评测通过。该结论不证明任意远端业务绝对没有隐藏动作。

## 版本、隔离与复现

| 制品 | 实测/固定版本 |
|---|---|
| Langfuse web / worker | 4.38.0，Compose 固定镜像 digest |
| Runner Langfuse Python SDK | 4.16.0 |
| 业务框架 | pydantic-ai-slim 2.55.0，未添加 extras |
| OTel SDK / HTTP exporter | 1.45.1 |
| Agent Python | 3.12.14，独立 venv |
| 本地质量门禁 | Node 22.23.3、pnpm 10.5.2 |

业务 Agent 的实际安装闭包共 31 个 distribution（含 venv 引导的 pip），没有 Langfuse、Pydantic Evals、LangSmith、Argus Eval SDK 或 logfire SDK。`logfire-api` 是框架附带的 API shim；Pydantic 的 OTel metadata 中出现 `logfire.*` 键不等于安装了 logfire SDK。

在启动或拉取镜像前核验 Docker endpoint 为本机 Unix socket，拒绝 SSH/TCP 和远端 context。夹具创建随机命名的 Compose project、临时卷、初始化项目和凭据；只有 Web 发布随机 `127.0.0.1` 端口，数据库及其它服务不发布宿主机端口。桥接网络关闭出站 masquerade；Agent 进程只继承夹具配置，socket connect/connect_ex 与 getaddrinfo 拒绝非 numeric loopback 目标。SDK、HTTP 与 OTLP 禁用环境代理；退出后恢复隔离的环境设置。未加载开发者 `.env`，未使用 Cloud 密钥或本机业务 PostgreSQL。清理逐项执行，审批 readiness 失败也会关闭子进程。真实测试结束后，所创建的容器、网络和卷已清理。

先按贡献指南准备 Runner Python 3.12 环境与 Console 依赖，再执行：

```bash
make trajectory-prepare
make trajectory-test
make validate
```

准备阶段下载固定制品；测试阶段 `--pull never`，不回退到线上服务。若需要固定报告目录，在一个新建的、仅用于本次测试的目录执行：

```bash
mkdir -p artifacts
python -m pytest -q tests/test_trajectory_observation_integration.py -m trajectory_local \
  --basetemp=artifacts/s4-local --junitxml=artifacts/s4-junit.xml --tb=short
python tests/fixtures/trajectory_agents/summarize.py \
  artifacts/s4-local/s400/field-matrix.json docs/validation/s4-0/field-matrix.json
```

pytest 会重建 `--basetemp`，不要指向用户数据目录。完整元数据矩阵由 CI 作为 artifact 留存；提交的 [机器可读摘要](field-matrix.json) 包含版本、计数、类型、ID 父链及原矩阵哈希，不包含 Header、凭据或原始 input/output。

## 实际 API 与字段矩阵

已从本地 `/api/openapi.yaml` 核对 `/api/public/v2/observations`。查询显式使用 `traceId`、`fields=core,basic,time,io,metadata`、`expandMetadata`、`limit`、`cursor`；下一页位于 `meta.cursor`，不是 `nextCursor`。

`input/output` 返回字符串或 null；`parseIoAsJson=true` 已在该版本契约中禁用。业务 metadata 为平铺键，例如 `attributes.workflow.phase`；JSON 数组属性经该版 API 返回为 list。来源映射须按此实例的真实形状处理，不能直接套用嵌套 `metadata.attributes` 的文档示例。

| Profile | Case 数 | Observation 数 | 9 个基础字段缺 key 数 | null parent/input/output |
|---|---:|---:|---:|---|
| native_otel | 18 | 154 | 0 | 18 / 84 / 100 |
| pydantic_ai | 11 | 157 | 0 | 11 / 50 / 59 |
| http_only | 1 | 2 | 0 | 1 / 0 / 1 |

九字段是 `id`、`traceId`、`parentObservationId`、`startTime`、`endTime`、`type`、`name`、`input`、`output`。身份、时间、type/name 在当前样本中非 null；parent 的 null 对应物理 Trace 根。矩阵还区分空字符串、空对象、空数组及字符串编码的空对象；完整报告按 span 类别和 phase 拆分计数。基础字段存在不等于参数能力完整。

| 业务语义 | 实际物理来源 | 验证与后续约束 |
|---|---|---|
| 工具 identity | `attributes.gen_ai.tool.name` | 原生 `crm_lookup/ledger_read`，Pydantic `account_crm_lookup/account_ledger_read`；T1 显式映射 |
| 请求/执行关联 | `attributes.workflow.call_id` | REQUESTED 与 EXECUTED/BLOCKED 同一业务调用 ID |
| phase/status | `attributes.workflow.phase/status` | 已覆盖 REQUESTED、EXECUTED、BLOCKED 与 SUCCEEDED、FAILED、CANCELLED |
| 参数可见性 | `attributes.workflow.arguments_visibility`、`attributes.workflow.hidden_paths`、input | 完整对象、缺路径、策略隐藏、未采集分别实测；不把 null 自动解释为缺字段 |
| 显式耗时 | `attributes.workflow.duration_ms` | 业务计时，数值且非负；来源缺失时不补造 |
| scope / sequence | `attributes.workflow.scope_id/sequence` | scope 内序列唯一；序号用于覆盖检查，不串行化并行分支 |
| 前置关系 | `attributes.workflow.predecessor_event_ids` | 串行来自已完成工具；并行请求的 predecessor 列表为空 |
| OTel Link | 未在查询字段中观察到 link 对象 | 父边和时间不能替代兄弟步骤因果关系 |
| 终结 inventory | `attributes.workflow.event_ids/event_count/final_sequence` | terminal 先到而前序缺号时仍不完整；到齐后才通过覆盖断言 |
| HUMAN 来源 | `attributes.workflow.source/event_id`、`resourceAttributes.service.name` | 独立合成审批进程产生事件 ID；来源 scope 与 terminal 一致 |
| HUMAN 零事件覆盖 | `attributes.approval.event_count/scope_closed` | 审批系统关闭本 workflow 后返回零事件；普通 `escalate` 工具不成为 HUMAN |

HUMAN 正例只证明本夹具中独立审批来源可审核，不声称真实人员审批，也不证明任意不可信 Agent 上报的同名属性可信。生产 profile 必须有获准来源及其验证契约；未验证的人工能力保持 NONE/PARTIAL。

## 延迟、分页与 Attempt

以下延迟从业务返回并 flush Runner 后开始轮询计算，轮询间隔 300 ms。它包含服务可见性和轮询开销，不能作为 Langfuse SLA。

| Profile | 最小/中位/P95/最大 ms |
|---|---|
| native_otel | 387 / 718 / 8062 / 8062 |
| pydantic_ai | 429 / 443 / 1231 / 1231 |
| http_only | 656 / 656 / 656 / 656 |

原生分布包含两项主动延迟导出 8 秒的场景，不能将其 P95 解释为常规 ingestion 延迟。late_all 先观察到 Runner-only，再观察到同一 Trace、同一 parent 的 Agent spans；terminal_first 先观察到终结事件和缺失 inventory，再观察到补齐。T1 的 quiet window/deadline 仍需 acquisition 回归校准，不能只凭首次查询判完整。

每个非 Root-only Case 请求页大小 2，逐页断言实际返回条数不超过 2，记录页数与游标跳数；当前最少续页 3 次。分页唯一 ID 集合和 limit=1000 的完整查询集合相同，未丢失、重复或出现游标环。串行前置 event ID 映射到同 scope 中成功完成的工具执行，报告保存脱敏关系 ID；并行不造顺序。重试实际返回 503、200，两个 Attempt 在 DB 中可追溯；trace/root 与 Observation ID 集合分别核验且不相交。旧 Attempt 的 spans 不归入成功 Attempt。

## 验收状态

| 验收 | 本地证据 |
|---|---|
| S40-01 | 隔离本地服务、预装制品、无 Cloud fallback；真实进程运行 |
| S40-02 | Worker HTTP root → Agent 根 span 逐边父 ID 断言 |
| S40-03 | 两框架真实调用，串行各重复 3 次，独立不可变 AgentVersion |
| S40-04 | DB Launch/Item/Attempt/generation 与 Trace/root 关联；真实 503→200 重试 |
| S40-05 | 九字段、metadata、类型/null/缺失与 span 类别矩阵；版本与 ID 边 |
| S40-06 | 两类迟到场景；强制分页与完整查询集合一致 |
| S40-07 | HTTP-only Runner 根观测与答案评测通过 |
| S40-08 | 安装闭包审计；独立进程、无外部模型；Demo 基线回归通过 |
| S40-09 | 报告只保留字段元数据/计数/关系；不保存原文及凭据 |
| S40-10 | 本地真实门禁通过；新增独立 CI Job，远端结果见实施 PR |
| S40-11 | 串行成功完成→后续请求；并行无 sibling predecessor；缺 OTel links 明示 |
| S40-12 | phase/status、参数、duration、审批正/负例、terminal inventory 实测 |

最新提交树的两层真实门禁：31 passed，0 skipped，94.656 秒；其中业务链路 29 项、独立 SDK/API 2 项。业务链路包含 30 份 Case 报告（503→200 重试分别保存两个 Attempt）。代理隔离、清理、网络拒绝的 11 项回归，以及同步父链的 2 项回归通过。`make validate`：Python 795 passed、Console 847 passed，Token lint/typecheck/build/契约同步/Compose 通过。4 项既有 PostgreSQL 专项在本地因 `TEST_POSTGRES_URL` 未配置跳过；不计为本地通过，远端 CI 的 PostgreSQL 检查单独核验。

遗留的 datetime/Starlette 弃用警告和前端 bundle 体积提示保留，不属于本次观测修复。此记录不声称 S4 整体完成，也不关闭 #5 或后续阶段 Issue。
