# Issue #198：真实模型 Agent E2E 验收记录

日期：2026-10-15（Asia/Shanghai）。方案：`docs/plans/2026-10-15-issue198-real-llm-agent-luna-guide.md`。

## 身份与授权边界

- 分支 `codex/issue198-real-llm-agent`，基于 `origin/main` 的 `73e77172f12d60ea8d5c16704d568dc115f2346f`。
- 功能与修复提交：`d31faa9`（Agent 服务）、`45ddcf7`（Compose/部署治理）、`313ba77`（Runner 集成测试）、`914f52e`（README 文档）、`b22ebc8`（网关会话头修复）。
- 仅本地提交。没有 push、PR、merge、远端配置或 Issue 关闭；Issue #198 保持未关闭。
- 真实 `MODEL_API_KEY` 只注入 `real-llm-agent` 服务容器环境（`.env.local`，不入仓库）；本记录与镜像、日志、Manifest、报告均不含真实 Key。

## 运行环境

| 项 | 值 |
|---|---|
| Cloud 栈 Compose project | `enterprise-agent-eval-poc-cloud`（`docker-compose.cloud.yml` + `docker-compose.real-llm.yml`） |
| 端口 | eval-runner `18080`；demo-agent-v1 `18081`；demo-agent-v2 `18082`；argus-console `18083`；real-llm-agent 仅 `127.0.0.1:18084` |
| Langfuse | 云端（jp 区域，项目 `cmu5a6fj500aoad0jvrckiypb`）；本轮不使用本地 Langfuse 实例 |
| 模型网关 | OpenAI 兼容网关，脱敏 origin `https://<provider>.ai/zen`（`MODEL_BASE_URL` 指向 API 前缀） |
| 模型 | `deepseek-v4.1-flash` |
| 获证镜像 | `enterprise-agent-eval-poc-cloud-real-llm-agent`，image SHA `sha256:e241787e71680d06fb784caa095227cee02f4d3eaebc083bb5e3dbfbc49d0d6f`，healthy |

real-llm-agent 拉起命令（与云栈同 project）：

```bash
docker compose -f docker-compose.cloud.yml -f docker-compose.real-llm.yml \
  --env-file .env.cloud --env-file .env.local up -d --build --no-deps real-llm-agent
curl -fsS http://127.0.0.1:18084/health
```

## 冻结版本（引自 Launch 快照，evidence_state = COMPLETE）

| 对象 | 版本 / 指纹 |
|---|---|
| Dataset | `banking-agent-regression`，version `2026-10-15T08:55:05.523714+00:00`，digest `19e7bbcb…a4629526`，6 条 Items |
| Agent | `real-llm-agent` v1，AgentVersion ID `ee8a2acc-a3cd-41d9-99d3-7c7741112353`，spec_digest `95591108…c103e6` |
| Evaluator | `answer_present@1.0.0`（builtin_python，`app.evaluators:answer_present`），binding `bind_b3d0ef7bca43bd2f`，definition_digest `sha256:7ba48ef5…cf26d5f5`，`FROZEN_VERIFIED / RECORDED / release_eligible=true` |
| 质量策略 | `custom / v1.0`，digest `sha256:51966620…fb077f9`，唯一规则 `answer_present == True (boolean, required)` |
| Runner | `0.2.0`，build `990a42a4dc850df5e1da13bae0119bf9927294c9`，mapping engine `sha256-mapping-engine-v1` |

## 执行记录

### Launch 1（失败，保留为证据，未为其修补后掩盖）

- Launch `d78c126d-4cac-4e4d-b1c3-a1d80a1e7da9`（key `issue198-debug-1`）：6/6 上游失败。
- 根因：OpenCode Go 网关要求 `x-opencode-session` 会话头，缺失返回 400 `MissingSessionID`，错误被统一映射为 HTTP 502 执行失败；未产生假 PASS。
- 修复：`b22ebc8` 将 W3C `traceparent` 的 trace-id 作为 `x-opencode-session` 值透传（缺省时本地生成），并补充 README 会话头说明与 Launch payload 样例中的 `quality_policy.rules` 显式声明（否则 boolean 预期策略会被服务器以 400 `QUALITY_POLICY_OPERATOR_UNKNOWN` 拒绝）。

### Launch 2（成功，正式验收证据）

- Launch `22d697bd-667c-4cba-a234-c14e88969f23`（key `issue198-r2-5609c0f7-8dac-4658-9b65-9b31bbe803c8`），状态 `COMPLETED`。
- Snapshot `adde0ded-66d2-49f1-a8ad-b2d505618afd`（revision 1），manifest_digest `31f56478…03967`，source_result_digest `145521e7…11225d5`，evidence_state `COMPLETE`。
- 汇总：total 6 / evaluated 6 / quality pass 6 / fail 0 / unknown 0；pass_rate 1.0；execution_error 0；p95 latency 36346 ms。

| DatasetItem | Attempt | Trace | Attempt 序号 | execution | evaluation | 质量 | answer_present | Langfuse |
|---|---|---|---|---|---|---|---|---|
| `…0001` | `344dab39…f9ca85` | `9e9192fe…ff29e0` | 第 1 次（无重试） | succeeded | succeeded | pass | True / 1.0 | [Trace](https://jp.cloud.langfuse.com/project/cmu5a6fj500aoad0jvrckiypb/traces/9e9192fea64b24519c182a4e7eff29e0) |
| `…0002` | `07800b24…34225` | `04232a87…eb8a43` | 第 1 次（无重试） | succeeded | succeeded | pass | True / 1.0 | [Trace](https://jp.cloud.langfuse.com/project/cmu5a6fj500aoad0jvrckiypb/traces/04232a879c0abeef65c18fe7c5eb8a43) |
| `…0003` | `89c30fc3…dbd7b` | `059c5fcd…559a63` | 第 1 次（无重试） | succeeded | succeeded | pass | True / 1.0 | [Trace](https://jp.cloud.langfuse.com/project/cmu5a6fj500aoad0jvrckiypb/traces/059c5fcd3c68d05dd362ff12c559a63) |
| `…0004` | `51764a05…970d2` | `53803f2a…b8ee31` | 第 1 次（无重试） | succeeded | succeeded | pass | True / 1.0 | [Trace](https://jp.cloud.langfuse.com/project/cmu5a6fj500aoad0jvrckiypb/traces/53803f2afea5b28871f6c0f010b8ee31) |
| `…0005` | `7a57cec9…ba1da` | `cfc074d7…4b35af` | 第 1 次（无重试） | succeeded | succeeded | pass | True / 1.0 | [Trace](https://jp.cloud.langfuse.com/project/cmu5a6fj500aoad0jvrckiypb/traces/cfc074d7e7c2b95c1e8aa330d34b35af) |
| `…0006` | `213cbf96…bdeb1` | `ae8c7549…269f79` | 第 1 次（无重试） | succeeded | succeeded | pass | True / 1.0 | [Trace](https://jp.cloud.langfuse.com/project/cmu5a6fj500aoad0jvrckiypb/traces/ae8c754913e7868984363f92fd269f79) |

所有 Attempt 均为首次成功，无任何重试行为；Item → Attempt → Trace 一一映射。

## 成本 / Usage 状态（token 口径）

- 本轮成本计量口径按用户指定为 **消耗 token 数**（不用货币金额表达）。
- 人工核查到的单次真实调用消耗：`prompt_tokens=35, completion_tokens=15, total_tokens=50` → 单次成本 50 tokens。
- 该 50 tokens 是对一次调用的人工网关侧核查值（含 reasoning 13 tokens）；其余调用未逐条核查，6 次调用的平台侧总消耗不可重建（Attempt / Item 记录无 usage 字段，`usage_cost=null`），不得把 50 套算为 6 条总数（即不编造 300 tokens）。
- 系统侧口径保持如实记录：汇总 `cost_total = null`、`cost_partial = true`、`cost_unavailable_reason = INCOMPLETE_ATTEMPT_COST`；如需把 token 数转为成本金额，需在 Launch 配置中补充 `amount`/`currency` 路径。
- 已知投影行为：`item.scores` 为空对象（boolean 投影已知行为），逐项结论以 `evaluation_results` 为准。

## Langfuse 同步（有界等待后的最终状态）

- `item_trace`：6/6 任务成功，`SYNCED`。
- `run_score`：1/1 任务成功，`SYNCED`。
- `overall = SYNCED`；6 条 Trace URL 上表逐条可打开，对应同一 Launch/Item。

## 验收结论（AC1–AC10）

| 标准 | 结论 | 主要证据 |
|---|---|---|
| AC1 默认隔离 | PASS | `docker-compose.real-llm.yml` 仅在显式 `-f` 与三个 `MODEL_*` 变量齐备时启用；`test_real_llm_deployment.py` 断言 base compose 不含 real-llm-agent；`make demo` 基线不受影响 |
| AC2 真实模型 | PASS | Launch2 六条均为真实非空回答；无 mock / fallback 开关；`real-llm-agent/requirements.txt` 无评测 SDK，`make validate` 第 3 步覆盖 real-llm-agent zero-SDK 检查；traceparent 传播由 Agent/Runner 集成测试锁定 |
| AC3 完整执行 | PASS | AgentVersion `ee8a2acc…`（不可变）；Runner 经 Compose DNS 访问；6 条 Dataset Items 冻结；Attempt→Trace 全部可查且无重试 |
| AC4 基本评分 | PASS | Manifest 只含 `answer_present@1.0.0`；6/6 boolean True；文案与本报告不称 100% 业务准确率 |
| AC5 可观测闭环 | PASS | Console 可见真实输出 / Item / Attempt / 逐项评分；Langfuse Experiment/Trace/Score 可打开并与同一 Launch/Item 对应；item_trace 与 run_score 均 `SYNCED` |
| AC6 真实调用证据 | PASS（有标注） | 消耗口径为 token：单次核查 35/15/50 → 50 tokens（见成本/Usage 小节）；Attempt 层 usage 明确标注 unavailable；provider_request_id 未实现，属可增强项 |
| AC7 失败区分 | PASS（自动） | 401→`MODEL_AUTH_FAILED` / 502→`MODEL_UNREACHABLE` / 504→`MODEL_TIMEOUT` 映射由自动化测试锁定；真实 Provider 故障专项未执行；以真实 400 `MissingSessionID`→502 佐证"上游错误统一映射 502"路径 |
| AC8 安全 | PASS | 真实 Key 只注入服务容器；`test_provider_error_never_leaks_key_or_body`、`test_missing_required_config_rejects_startup_without_leak`、`test_invoke_logs_do_not_contain_session_id` 锁定日志脱敏；build context 与启动命令不携带 Key |
| AC9 自动化门禁 | PASS | 下表命令记录 |
| AC10 交付完整 | PASS | README、`.env.real-llm.example`、可选 Compose、注册/Launch 示例齐备；本报告为脱敏真实 E2E 记录；本轮提交无任务外内容 |

## 自动化记录

执行环境：本地 Python 3.12（仓库 venv）、Node 24.13.0、pnpm 10.5.2。

| 命令 | 结果 |
|---|---|
| `pytest tests/test_real_llm_agent.py tests/test_real_llm_agent_integration.py tests/test_real_llm_deployment.py` | **60 passed** |
| `ruff check`（目标范围） | PASS |
| `pip check` | 无损坏依赖 |
| `make validate`（8 步全链） | PASS（exit 0）：步骤 1–5 校验、Console typecheck / vitest / build、后端全量 **1325 passed / 7 skipped**（`TEST_POSTGRES_URL` 未配置的显式跳过，非遗漏）、Compose config OK |
| Docker `up -d --build --no-deps real-llm-agent`（云栈同 project） | PASS（healthy） |

RED 留痕例外说明：S1–S5 各提交将实现与测试合并落库，未单独保留"先 RED 后 GREEN"的失败运行记录，与 TDD 流程建议不符，已如实记录；无删除断言、放宽验收或跳过测试行为。

## 未运行 / 局限

- `make trajectory-test`（S4 真实采集专项）：本轮任务不涉及 S4 观测链路，未执行；`make validate` 已显式提示该专项需另行运行。
- 真实 Provider 专项故障演练（401/504）：未执行，属方案 §S5 建议的额外专项；AC7 目前以自动化测试 + 真实 400 场景佐证，不是完整故障实测。
- 未使用本地 Langfuse 完整栈（按本轮约束，Langfuse 走云端 jp 区域）。

## 后续（非本轮范围）

- push / PR / merge / Issue 关闭需单独授权。
- Attempt 层 usage/cost 可在后续迭代声明 `amount` / `currency` 路径，消除 `INCOMPLETE_ATTEMPT_COST`。
