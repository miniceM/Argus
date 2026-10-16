# Issue #198 后续方案定稿：Token 口径消耗 + Langfuse Run-Item 修复

> 设计会话：2026-10-15（Asia/Shanghai）协同定稿；实施：2026-10-16。
> 前置：`docs/plans/2026-10-15-issue198-real-llm-agent-luna-guide.md`（S1–S5 已完成并验收，
> 见 `docs/validation/issue198-real-llm-e2e.md`）。本方案是该验收遗留的两个产品口径问题的修复计划。
> 本文按 Luna 实施规范编写：目标 / 决策口径 / 事实证据 / 原子提交 / 测试 / 验收，均给出可复现的文件与命令。

## 1. 目标与用户确认口径（一票定案）

1. **Token 总数口径包含 reasoning**：单次调用取 `total_tokens`（例如 `prompt=35 / completion=15 / total=50`，50 已含 reasoning 13），不做货币换算。
2. **平均分母 = `total_cases`**：失败用例与重试 Attempt 的 token 全部计入分子；配 `usage_unavailable_reason` 兜底（fail-closed，不写 0、不编造证据）。
3. **Money 口径彻底退出展示层**：KPI 卡片不再展示金额；API money 字段保留兼容历史快照，不在 UI 渲染。
4. **方案 ② A + B 都做**：A = compose 云栈默认 dataset source 改回 `langfuse`；B = 对历史 run 一次性补绑 `dataset_run_items`。
5. **方案 ③ 采用推荐**：Argus 侧将 token 作为 numeric Score 上报 Langfuse（item 级 `tokens_total` + run 级 `run_tokens_total`），不改 Langfuse 上游 `Total Cost ($)` 列的产品语义。

## 2. 事实与证据（实施依据）

1. **KPI 卡片空值根因**：`services/console/src/features/launches/LaunchKpiOverview.tsx` 第 4 张卡读取 `cost_per_case` money 字段；real-llm-agent v1 注册时未配 `usage_cost_mapping` → `cost_answer = null` → 显示 "—"。Runner 的 token 采集链路完整（`services/eval-runner/app/costs.py` `extract_usage_cost` → Attempt 成本 → `aggregate_attempt_costs` case 级 usage 聚合，usage 独立于 amount 校验），无需改 Agent 或 Runner 调用协议，只需注册 AgentVersion v2 时配 token 三路径（不配 amount / currency）。
2. **Langfuse run-item 缺失根因**：`docker-compose.cloud.yml:56` `ARGUS_DATASET_SOURCE:-seed` 是 2026-10-13 4591d28（PR #139，S4.0）引入 → 版本化 Launch 路径（无 `dataset_client` 时 `DatasetResolver` 读 env，`dataset.py:61-68`）冻结 `dataset.source=seed` → `langfuse_sync.py:663` 显式跳过 `dataset_run_items.create` → 云端 run 有 URL 但无 run-item，详情 404。
3. **CI itemCount=6 与 seed 默认不冲突**：CI `scripts/ci-e2e-cloud.sh` 走 legacy `/experiments/run`（`main.py:395-421`）显式传 `dataset_client` → 命中同步路径 `run_experiment`（`execution.py:536`），run-item 由 SDK 创建，**不读 `ARGUS_DATASET_SOURCE`**。因此把 compose 默认改回 `langfuse` 不影响 CI（回归证据：CI experiments itemCount 保持 6）且修复版本化 Launch。
4. **Langfuse 云端能力边界（v4.59 jp）**：`GET /api/public/datasets/{id}/runs`、`/api/public/traces`、`/api/public/dataset-run-items` 均 410 LEGACY；可用端点：`GET /api/public/experiments`（分页，含 itemCount/totalCost/totalTokens）、`POST lf.api.dataset_run_items.create(run_name, dataset_item_id, trace_id, observation_id, dataset_version)`。后端补绑复用 runner 同一调用路径，免费可行。
5. **Langfuse `Total Cost ($)` 列**：上游产品内置列，取值来自观测级 usage 或 ingestion cost；当前所有 trace 无观测级 usage，列为 "not recorded"。不能改名换语义，token 呈现走 Score（决策 ③）。

## 3. 实施分节（原子提交）

### S6.1 — compose 云栈默认 dataset source 改回 `langfuse`

- 文件：`docker-compose.cloud.yml`（第 56 行 `ARGUS_DATASET_SOURCE: ${ARGUS_DATASET_SOURCE:-seed}` → 默认 `langfuse`）。
- 反回归测试：在 `tests/test_real_llm_deployment.py` 增加 compose 断言 — 默认缺省值必须是 `langfuse`（覆盖 URL 之外，防止历史 seed 默认回归）。
- `make validate` 第 8 步 Compose config 校验保持通过。

### S6.2 — 一次性运维补绑脚本 `scripts/backfill_langfuse_run_items.py`

- 约束：凭据运行时通过 `.env.cloud` 注入（脚本内加载，不读取/不打印内容，不入库不入仓）；只对显式传入的 Launch ID 生效，不加通配符兜底。
- 逻辑：每个 launch 从 Runner HTTP API 取 item ↔ trace 归属，逐条调用与 worker 路径相同的 `lf.api.dataset_run_items.create(run_name=…, dataset_item_id=…, trace_id=…, observation_id=…, dataset_version=…)`；重复绑定的报错按 Skip 记录，不当作失败。
- 执行边界：**运行前需要用户显式批准该命令**（云端写操作 + 使用云端凭据）。首个执行目标为三条历史 launch 的 run 补绑；验收 = `GET /api/public/experiments`（fromStartTime 分页）显示三个 run 的 itemCount 达到对应 dataset item 数，Langfuse UI 可打开。验收证据写入 `docs/validation/issue198-token-usage-langfuse-runs.md`。
- 脚本设计 / dry-run 逻辑有单测覆盖（伪造 runner payload + 捕获 `dataset_run_items.create` 调用），真实云端执行不做单测。

### S6.3 — Runner summary token 聚合字段（RED → GREEN）

文件：`services/eval-runner/app/aggregation.py`（run 级聚合），新增字段：

| 字段 | 语义 |
|---|---|
| `usage_tokens_per_case` | 单用例平均 token 消耗 = `sum(case_total_tokens) / total_cases`（分母按用户定稿为 total_cases；失败/重试全计入） |
| `usage_input_tokens_per_case` / `usage_output_tokens_per_case` | 分解口径，同分母 |
| `usage_case_count` | 有完整 case 级 token 证据的 case 数 |
| `usage_coverage` | `usage_case_count / total_cases` |
| `usage_partial` | boolean：total>0 且 coverage < 1.0 |
| `usage_unavailable_reason` | Null 安全兜底：复用 `INCOMPLETE_ATTEMPT_COST` / `COST_NOT_RECORDED` 词汇，不造新枚举 |

- 统计约束：case 级 usage 取 `aggregate_attempt_costs` 的 `usage` 块（多 attempt 自动求和；`total_tokens` 不覆盖自算口径）。`usage_tokens_per_case` = 所有有证据 case 的 `total_tokens` 总和除以 total_cases（不是除以 usage_case_count）。
- 契约同步：`scripts/export_openapi.py` → `docs/openapi.json` → `pnpm --dir services/console api:generate` → `services/console/src/api/schema.d.ts`。
- 单测：`tests/test_aggregation_usage.py` 覆盖（分母=total_cases、重试追加、失败例分母不加探针、coverage/reason 兜底）。

### S6.4 — AgentVersion v2 注册（运行时操作）

- 约束：真实环境 runtime 操作，需要用户提供 Langfuse/网关凭据的环境上下文后执行；命令呈现前方能动手。
- Payload：在现有 real-llm-agent v1 注册体上追加 `usage_cost_mapping`（`input_tokens_path` / `output_tokens_path` / `total_tokens_path` 三路径；**不配** amount/currency 路径），创建 v2 不可变快照。
- README 指引同步（`services/demo-agent` 对应样例文档，如涉及）。

### S6.5 — Console KPI 卡片 token 主口径（RED → GREEN）

- 文件：`services/console/src/features/launches/LaunchKpiOverview.tsx`。
- 第 4 张卡主值切换为 `usage_tokens_per_case`（title「单用例平均 token 消耗」），subtext 显示 input / output 分解；coverage < 1.0 时透出 `usage_unavailable_reason` 语义化提示；money 文案（`cost_per_case` / `formatMoney` / 汇率字样）不再出现在该卡与对比子文本。
- API money 字段保留在类型的兼容位置（不改 schema 不删字段），只是 UI 不消费。
- 交互 / 无障碍遵循既有卡片结构；不写裸十六进制色值，遵守 `argus-design-system` token 规则（`pnpm --dir services/console lint:tokens` 必须通过）。
- Vitest：为 token 卡新增/更新 snapshot 或交互断言（含 coverage=0 时文案）。

### S6.6 — Langfuse token Scores（item `tokens_total` + run `run_tokens_total`）（RED → GREEN）

- 文件：主要在 `services/eval-runner/app/langfuse_run_scores.py`（及必要的 worker / executor 侧 item score 组装点）。
- 语义：独立的平台 metric Score，不复用 evaluator 投影语义；score id 固定为 `tokens_total`（item 级，取 case 总 token 含全部 attempt 与 reasoning）与 `run_tokens_total`（run 级，含 launch 全部 case token）；score 侧幂等以稳定 score id 保证。
- 证据不完美时 fail-closed：无 token 证据 → 不上报 Score（不写 0、不编造），该情况的解释由 `usage_unavailable_reason` 在 Argus summary 承担。
- 单测：`tests/test_langfuse_run_scores.py`（或同目录既有 Scores 测试文件）覆盖 item/run 两级 Score 组装与空证据跳过。

### S6.7 — 文档 / 验证记录 + `make validate`

- 更新 `docs/validation/issue198-real-llm-e2e.md` 或新增 `docs/validation/issue198-token-usage-langfuse-runs.md` 记录本轮验证证据。
- 命令验收链：`make validate`（前端 vitest + 后端 pytest + openapi/console schema 同步 + compose config）+ Console `pnpm --dir services/console lint:tokens`。
- 真实 token 证据（S6.4+S6.6+S6.3 走一次真实付费 launch）acquired 后记录样本数据（total_tokens per item、run 汇总、Langfuse run 页面 itemCount、Console 卡片截图），并回填验证记录。

## 4. 运行时授权点（明确停等）

1. S6.2 补绑脚本的真实云端执行（写入云端 Langfuse）— 用户批准命令后执行。
2. S6.4 AgentVersion v2 注册（真实注册操作）— 用户批准 payload 后执行。
3. S6.7 至少一次真实付费 launch 验收 — 用户批准启动命令与成本预算后执行。

以上三项各自单独报价命令；不经授权不自动触发。

## 5. 风险与护栏

- 只有 S6.1 的 compose 默认值影响随后的云栈启动，需配那个 Reflects 的 `ARGUS_DATASET_SOURCE:-langfuse`（覆盖 default `seed` 残留），且 CI legacy 路径不改（由 CI 另一端回归证据锁定）。
- S6.3 的 `usage_tokens_per_case` 语义边界：分母固定 `total_cases`（用户定稿），不做 quality comparable cohort 修饰；失败/重试都算入分子。
- S6.5 的 money 字段仅降权展示，不删 API 兼容字段（避免破坏历史快照消费者）。
- S6.6 的 score id 命名空间与业务 evaluator（如 `overall_pass`）隔离，platform 侧 scorer 失败不阻塞质量结论。

## 6. 完成标准（PASS/FAIL 封口）

| 项 | PASS 证据 |
|---|---|
| 组合表（ commitment） | 本计划文与用户确认决策一致 |
| S6.1 | compose diff 默认 langfuse，回归/新增断言覆盖，`make validate` 通过 |
| S6.3 | summary 字段单测通过（分母=total_cases），schema 双侧同步 |
| S6.4 | AgentVersion v2 注册成功并冻结 usage_cost_mapping |
| S6.5 | Console 卡片 token 主值 + 次级信息，vitest / lint:tokens 通过 |
| S6.6 | item `tokens_total` / run `run_tokens_total` Score 上报，Langfuse 云端可查 |
| S6.7 | 验证记录含真实 launch 的 token 证据与 Langfuse experiment itemCount |
