# Issue #198 后续：Token 口径消耗 + Langfuse Run-Item 修复验证记录

日期：2026-10-16（Asia/Shanghai）。方案：`docs/plans/2026-10-15-issue198-token-usage-langfuse-runs-luna-guide.md`。

## 身份与授权边界

- 分支 `codex/issue198-real-llm-agent`，本工作树 `/Users/hosea/.codex/worktrees/1a39/Argus`。
- 本轮提交（仅本地，未 push）：
  - `4f7efd8` fix(deploy): default cloud dataset source to langfuse for run items（S6.1）
  - `fca0912` feat(runner): add token usage aggregation with total-case denominator（S6.3）
  - `f091779` feat(console): show token consumption card instead of money kpi（S6.5）
  - `e4fe5ce` chore(lint): fix import order in aggregation usage test
  - `8126d05` feat(runner): publish langfuse token usage scores for items and runs（S6.6）
  - `a963240` feat(ops): add langfuse run item backfill script（S6.2 本地）
- 无 push / PR / merge / 远端配置变更；真实云端写操作与付费 launch 全部等待用户单独授权。

## 用户定稿口径（不再变更）

1. token 总数含 reasoning（`total_tokens`，如 prompt=35 / completion=15 / total=50），不做货币换算。
2. 平均分母 = `total_cases`；失败用例与重试 attempt 的 token 全部计入分子；`usage_unavailable_reason` 兜底，绝不写 0。
3. money 退出 KPI 展示层（API 字段保留兼容）；GateComparisonTab / ComparisonReport 既有 money 文案不在本轮范围。
4. Langfuse token 走 numeric Score：item 级 `tokens_total` + run 级 `argus_run_tokens_total`（独立平台 metric，稳定 score id 幂等，无证据不上报）。

## S6.1 — compose 云栈默认 dataset source（本地完成）

- `docker-compose.cloud.yml` 默认 `ARGUS_DATASET_SOURCE:-langfuse`。
- 回归测试：`tests/test_real_llm_deployment.py::test_cloud_compose_runner_dataset_source_defaults_to_langfuse`（RED → GREEN）。
- 证据：compose 默认值变更后 CI legacy `/experiments/run` 路径不受影响（该路径显式传 `dataset_client`，不读该环境变量）。

## S6.3 — token 聚合（本地完成）

- `services/eval-runner/app/aggregation.py`：`_usage_summary` 新增分母=total_cases 的聚合与 `usage_tokens_total`；`app/models.py` 新增 7 个 typed usage 字段。
- 单测：`tests/test_aggregation_usage.py`（7 个用例：分母=total_cases、重试追加、失败例入分母、coverage/reason 兜底、usage_tokens_total fail-closed）。
- 契约同步：`scripts/export_openapi.py` → `docs/openapi.json` → `pnpm --dir services/console api:generate` → `services/console/src/api/schema.d.ts`，快照 diff 为空。

## S6.5 — Console KPI 卡片（本地完成）

- `services/console/src/features/launches/LaunchKpiOverview.tsx` 第 4 卡改为「单用例平均 token 消耗」（testid `usage-tokens-per-case` / `usage-tokens-breakdown`），money 展示移除，cohort 不回退全量，fail-closed 语义化提示。
- Vitest：`LaunchKpiOverview` 22/22；受影响的 5 个测试文件（含 snapshot、e2e 断言）194/194；`pnpm typecheck`、`pnpm lint:tokens`、`pnpm build` 通过。
- 注意：注释中不得出现 `#数字` 形态（token-lint 会误判色值），Issue 引用写作 `Issue 198`。

## S6.6 — Langfuse token Scores（本地完成）

- item 级：worker finalize（`app/worker.py` Phase 4）把 case 级 usage（`aggregate_attempt_costs`，含重试全部 attempt）写入 outbox `scores_payload["_usage_tokens"]`；`app/langfuse_sync.py` 投影时以非内省的独立平台 Score 上报：
  - Score name `tokens_total`；score id `score:{item_id}:gen{generation}:tokens_total`（`stable_score_id` 幂等）；
  - metadata 含 input/output tokens、snapshot_id、revision、source=`ARGUS_FROZEN_SNAPSHOT`。
- run 级：`app/result_snapshots.py` run-score outbox payload 增加 `run_tokens_total`（= summary `usage_tokens_total`，全部 usable case token 总和）；`app/langfuse_run_scores.py` 自动以 `argus_run_tokens_total` 发布，score id `argus-run-` + sha256(snapshot_id:metric)。
- fail-closed：全部用例无 token 证据 → 不发 Score（不写 0）；部分用例缺证据 → 只对有证据的 case 计入，run 总和仍发布（由 `usage_unavailable_reason` 在 Argus 侧解释）。
- 单测：`tests/test_langfuse_usage_scores.py` 7 个用例覆盖 outbox 组装（重试求和 / 无证据缺省）、item Score 发布与负例、run payload、run Score 发布与负例；RED 先行（4 失败 → 实现 → 13/13 绿，含 `test_aggregation_usage.py`）。

## S6.2 — 历史补绑脚本（本地完成；云端执行待授权）

- `scripts/backfill_langfuse_run_items.py`：凭据运行时从 `.env.cloud` 读取（只保留 `LANGFUSE_*` 前缀，值不入日志、不入库、不入仓）；只处理显式 `--launch-id`；载荷与 worker 相同（`run_name/dataset_item_id/trace_id/observation_id/dataset_version`，版本不得为 `latest`）；HTTP 409 / duplicate / conflict 归类为 `SKIPPED_DUPLICATE` 不算失败；`--dry-run` 不触网。
- 单测：`tests/test_backfill_langfuse_run_items.py` 6 个用例（载荷组装、缺 trace/非终态 Skip、发布与重复归类、dry-run 不触网、env 过滤、缺必需凭据退出）。

## 门禁与回归（2026-10-16 实测）

- `make validate`：**PASS**（8/8；第 7 步 pytest 1345 passed / 7 skipped，`TEST_POSTGRES_URL` 相关跳过属环境边界；第 8 步 `docker compose config: OK`）。
- `ruff check services tests`：PASS（含本次修复 `e4fe5ce`）。
- Console 契约快照：`docs/openapi.json` 与 `services/console/src/api/schema.d.ts` 无 drift。

## 待授权的运行时验收（BLOCKED / NOT RUN）

| 项 | 状态 | 需要的授权 |
|---|---|---|
| S6.2 真实云端补绑（三条历史 run：`6a72839b` / `22d697bd` / `d78c126d`） | NOT RUN | 用户批准 `--env-file .env.cloud` 真实执行命令（先 `--dry-run`）；验收 = `GET /api/public/experiments` 三 run itemCount 达到 dataset item 数 |
| S6.4 AgentVersion v2 注册（v1 之上追加 `usage_cost_mapping` 三 token 路径，不配 amount） | NOT RUN | 用户批准注册 payload（真实注册操作） |
| S6.7 真实付费 launch 验收（一次 launch 串联 v2 注册 + token Scores + Console 卡片） | NOT RUN | 用户批准启动命令与成本预算（Attempt 层 retry=0，Langfuse 云端联调） |

以上任一项执行前单独呈报具体命令；未获授权不执行。真实证据回填本记录后本轮完成。

---

## 运行时验收（2026-10-16 云端执行 S6.4 / S6.7）

> 授权：用户批准后执行（Langfuse 云端 jp 区，项目 `cmu5a6fj500aoad0jvrckiypb`，dataset `banking-agent-regression` = `cmu5gbnus0006ad0kjc2u7r1k`，不本地起 Langfuse，不读取/不打印 `.env.cloud` 凭据）。

### S6.4 — Agent 版本注册（实际执行）

- `real-llm-agent` 经 `POST /api/v1/agents` + `POST /api/v1/agent-versions` 注册：
  - v2（id `325a7383…`）：mapping 路径误用 `usage.prompt_tokens/completion_tokens`（网关层字段），不是 Agent 实际响应字段 → 仅 `total_tokens_path` 生效，input/output breakdown 落 0。
  - v3（id `3bd48ffe…`）：mapping 路径修正为 Agent 实际响应字段 `usage.input_tokens / usage.output_tokens / usage.total_tokens`。
- 事实修正：Agent 响应 usage 字段为 `{input_tokens, output_tokens, total_tokens}`（如 33/31/64）；`prompt_tokens/completion_tokens` 只存在于网关/底层模型侧。

### S6.7 — 真实付费 launch（实际执行）

- 云栈重建：`docker compose -f docker-compose.cloud.yml -f docker-compose.real-llm.yml --env-file .env.cloud --env-file .env.local up -d --build --no-deps eval-runner argus-console`，`ARGUS_BUILD_ID` 以临时 override（`.tmp.compose-buildid.yml`）指向本地 HEAD `e59c431…`（提交后保持未跟踪状态，不入仓）。
- launch v2（`6c8a51f9-5de2-4d53-8454-a9efa705d605`，agent v2）：
  - 6/6 execution succeeded，`COMPLETED`，`langfuse_sync_status=SYNCED`；run `b370a9bf-250a-4c3a-925e-2658fda47f50`。
  - summary：`usage_tokens_total=6541`、`usage_tokens_per_case=1090.1667`（6 cases）；`usage_input_tokens_per_case=0`、`usage_output_tokens_per_case=0`（映射不对，符合预期边界）。
- launch v3（`ef196ee2-8b94-409f-8b35-aaf5e77dcce3`，agent v3，正确 mapping）：run `fb7f61b8-761e-4ce7-b980-880c58fa51d4`，`COMPLETED` + `SYNCED`。
- Agent registry 事实：eval-runner 云栈 DB 为容器内 sqlite（`/tmp/argus.db`），容器重建即丢注册数据；首注册因重建丢失后重注册回（已知运维边界，registry SSOT 仍是 Postgres 的 prod 配置，云栈 standalone 模式为临时演示面）。

### S6.6 — Langfuse token Scores 云端实证（scores_v3 API 查询）

- `langfuse.api.scores_v3.get_many_v3(from_timestamp=…)`（26h 窗口）：
  - item 级 `tokens_total`：**12 条**（两次 launch 各 6 case，NUMERIC）。
  - run 级 `argus_run_tokens_total`：**2 条**，value `6541.0`（v2 @ 01:23Z）/ `8048.0`（v3 @ 01:27Z），`source=API`, `project_id=cmu5a6fj…`。
- `argus_score_mean_*`、`argus_pass_rate`、`argus_p95_latency_ms` 等既有 run Score 同窗可见，投影链路正常。
- run 级 Score 按 `experiment_id=<run_id>` 过滤可查（v2 / v3 各 10 条）→ 新组织 Langfuse v4 把我们的 run id 当作合法 experiment 实体存储、Score 关联正确。

### Langfuse run-item 可见性根因（v4 新组织，S6.1/S6.2 原设计不成立）

1. **现象**：三次 `experiments.list`（cursor 分页 3 页、窗口 3 天、142 条）与 `experiments.list_items(experiment_id=<run_id>, from_start_time=…)`（含 1970 起点与宽时间窗）均未出现我们的 run `issue198-token-e2e-v2/v3`（及此前手动补绑 probe 的那条）。仅返回 2026-10-15T07:06Z 及更早的 run。
2. **对照组**：昨天的 CI run（`argus-ci-v1/v2-<workflow>`，itemCount=6）在 `experiments.list` 可见；其 `list_items` 有 6 条 item，且 item `dataset_item_id=None`, `dataset_version=None`，6 条共享同一 `trace_id` —— 这不是 `dataset_run_items` 表形态，而是 v4 experiment 投影形态。
3. **根因**：`scripts/ci-e2e-cloud.sh` 走 legacy `/experiments/run` 端点，内部命中 `execution.py` 的 `lf_dataset.run_experiment(...)`（Langfuse Python SDK v4 experiment runner，OTel 投影）→ 物化 v4 experiment。而版本化 Launch 的 worker 同步路径（`langfuse_sync.py` Phase 4）用 `lf.api.dataset_run_items.create`（legacy REST）。Langfuse 官方文档（`docs/evaluation/experiments/data-model`）明确：**没有创建 experiment run 的公开 REST 端点；legacy `POST /api/public/dataset-run-items` 已 deprecated**。v4 新组织只能通过 SDK `run_experiment` / UI / OTel（baggage + item root span 属性契约）物化 experiment；`dataset_run_items.create` 虽然返回成功（写入 legacy 行，可重复 create 不冲突），但在新组织不进 `GET /api/public/experiments`、UI run 详情为空。
4. **结论**：S6.2 原验收标准（`GET /api/public/experiments` 三个历史 run itemCount 达标；worker 路径 `dataset_run_items.create` 即可）对本组织不成立。真正的修复方向是让 run-item 物化走 v4 合同（OTel experiment 属性重放 / SDK `run_experiment`），这是新的实施分节，需用户决策后另立方案执行（见下）。

### 成本 / Usage 状态（更新口径）

- v2 launch：仅 total token 口径生效（6541 / 1090.17 avg，6 cases）；input/output breakdown 为 0 是 v2 冻结 mapping 的已知缺陷，v3 已修正，不再计入缺陷。
- v3 launch：run 级 `argus_run_tokens_total=8048`（云端 Scores 侧读数）。
- money 字段：不展示、不参与换算（保持用户定稿）。

### 待决（BLOCKED，等用户决策）

1. **S6.2 历史补绑执行方案改向**：旧三条历史 run（`6a72839b` / `22d697bd` / `d78c126d`）的 Runner 侧数据已随云栈容器重建丢失（sqlite 重置）；且 `dataset_run_items.create` 的原方案在新组织不可见。候选方向：
   - A. OTel experiment 属性重放（`langfuse.experiment.id/name/dataset.id` baggage + item root span `langfuse.experiment.item.*`），把已有 trace 重新投影进 v4 experiment；workerPhase 4 与 backfill 脚本都要改到该路径，需先做一次最小 probe 验证 Langfuse 对"同 trace 重放"的合并语义。
   - B. 接受 v4 新组织边界：run-item 面通过 CI 的 `run_experiment` 路径保证（可证明），版本化 Launch 的 run-item 可见性作为 open issue 记录、暂不修。
   - C. 迁移到 SDK `dataset.run_experiment` 重跑（付费重放，改变评测语义，不推荐）。
2. `.tmp.compose-buildid.yml` 为本地临时 override，不提交（保持 untracked）。

### 方向 A probe 记录（2026-10-16，OTel replay 三连验证）

执行环境：Langfuse 云端（jp 区，`cmu5a6fj…` 项目）；POST `/api/public/otel/v1/traces`（OTLP/protobuf，basic auth，`x-langfuse-ingestion-version: 4`）。三次 probe 全部 200。

| probe | 动作 | 关键属性 | 结果 |
|---|---|---|---|
| P1 合成 trace | 新 trace root span（app 侧 `langfuse.experiment.*` + item 属性，experiment id 自配 `107bcc65…`，run name `argus-issue198-otel-probe-1`） | `langfuse.experiment.id/name/dataset.id`, `item.id`, `item.root_observation_id` = 本 span id | `experiments.list(id=…)` **1 次命中**、itemCount=1、`list_items` 1 条（trace id 匹配）→ v4 experiment 由 OTel 属性物化成立，experiment id 可自配 |
| P2 已有 trace 重放 | v3 run `fb7f61b8…` 的真实 case trace `8dbe7811…`（root observation `88b26ef7…`）同 trace_id / 同 span_id 重放，experiment id = **真实 run id**、name = `issue198-token-e2e-v3` | 同上 + item.id = `00000000-0000-4000-8000-000000000001` | `experiments.list` **命中真实 run 且 itemCount=1**；`list_items` item trace_id 匹配；原 CHAIN root observation 保留（重放同 span id 生成第二行 SPAN 记录，无数据丢失）→ backward 合并语义成立 |
| P3 补 item.version 重放 | P1 trace 重放并增加 `langfuse.experiment.item.version` | + `item.version` | item_count 不变，`list_items.dataset_item_id` 仍 None（与 CI 可见 run 同形态，v4 rest 投影不含该列，非阻断） |

结论（直接进入实施设计输入）：

1. **v4 合同成立**：在新组织物化 run + run item 的做法 = 对每个 case 的 item root observation 元数据重放带 `langfuse.experiment.*` baggage 属性（`OTLP POST /otel/v1/traces`）。
2. backward 合并安全：同 trace / 同 root span id 重放保留既有 observation 与 Scores，不会写 0。
3. worker / backfill 的下位可选实现（供正式实施分节）：
   - **首选**：Runner 在创建 item trace 时即注入 experiment baggage 属性（execution 时 manifest 已可用），实现"一次写入即物化"，无重放；
   - **补绑**：backfill 对历史 launch 的每个 case root span 重放（P2 路径），幂等（重放同 id 不炸不重复）。
4. 云端遗留：probe 产生的两条数据（experiment `107bcc65…` + v3 run 的 1 条已物化 item）保留作为 UI 侧证据点，可人工在 UI 核对；project 数据量不爆炸。

## S6.2 方向 A 落地（2026-10-16）：OTel projection 策略 + 真实 E2E 终验

### 实现内容（本地，分支 `codex/issue198-real-llm-agent`，未 push）

- **Worker 路径策略化**（`services/eval-runner/app/langfuse_run_items.py`）：`link_dataset_run_item` 统一封装 legacy REST 与 `otel` 两条策略；`ARGUS_LANGFUSE_RUN_ITEM_STRATEGY=otel`（每次调用实时读取，不缓存）。OTel 路径强校验 `dataset_id` / `observation_id` / base_url+keys，缺一 fail closed。
- **OTLP 投影模块**（`services/eval-runner/app/langfuse_otel_projection.py`）：自建 OTLP protobuf payload，POST `/api/public/otel/v1/traces`（`x-langfuse-ingestion-version: 4`，basic auth），span 属性 `langfuse.experiment.id/name/dataset.id` + `langfuse.experiment.item.id/root_observation_id/version`；凭据不落日志/DB/仓库。
- **backfill 脚本升级**（`scripts/backfill_langfuse_run_items.py`）：支持 `otel` 模式（从 env-file 读凭据、失败 entry 附 `detail`/`status_code`、非 dry-run 逐条打印非 CREATED 结果）；修复原先只读 `os.getenv("LANGFUSE_BASE_URL")` 导致 backfill base_url 为空的根因。
- **版本归一化修复**（`langfuse_sync.py` Phase 4）：`parse_dataset_version` 返回 datetime 时转 `isoformat()`，消除 `_attr_str` 的 `bad argument type for built-in operation`；Phase 4 直接调用统一包进 `_invoke_with_timeout`（对齐 round4 audit 语义）。
- 环境事实（诊断时确认）：cloud POC 栈 eval-runner 为容器内 SQLite + 无 Redis；`_syncer_loop` 静默吞异常，"容器日志无声"不等于 sync 未执行，需以 `langfuse_sync_tasks` 状态为准。

### 真实 E2E 终验（v5 launch，付费真实模型）

- Launch `79dca54d-c95c-4aa5-8761-e154512a9e00`（dataset `banking-agent-regression` = `cmu5gbnus0006ad0kjc2u7r1k`，frozen version `2026-10-16T01:25:21.912011+00:00`，6 cases，全部 succeeded）。
- Runner outbox：`langfuse_sync_tasks` 6 条全部 `SYNCED`、`attempts=1`、无 retry；trace_id/observation_id 为 launch 真实链路标识（32/16 hex）。
- **云端验证（Langfuse v4 API，`fromStartTime` 时间窗）**：
  - `experiments.list(id=53751a88-3a79-5564-8e10-2b9470dcefb9)` → **1 命中**，name=`argus-79dca54d-c95`；
  - `experiments.list_items(experiment_id=…, from_start_time=1970 起点)` → **6 items**，trace_id 与 outbox 6 条逐一匹配。
- run_id 派生口径（重要）：worker 实际发送的 `dataset_run_name` 为 LaunchService 派生的 `argus-<launch_id 前 n 位>`（本例 `argus-79dca54d-c95`），不是 launch 人类可读名 `issue198-token-e2e-v5`。`self_assigned_run_id = uuid5(URL, "argus-run:<launch_id>:<run_name>")`。
- 排查更正记录：此前"experiments.list(id=677feeaf…)=0"为**假阴性**——验证脚本用 launch 可读名派生 run_id，与 worker 实际发送的 run_name 不一致；worker OTel 投影本身一直是成功的，无需 backfill 补绑。
- backfill 路径同样验证过（v3 run）：launch `ef196ee2-8b94-409f-8b35-aaf5e77dcce3` 补绑 `created=5 duplicated=0 failed=0 skips=1`；run `fb7f61b8-761e-4ce7-b980-880c58fa51d4` itemCount=6。

### 验证结论

- S6.2 方向 A：**PASS** —— 真实付费 Launch 在 worker 内经 OTel 策略完成 v4 experiment + 6 run item 的物化，云端 `GET /api/public/experiments` 与 `list_items` 双向可见。
- 附录：v4 experiments API 需要必填 `fromStartTime`（ISO 8601）；不带该参数返回 400。
