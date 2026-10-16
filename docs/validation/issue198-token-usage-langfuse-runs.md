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
