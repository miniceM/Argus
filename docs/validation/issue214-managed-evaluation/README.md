# Issue #214：OTLP → Langfuse 原生托管双 Evaluator 实测

**日期：2026-10-19（Asia/Shanghai）。结论：PASS。** 两次真实 HTTP Agent Launch，各由 Langfuse 原生 Observation Rule 异步生成 LLM Judge 与 Python Code 两类 `source=EVAL` Score。**不是 Argus Score 采纳、Snapshot 或 Quality Gate 已完成**；这些仍属于 #215～#219。

## 1. 范围、环境与证据可信边界

- 父 Issue [#212](https://github.com/miniceM/Argus/issues/212)；本项 [#214](https://github.com/miniceM/Argus/issues/214)。复用 [#213](https://github.com/miniceM/Argus/issues/213) 已验证的原生 Flowise Agentflow V2 / Calculator endpoint，不重造 Agent，也不代表 #213 的全部交付已关闭。
- 实测 Argus 源码：`265c55014b4810d5f481aa67b37f047a658fe3ac`，隔离 SQLite、Runner 进程、端口及 AgentVersion。现有依赖镜像上只读挂载该 main 的源码，**没有重新构建完整生产镜像**；`services/`、协议、数据库、OpenAPI、Console 均零修改。
- Flowise 镜像：`flowiseai/flowise@sha256:cf880b17b1290fbaebebc76c33954bde67b3775aff95feec72a1d060cc81e43d`，复用 3.1.2 实例。真实调用 `POST /api/v1/prediction/8b8d2606-379c-42c4-9ac9-08dcbf9490e8`；没有 Agent Adapter、响应转换或新增 Agent SDK 接入。
- Langfuse：Japan Cloud、v4 observations-first；Cloud **不公开精确服务端 build SHA**，因此不把 SDK 版本冒充服务端版本。实际 Runner 为 Python **3.14.8**、Langfuse SDK **4.17.0**、OTel SDK **1.45.1**；本地质量门禁另使用 Python **3.12.4**、Node **22.22.0**、pnpm **10.5.2**。
- Judge 使用项目已有 LLM Connection `p0-flowise-20261019` / `deepseek-v4.1-flash`；没有读取、变更或发布该连接的 Secret。实际模型输出、usage、Rule / job 身份在证据中可核对。
- 只新增验收脚本、契约测试与本记录；未实现 Judge 引擎、Sandbox、Evaluator Worker/Registry/版本系统、Score Collector、Snapshot/Gate 改造。记录远端 `versionId` 仅用于审计本次实际执行，**不是 Argus 冻结远端实现**；Rule 仍遵循官方 latest 语义。

[证据索引](evidence/index.json) 包含逐文件 SHA-256、体积及脱敏说明。任务内容均为合成算术题；凭据（包含 SDK metadata 内的项目 Public Key）替换为 `[redacted]`。Trace、Observation、Dataset Item、Evaluator、Rule、Job、Score ID 保留以支持精确关联。没有发布本机 `.env`、Cookie、容器环境或模型 Key。

## 2. 真实正例：不是重放出来的新 Agent 执行

任务：`Compute (135 + 287) * 3. You must call calculator.`。Langfuse Dataset Item 的 expected output 为 `{"answer":"1266"}`；HTTP 请求映射只发送 `question`、`streaming=false`，**不向 Agent 发送 Ground Truth**。

| 身份 | Launch A | Launch B（同配置独立再运行） |
|---|---|---|
| Launch | `b8c9e206-d643-4a1d-828b-48e9babb227b` | `7894e45e-9bbf-4cb7-af65-5f5d0b57fb8a` |
| Experiment | `7159593c-85aa-5caa-963e-c6ffbc7877c5` | `dc67f695-d2ce-58eb-8659-2141f55d45e6` |
| Trace | `8bdb2c77e83712ddcb98b34110d22fb3` | `c8dd1b2f3645e514ae39c85c8ffb8f9e` |
| HTTP Observation | `36f88fa0b6dfcf14` | `bb24565c4e5a7c18` |
| Judge Score | `c2904b83-0f19-5c09-8190-7416f3d6a019` | `97a70f7f-fead-50f9-91fa-7eb580076c6a` |
| Code Score | `f28b2bcc-5e28-5030-b70f-f56c88a22978` | `5ac91494-a574-560d-a2df-cba41f016691` |
| 两种评分 | `EVAL / BOOLEAN / true` | `EVAL / BOOLEAN / true` |

共同 Dataset：`cmvf080gu001iad0i4ctjw5l4`；Dataset Item：`5e459f57-c5f5-4cd1-90da-db533d16591e`；HTTP Observation environment：`issue214-20261019`。

实际 HTTP 输出是原生 `{"text":"1266", ...}`，不是人为补的 `answer`：

- `agentFlowExecutedData` 包含模型真实 `calculator` tool call、`tool` message 与 `usedTools.toolOutput="1266"`，不是以最终答案猜测工具执行。
- 两次执行均为 HTTP 200、**1 Attempt**，`max_retries=0`；不同 `chatId`、`executionId` 保留在完整响应中。
- Argus `ExecutionCheckpoint.agent_output` 与实际 HTTP TOOL observation 的解析后 output 完全相等。
- **Langfuse 托管 Python 代码**计算 input/output SHA-256 并写入自身 Score metadata。它们与 checkpoint/HTTP observation 的规范 JSON digest 相等。A/B 的 input digest 相同，output digest 不同（原生响应内的执行身份不同），没有串用旧 Launch 输出。
- A 执行完成为北京时间 **16:40:40.809596**，Code Score 为 **16:40:43.480**，Judge Score 为 **16:40:46.282**；B 分别为 **16:45:35.928813 / 16:45:39.077 / 16:45:41.290**。评分发生在 Agent 完成后，由 Langfuse 队列异步执行。

原始数据：[A checkpoint](evidence/a-checkpoint.json)、[B checkpoint](evidence/b-checkpoint.json)、[A/B receipt](evidence/receipt.json)。独立 HTTP JSON 响应见 [A Judge page](evidence/fresh-api/read-005.json)、[A Code page](evidence/fresh-api/read-006.json)、[B Judge page](evidence/fresh-api/read-011.json)、[B Code page](evidence/fresh-api/read-012.json)。分页 `limit=1` 被真实执行，不是 mock 分页。

## 3. 关联契约与为什么不修改 OTLP 产品代码

```text
Argus launch.id
  └─ langfuse_sync_tasks.dataset_run_name（读取实际值，不凭 Launch 展示名猜）
     └─ self_assigned_run_id(launch_id, run_name) → Experiment.id
Argus Item.dataset_item_id → experimentItemId
Argus Item.trace_id         → Experiment Item.traceId → Score.subject.traceId
Argus Item.observation_id   → Experiment Item.id      → Score.subject.id
                                                    + subject.kind=observation
```

再核对 `experimentDatasetId`、`experimentId`、Evaluator ID、实际 evaluator version ID、Rule ID、job execution ID、Score name、`source=EVAL`、`dataType=BOOLEAN`、真正的布尔值（不是数字 1）及 environment。**同名不代表同一身份，缺失/重复不选 latest、不以 0 补齐。** [A Experiment Item](evidence/a-experiment-items.json) 与 [B Experiment Item](evidence/b-experiment-items.json) 均匹配对应 frozen Dataset Item。

当前同一个 Trace/Observation ID 可在 API 中出现：

1. **真实业务 TOOL**：`name=remote-agent-http`，input 为 `{agent, request}`，output 为原生 HTTP JSON，environment 为 Runner SDK 配置；这是本次 Rule 唯一目标。
2. **Experiment replay SPAN**：`name=argus-run-item-replay`，只有 `langfuse.experiment.*` 关联属性、environment=`default`，IO 为空。重复 metadata replay 后列表甚至可出现不止一条这种 SPAN；不能仅按 ID 取第一条。[fresh pages 1～4](evidence/fresh-api/read-001.json) 保留了这一现象。

实际 TOOL 已足够评分，因此**不补第二个评测 Observation，不把空 replay Span 改造成另一个评分目标，也不改变 Runner / Agent 协议**。选中真实 TOOL 的 input/output，与 checkpoint 对照后才验收；现有 #211 输出消歧回归也通过。

**Ground Truth 限制**：本次使用 Observation-level Rule。托管 Code 实际返回 `experiment_context_present=false`；不能因为 ID 能关联 Experiment，就声称 `ctx.experiment.item_expected_output` 已可用。Judge 映射的是 `input.$.request.question` 与 `output.$.text`；1266 是此烟测的固定 rubric reference，**不证明通用 expected-output 动态映射**。没有额外投影 Ground Truth、隐藏地传给 Agent，或提前改造 #215 评分来源契约。

另一个限制：当前 HTTP Observation 的 environment 来自 Runner SDK 进程的 `LANGFUSE_TRACING_ENVIRONMENT`，不是由每条 Launch 参数自动传播。本次显式对齐二者；不借此宣称跨环境传播问题已修复。

## 4. 托管执行的直接证据

| 配置/运行 | 真实证据 |
|---|---|
| LLM Judge 定义及请求/响应 | [evaluator-llm_as_judge.json](evidence/evaluator-llm_as_judge.json) |
| Python Code 定义及请求/响应 | [evaluator-code.json](evidence/evaluator-code.json) |
| 100% Sampling、environment + name 筛选 | [Judge Rule](evidence/rule-llm_as_judge.json)、[Code Rule](evidence/rule-code.json) |
| 实际被执行的远端 evaluator version | [Judge readback](evidence/phase2-evaluator-llm_as_judge.json)、[Code readback](evidence/phase2-evaluator-code.json) |
| Langfuse 自身执行 observations | [managed-execution-observations.json](evidence/managed-execution-observations.json) |
| Score 逐页实时 API + 精确 join | [fresh-api/verification.json](evidence/fresh-api/verification.json) |

Judge 的内部 observation 有实际模型 prompt、score explanation、model/usage 和 `evaluation_rule_id` / `job_execution_id`，且 `is_test=false`。Code 有 `dispatcher_name=aws-lambda`、`code_eval_runtime=PYTHON`、实际 source code 与执行 output；两者与 Score metadata 的 target Trace/Observation 一致。

验收探针 `scripts/verify_langfuse_managed.py` **只允许 GET 四种公开读接口**，不执行或配置 Evaluator，不创建 Score，也不更新 Argus。受控重放脚本只允许固定 OTLP traces POST。配置操作为官方 Evaluator / Rule API，不调用执行测试按钮或 Langfuse Experiment Runner。

**不要扩大“没有 Score 写入”的说法**：Argus 原有 Builtin 出站同步仍会创建 `source=API` 的 `answer_present` Score，这是既有兼容路径，不是本次两类 `source=EVAL` 的来源。没有新增任何 Argus 侧 Judge / Code Score 提交；[探针请求审计](evidence/request-audit.json) 无 Score 写操作。托管 job、模型调用与 AWS Lambda provenance 才是成功的执行证据，不能只凭“Score 有值”。

## 5. 重复、Rule 未命中与错误输入

以下负例是**明确标记的受控 OTLP 故障注入**，复用已捕获的真实业务 IO；不是额外 Agent 执行，也不计入上面的两个真实 Launch。没有注入 Score。

| 场景 | 实际结果 | 证据 |
|---|---|---|
| 同 Trace/TOOL ID 重放 + metadata replay | A 的两条 Score ID 保持不变，未新增评分；Agent Attempt 仍为 1 | [duplicate-scores-after](evidence/duplicate-scores-after.json)、[phase2 checks](evidence/phase2-checks.json) |
| `name=issue214-rule-miss` 不命中 name filter | EVAL Score 为 0；按真实 UTC 请求审计，观察窗口 **290.974 秒**，同时存在正常处理的负向控制 | [rule-miss](evidence/rule-miss-scores-v3.json)、[wall-clock window](evidence/negative-window-evidence.json) |
| output 没有 `text` | Langfuse Python 执行 ERROR，`ISSUE214_INVALID_OBSERVATION_IO`；该 Code evaluator Score 为 0；Judge 实际仍生成 false，不把它说成“两项都无分” | [真实 Code error](evidence/invalid-code-execution.json)、[scores](evidence/invalid-io-scores-v3.json) |
| output.text=`0`（合法形状、错误答案） | 两个真实 `EVAL / BOOLEAN / false` | [wrong-answer scores](evidence/wrong-answer-scores-v3.json) |
| 两个不同 Launch，同 Dataset Item | 两套不同 Trace/Observation/Experiment 与 4 条不同 Score；各自 join | [receipt](evidence/receipt.json) |
| 同名 Rule，分别绑定 Judge / Code | A/B 各得到一条对应评分，名称不是关联依据 | 两个 Rule 的 name 相同但 ID、assignment 不同 |
| 额外同名 Rule 再绑定同一个 Code evaluator | **确实产生两条 Code Score**，来自不同 Rule。验收器拒绝这一歧义，不能认定上游保证“一个 Item 只有一个有效分数” | [extra Rule](evidence/duplicate-name-extra-rule.json)、[真实重复分数](evidence/duplicate-rule-scores-v3.json) |

因此 #216 后续采纳层必须严格限定 evaluator / rule / subject，并拒绝重复或歧义；**不能只按 score name、最大时间或布尔 true 采纳**。本项没有实现该产品采纳层。

正式验收命名空间的 **3 条 Rule 已暂停**，逐条 PATCH 响应见 `evidence/paused-final-*.json` 和 [汇总](evidence/all-owned-rules-paused.json)。另外两轮预检曾因 API 参数和 SDK environment 不匹配失败，各自的 2 条 Rule 也已暂停，失败预检没有计入成功 Launch；[本次全部 7 条自有 Rule 的最新 GET 回执](evidence/cleanup-all-owned-rules.json) 确认 `enabled=false`。没有改动或重新启用 #213 会话原有 Rule；Evaluator / 合成 Dataset / Trace 保留用于只读复核。

## 6. 可复现操作

### 6.1 立即只读复核（无需重新调用 Agent/模型）

在仓库根目录，安装项目 Python 依赖；在环境中安全提供 `LANGFUSE_BASE_URL`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`，不要开启 shell `set -x` 或将 Key 放进命令行/文档。

```bash
python scripts/verify_langfuse_managed.py \
  docs/validation/issue214-managed-evaluation/evidence/receipt.json \
  --evidence-dir work/issue214-readback --page-size 1
```

实际运行结果为 PASS，13 个独立 API page；所有请求 GET，Score 写入数为 0。脚本严格检查来源、类型、subject、远端 job provenance、代码 IO digest、Experiment Item 关联及重复；任何缺失/错误均非零退出。

当前官方接口细节（已由真实请求及本次下载的 OpenAPI 确认）：

- `GET /api/public/v2/observations?traceId=...&fields=core,basic,io,metadata,trace_context` 返回 IO **原始字符串**。**不要使用 `parseIoAsJson=true`**，当前会返回 400；本地显式 JSON 解析。
- `GET /api/public/v3/scores?source=EVAL&traceId=...&observationId=...&fields=subject,details`；下一页令牌是 **`meta.cursor`**，不是 `nextCursor`。`limit=1` 真分页证据已保存。
- `GET /api/public/experiments` / `/api/public/experiment-items` 必须带 `fromStartTime`。后者再带实际 `experimentId` 核对 Item。
- 本次项目曾真实触发 Cloud **30 requests/minute** 的 429；先暂停自有 Rule，再按 `retryAfterSeconds` 等待。只读脚本保守节流，最多三次读尝试，单次服务端等待上限 60 秒；不无限轮询，不把 429/缺分当作成功。

### 6.2 新跑真实 Agent（会产生模型费用）

1. 先完成 #213 的原生 Flowise Agent/Calculator 部署与独立 AgentVersion 注册。不要改业务 DTO、安装评测 SDK，或把 `expected_output` 映射给 Agent。参考本次 [AgentVersion 请求](evidence/agent-version.json)、[Dataset](evidence/dataset.json) / [Item 请求](evidence/dataset-item.json)，使用自己的新名称与隔离环境；`max_retries=0`。
2. 启动独立 Runner，设置其 **`LANGFUSE_TRACING_ENVIRONMENT`**，并让 Launch 使用相同 environment；选择 `ARGUS_DATASET_SOURCE=langfuse`、`ARGUS_LANGFUSE_RUN_ITEM_STRATEGY=otel`。确认 `/health`、Launch 冻结 `manifest.runner` 及实际挂载源码/依赖身份，不复用旧实例的 build ID 冒充当前 main。
3. 在 Langfuse 官方 UI 或公开 API 创建两个 evaluator：`POST /api/public/v2/evaluators`。复制上面两个 evaluator 证据中的 **request**，更换名称；Judge 的 `modelConfig.provider/model` 必须是自己项目中已配置的真实 LLM Connection。Code 的 source code 在 Langfuse 执行，不能在 Argus 跑它。
4. `POST /api/public/v2/evaluation-rules` 创建两条规则。使用返回的 evaluator ID，`enabled=true`、`sampling=1`，filter 为自己的 environment 与 `name=remote-agent-http`；Code `variableMapping=null`，Judge 继承定义中的两个 JSONPath mapping。新 Rule 必须只作用于本次合成数据。
5. 将 [A Launch request](evidence/a-launch.json) 中的 Agent/Dataset/name/idempotency/environment 换成自己的值，调用 `POST /api/v1/experiment-launches`，随后 `POST /api/v1/experiment-launches/{id}/run`。只运行一次；从 `/items` 取实际 trace/observation/dataset_item IDs。真实 HTTP 调用完成后等待 Langfuse 自身 Worker，不手工创建 Score。
6. 用上述公开 API 查询当前 TOOL、Experiment Item 与 Score。根据实际返回的 IDs、IO digest 和 job metadata 形成新的 receipt，再执行只读验收器。digest 规范是 `sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())`。Code Score 中两项 digest 应与 HTTP checkpoint/实际输出一致。
7. **最终必须暂停本次拥有的 Rule**：`PATCH /api/public/v2/evaluation-rules/{id}`，body 为 `{"enabled":false}`，逐项确认成功。仅暂停自己创建的 Rule，不更改共享连接或其他人的 Rule。

上述 write bodies 均保留于 evidence，返回的 evaluator / rule / experiment ID 必须使用服务端值，不凭名称或旧 IDs 猜测。规则始终使用 Langfuse latest 语义，复跑前检查 evaluator status/model/mapping；不把本次历史 receipt 当作对将来配置的冻结。

### 6.3 重复 / Rule 未命中 / 错误 IO 复现

先在专用测试环境手工启用自己的两条 Rule；默认命令仅 dry-run：

```bash
python docs/validation/issue214-managed-evaluation/controlled_replay.py duplicate \
  --receipt docs/validation/issue214-managed-evaluation/evidence/receipt.json \
  --observations docs/validation/issue214-managed-evaluation/evidence/a-observations.json
```

确认是自己的合成数据后加 `--emit --output work/replay-receipt.json` 才真正发出 **OTLP**。case 可换为 `rule-miss`、`invalid-io`、`wrong-answer`；后三者使用新 Trace/Observation ID，不污染 A/B。工具固定 POST `/api/public/otel/v1/traces`、`x-langfuse-ingestion-version: 4`，不调用任何 Score 写接口。最后查询实际 `source=EVAL` Score 与内部 evaluator execution observations，并暂停自有 Rule。重复同名绑定需要另建自己的第三条 Rule，不能对正常 A/B 的结果静默选 latest。

## 7. 自动化与质量门禁

- RED：先运行缺少验收脚本/重放夹具的测试；另有**实际失败断言**覆盖 v2 原始 IO 字符串和 `meta.cursor` 分页，再修至 GREEN。
- 新增本地契约测试：`test_langfuse_managed_probe.py`、`test_langfuse_managed_observation.py`、`test_langfuse_managed_replay.py`、`test_langfuse_managed_evidence.py`。这些单测使用合成/fake 数据，**不代表真实托管执行**；真实证据只来自第 2～5 节。
- Worker 契约：原生 request/output、不泄露 Ground Truth、同一 Item/Trace/Observation 的 checkpoint/outbox 关联；metadata replay 不是第二个 TOOL；脚本拒绝 API/ANNOTATION、错类型、错 subject、错来源、缺少 job、重复、digest 漂移，验证分页、429 有界只读重试及凭据脱敏。
- S4 独立真实采集：`make trajectory-prepare` 后 `make trajectory-test`：**31 passed**。与本次 Cloud 托管评分分别取证。
- 本地 `make validate` 及相关回归的最终统计见 [tests.json](evidence/tests.json)。测试跳过、deselected 的范围与原因保留，不将它们描述为执行通过。

## 8. 排障与停止线

| 现象 | 先核对 |
|---|---|
| Experiment 有 Item 但无业务 IO | 是否选中了空 replay SPAN，而非同 ID 的 TOOL；不能通过手工补 Score 掩盖 |
| Rule 无评分 | enabled/sampling、真实 TOOL name、**SDK 实际 environment**、evaluator paused reason / model connection；确保是 Rule 创建后新输入 |
| Code ERROR | 查 `environment=langfuse-code-eval` 的真实执行 observation，匹配 `metadata.target_trace_id/target_observation_id/evaluator_id`；不要将 error/missing score 改成 0 |
| Judge 无评分 | 查 `langfuse-llm-as-a-judge`、实际 prompt mapping、模型网络/鉴权及 Worker；不得泄露连接 Key |
| self-hosted 无 job | 检查 Langfuse 官方 Worker、Redis/队列及官方托管 Code runtime 能力；不能在 Argus 自建替代 Worker/Sandbox |
| 429 或短时无分 | 按服务端等待、控制分页频率、设上限；不能反复调用 Agent 重新触发来“恢复”评分 |
| 同名/迟到/多条 Score | 保留所有真实来源，按明确身份拒绝歧义；不以名称、最后时间或任意 true 冒充唯一分数 |

未实测：长期稳定性、并发恢复、跨项目/跨组织采纳、通用 Ground Truth 动态映射与正式 Snapshot/Gate 闭环。本项也不声称普通 CI 的 policy-skipped Cloud job 等于真实 Cloud 托管评分。

官方依据（本次通过 Context7 与实时官方文档/OpenAPI 核验）：[LLM-as-a-Judge](https://langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge)、[Code Evaluators](https://langfuse.com/docs/evaluation/evaluation-methods/code-evaluators)、[Stable Evaluator/Rule API](https://langfuse.com/changelog/2026-08-27-stable-evaluator-api)、[Public API](https://langfuse.com/docs/api-and-data-platform/features/public-api)、[OTel attribute mapping](https://langfuse.com/integrations/native/opentelemetry)、[实时 OpenAPI](https://cloud.langfuse.com/generated/api/openapi.yml)。精确 OpenAPI 下载 SHA-256 见 provenance，而非拿旧 SDK 类型代替当前公开契约。
