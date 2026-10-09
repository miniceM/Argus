# PR #125 后端读模型字段映射与契约缺口结论（BE-01 / BE-05 交付）

日期：2026-10-09（Asia/Shanghai）
基线：PR head `2acad659ebf8526145f1072cbf94b2dc4198b1ba`（OPEN / MERGEABLE，执行前已实时核对）；本地测试合并提交 `2d212eb4a1079a975278aa7974e8e3b84962b052`。
范围：仅 Runner API/领域契约与后端测试；前端 F1–F12 修复不在本文范围。

## 1. 结论摘要

1. **Console 拼装 PR #125 所需的全部冻结字段已可从现有端点取到，接口差异清单为空**：无需新增端点、响应字段、数据库表或迁移。
2. **正式质量/发布语义必须来自 `GET .../comparison` 的 `formal` 与 `comparability` 对象**；前端不得用 cohort 诊断或硬编码 90%/500ms 自造门禁。
3. **不存在 run 级冻结 `quality_conclusion` 字符串**：Snapshot detail/summary 只提供冻结计数（`quality_pass_count` / `quality_fail_count` / `quality_unknown_count`）、`evidence_state`、`releasable` 与逐用例结论。`GET /experiment-launches/{id}` 顶层的 `quality_conclusion` 是 live 可变字段。按交接约定：前端用冻结统计展示，不拿 live conclusion 补洞，也不因此新增 Gate 规则。
4. **三个易错形状**（前端消费时必须注意，详见 §3）：`versions` 扁平 vs comparison 分侧嵌套、`threshold` vs `expected`、`quality_evaluation` 是带 `rules` 的 dict。
5. **生产代码零改动；契约已验证，PR #125 已确认缺陷（F1–F12）的修复落在前端（FE-01–FE-07）**。交付 = 本映射 + 新增 5 个契约验证测试 + 既有回归引用；没有为了 F3 的 UI 硬编码新增 90% 或 500ms 规则。

## 2. 字段映射表（Header / KPI / Cases / Comparison / Export）

`来源` 列：F = 冻结（随 `(launch_id, snapshot_id)` 固定），L = live/可变。
`覆盖测试` 列均为本仓库当前测试，行末 PASS 证据见 §6。

| Console 显示项 | Endpoint → 字段 | 来源 | 覆盖测试 |
|---|---|---|---|
| 报告身份（Header/URL/Export） | `GET /api/v1/experiment-launches/{id}/result-snapshots/{sid}` → `launch_id`、`snapshot_id`、`revision`、`created_at`、`manifest_digest`、`source_result_digest` | F | `test_pr125_launch_detail_contract.py::test_console_report_contract_reaches_every_required_frozen_field` |
| 四维版本（Dataset/Agent/Evaluator/Runner） | 同上 → `versions.dataset/agent/evaluators/runner` + `versions.environment` | F | 同上 |
| 冻结输入文本（Cases input） | `GET /api/v1/experiment-launches/{id}` → `manifest.dataset.items[]`（Launch 创建时冻结的 Dataset 副本），按 `dataset_item_id` 关联；Snapshot 的 `items[].case_digest` 锚定内容身份 | F（Launch 级冻结副本） | `test_snapshot_bytes_are_independent_of_live_manifest_and_item_mutation` |
| 冻结规则/阈值 | ① `manifest.quality_policy.rules[]`（`operator`、`threshold`、`result_type`、`required`、`critical`）② `snapshot.items[].quality_evaluation.rules[]`（`operator`、`expected`、`observed_value`、`conclusion`、`reason_code`、`explanation`） | F | `test_unknown_safe_counts_frozen_thresholds_and_fail_closed_evidence` + `test_issue_83_quality_policy.py` |
| 全量质量 KPI（PASS/FAIL/UNKNOWN） | detail/summary → `summary.quality_pass_count`、`quality_fail_count`、`quality_unknown_count`、`total_cases`、`decided_case_count`、`decided_pass_rate`、`decision_coverage` | F | 新增 BE-02 两个测试 + `test_issue_83_quality_policy.py::test_four_pass_one_fail_one_unknown_reports_80_percent_and_five_sixths` |
| 报告级质量结论 | detail → `summary` 计数 + `evidence_state` + `releasable`；**无 run 级冻结 `quality_conclusion` 字符串**（见结论 3） | F | `test_unknown_safe_counts_frozen_thresholds_and_fail_closed_evidence` |
| 证据完整性 | detail/summary → `evidence_state`（COMPLETE/DIAGNOSTIC）、`evidence_reasons`、`releasable`（仅 detail） | F | 同上 + `test_issue_85_snapshot_revision.py` |
| Typed 结果与逐规则理由 | detail → `items[].evaluation_results[]`（权威 typed results）、`scores`（数值投影）、`quality_evaluation` | F | `test_issue_82_typed_results.py::test_snapshot_preserves_typed_results` |
| 延迟/成本（缺失≠0） | detail → `items[].latency_ms`、`cost`、`cost_evidence`；summary → `total_cost`、`cost_per_case`、`cost_coverage`、`cost_currency`、… | F | `test_api_results_s3.py::test_cost_comparison_uses_frozen_cohort_and_requires_complete_compatible_costs` |
| 正式比较与退化裁决 | `GET .../comparison?snapshot_id=…` → `formal`（`available`/`verdict`/`withheld_reasons`/`coverage`）、`comparability`（`comparable`/`reason_codes`/`dimensions`）、`classification_counts`、`summary.cost_comparison` | F | `test_issue_86_comparability.py`（REGRESSION / QUALITY_POLICY_CHANGED / COVERAGE_INCOMPLETE 三类） |
| 单用例证据与 Trace | detail → `items[].output_ref = {observation_id, trace_id}`、`trace_url`；`GET .../comparison/case` → `baseline/candidate.output_status`（AVAILABLE/NO_REFERENCE/NOT_FOUND/FETCH_FAILED）、`output`、`retryable`、`scores` | 冻结引用 F；Output 内容按引用实时取 | `test_result_outputs.py`（含"绝不接受调用方伪造 ref"）+ `test_api_results_s3.py::test_comparison_case_details_support_baseline_only_candidate_only_and_missing_cases` |
| 修订历史 | `GET .../result-snapshots` → `revisions[]`（`snapshot_id`、`revision`、各质量计数、`evidence_state`、`is_latest`）、`latest_snapshot_id` | F | `test_issue_85_snapshot_revision.py::test_shared_snapshot_link_returns_the_same_revision_after_a_new_one_exists` |
| Baseline 身份与 CAS | `GET/POST /api/v1/agents/{id}/baselines` → `revision`（绑定指针）、`result_snapshot_id`、`result_revision`、`expected_revision`（请求）、409（陈旧） | F（绑定状态 live，写入走 CAS） | `test_issue_85_snapshot_revision.py::test_concurrent_baseline_writes_conflict_on_expected_revision` + `test_api_results_s3.py::test_baseline_http_api_uses_revision_cas` |
| 实时执行状态/允许动作 | `GET /api/v1/experiment-launches/{id}` → `status`、`progress`、`allowed_actions`；`GET .../items` → `execution_status`、`eval_status`、`evaluation_status`、`attempt_count` 等 | L（必须与冻结报告分栏标注） | `tests/test_state_machine_s2.py`、`tests/test_issue_84_eval_only_retry.py` |
| Langfuse 同步状态 | summary → `langfuse_sync`（`item_trace` / `run_score` 分 scope）、`langfuse_score_sync_status` | L（独立于冻结结果） | `test_issue_87_langfuse_projection.py`、`test_status_and_langfuse_decoupling.py` |
| Launch 实时质量字段（**不要用于历史报告**） | `GET /api/v1/experiment-launches/{id}` → `quality_conclusion` | L | 由新增冻结独立性测试反向固定（live 变化不影响冻结读取） |

## 3. 三个易错的响应形状（前端必须按此消费）

1. **`versions` 形状不同**：detail/summary 是扁平的 `versions.dataset.version`；comparison 是按侧嵌套的 `versions.candidate.dataset.version` 与 `versions.baseline.dataset.version`。写错会直接 `KeyError`。
2. **阈值字段名不同**：冻结 Policy（`manifest.quality_policy.rules[]`）用 `threshold`；逐用例规则评估（`quality_evaluation.rules[]`）用 `expected`。两者都是原始数值（`0.8`），`operator` 都是 `">="`；**不得 ×100 或加 `%`**。
3. **`quality_evaluation` 是 dict 不是 list**：顶层有 `conclusion`、`policy_id/policy_version/policy_digest`、`releasable`、`unknown_reasons`、`rules[]`；逐规则信息在 `rules[]` 内。另外 `RunSummaryResponse` 没有 `releasable`，只有 detail 才有；summary 只有 `evidence_state`。

## 4. 接口差异清单

**必需字段差异：空。** 无新端点、无响应模型改动、无 OpenAPI/Console schema 同步需求、无数据库迁移。

按交接要求对 4 个候选缺口逐条核对：

| 候选缺口 | 结论 | 依据 |
|---|---|---|
| 冻结 Header 结论 | **不需要新字段**：冻结计数 + `evidence_state` + `releasable` 足以展示；正式发布语义用 `comparison.formal` | §2 报告级质量结论行 + BE-02 测试 |
| 数值单位/方向 | **已有**：Policy `operator`/`result_type` 与 evaluator `direction` 在冻结 manifest；逐规则 `expected`/`observed_value` 在 detail | `test_unknown_safe_counts_frozen_thresholds_and_fail_closed_evidence` |
| 缺失/不可用原因 | **已有**：`evidence_reasons`、`unknown_reasons`、`cost.unavailable_reason`、`output_status + reason + retryable` | §2 证据/成本/输出行 |
| 可重试错误读模型 | **已有**：`comparison/case` 的 `output_status=FETCH_FAILED + retryable=true`；无快照 409；不存在/跨 Launch 404 | 新增 fail-closed 测试 + `test_result_outputs.py` |

## 5. 场景夹具清单（BE-05）

夹具生成器位于 `tests/test_pr125_launch_detail_contract.py` 的 `_manifest` / `_quality_evaluation` / `_seed`，与生产响应同形状（FastAPI `response_model` 实际校验）。前端可直接用同一组场景做 mock；"真实 API"列指该场景是否已有真实端点断言。

| 场景 | 生成位置 | 前端预期断言 | 真实 API 断言 |
|---|---|---|---|
| 正常双侧 + 退化候选（1 FAIL vs 1 PASS） | `test_console_report_contract_...` | Header/KPI/Cases/Comparison 同一 `snapshot_id`；formal 对象可达 | 有（同一测试） |
| 无效 / 跨 Launch Snapshot | `test_pinned_endpoints_reject_...` | 四个读取端点全部 404，无 latest 回退，写操作不发出 | 有 |
| S1 冻结 vs live 已变绿 | `test_snapshot_bytes_are_independent_...` | 历史报告仍 1 PASS / 1 FAIL；live 栏才显示新状态 | 有 |
| UNKNOWN 混合结果 | `test_unknown_safe_counts_...` | 全量 1/3、decided 1/2、coverage 2/3 三口径分开展示；DIAGNOSTIC 不可作正式证据 | 有 |
| 无 Baseline（BASELINE_NOT_BOUND） | `test_api_results_s3.py::test_summary_and_comparison_use_frozen_baseline_and_trace_links`（同一夹具族） | 比较不可用 + 原因，Candidate 侧仍可看 | 有（既有） |
| 上游 Langfuse 不可用 | 新增首测（`output_status=FETCH_FAILED`、`retryable=true`） | 显示可重试错误，不得伪装成"无输出" | 有 |
| Typed 结果 + provenance（权威 typed results、缺失≠0） | `test_issue_82_typed_results.py::test_snapshot_preserves_typed_results`（既有夹具） | `evaluation_results` 类型保留；`scores` 仅数值投影 | 有 |
| 409 CAS / 单侧 Case / 成本缺失与币种不兼容 | 既有夹具 | 409 后取新 revision 再确认；单侧明确 CASE_MISSING；成本 reason 显示 | 有（`test_issue_85` / `test_api_results_s3`） |

**最小场景集缺口**：交接要求的"50+ Cases、混合币种、部分成本覆盖"已有成本语义测试但未在本文件生成 50+ 行数据样本；如前端需要大样本 mock，用 `_seed` 生成 50+ case 的变体即可，不需要后端改动。

## 6. 逐项验收状态（BE-01 → BE-05）

| 编号 | 状态 | 证据 |
|---|---|---|
| BE-01 冻结结果与 Snapshot 归属 | **PASS** | 新增 `test_console_report_contract_...`、`test_pinned_endpoints_reject_...`、`test_snapshot_bytes_are_independent_...`（S2/历史不变另有 `test_issue_85` 既有回归）；§2 映射 + §4 缺口清单为空 |
| BE-02 比较/数值/证据完整性 | **PASS** | 新增 `test_unknown_safe_counts_...`、`test_run_summary_keeps_...`；既有 `test_issue_83`（UNKNOWN 分母/真实零）、`test_issue_86`（formal REGRESSION/withheld）、`test_api_results_s3::test_cost_comparison_...`（缺失/部分/币种） |
| BE-03 Baseline CAS/资格 | **PASS** | 既有 `test_issue_85`（并发 409、资格、历史 Candidate 不动）+ `test_api_results_s3::test_baseline_http_api_uses_revision_cas`；本交付零产品 diff，未引入 overwrite/放宽 |
| BE-04 冻结 Output/单侧用例 | **PASS** | 既有 `test_result_outputs.py`（冻结 ref、双侧独立失败、伪造 ref 被拒）+ `test_api_results_s3::test_comparison_case_details_...`；新增 `FETCH_FAILED + retryable=true` 断言 |
| BE-05 真实形状夹具 | **PASS** | `_manifest`/`_quality_evaluation`/`_seed` 经 FastAPI `response_model` 校验；§5 场景表逐条给出生成位置与预期 UI 断言；接口差异清单为空 |
| 条件性产品改动 | **未触发（NOT NEEDED）** | §4 四个候选缺口逐一被现有字段否定；生产代码 diff = 0 |
| 新产物远端 CI | **NOT RUN** | 无 push 授权，本地提交未推送 |
| 前端联合真实 API E2E / 真实全链路 | **NOT RUN** | 前端修复未开始；不以 mock 或本地套件冒充 |

## 7. 验证结果（实际执行）

执行位置：`/Users/hosea/.codex/worktrees/pr125-review/Argus`（HEAD = PR 测试合并提交 `2d212eb`；执行前实时核对 PR head 仍为 `2acad659`）。

| 门禁 | 命令 | 结果 |
|---|---|---|
| 新增 BE-01/02/05 契约测试 | `python -m pytest -q tests/test_pr125_launch_detail_contract.py` | **5 passed** |
| 新文件代码质量 | `python -m ruff check tests/test_pr125_launch_detail_contract.py` | **PASS** |
| 完整后端套件 | `python -m pytest -q tests` | **756 passed, 4 skipped**；4 项 skip 全部是 `TEST_POSTGRES_URL` 未配置的真实 PostgreSQL 测试，单独列出，未计入通过 |
| 仓库唯一本地门禁 | `make validate` | **PASS（8/8）**：配置解析、ruff、Demo Agent 零评测 SDK、Runner W3C、OpenAPI 快照同步、Console（契约同步 + `lint:tokens` + typecheck + **526 vitest** + build）、`pytest 756 passed`、`docker compose config: OK` |
| 远端 CI（只读回查，未推送） | `gh pr checks 125` | Code Quality / Full Python Tests / Docker-Compose **pass**；Console Quality & E2E **fail**（前端范围，见交接 FE 项）；Langfuse Cloud E2E **skipping** |
| 新产物的远端 CI | — | **NOT RUN**：无 push 授权，本地提交未推送 |
| 前端联合真实 API E2E | — | **NOT RUN**：前端 F1–F12 修复尚未开始，属双人联合验收步骤 |
| 真实 Worker→Agent→Langfuse 全链路 | — | **NOT RUN**：本任务为后端契约验证，不冒充全链路完成 |

TDD 说明（RED → GREEN）：首轮 3 个失败均为**测试自身接线/期望错误，不是产品缺陷**——
① `main.launch_service` 未随 `db_manager` 一起指向测试库，导致 launch 详情读到另一个库（404）；
② `RunSummaryResponse` 没有 `releasable`（该字段只在 Snapshot detail 上）；
③ `ComparisonResponse.versions` 按 `candidate`/`baseline` 分侧嵌套。
修正测试接线与形状断言后 **5/5 GREEN，生产代码零改动**，与交接约定"已有行为正确只需验证/补测试"一致。

> 注：`make validate` 的 Console 526 项运行时，已将上一轮审查用的未跟踪探针文件 `services/console/src/features/launches/__tests__/Pr125ReviewProbes.test.tsx` 临时移出（该文件含 11 个**预期失败**的缺陷复现探针，属于审查工具、不得随本交付提交），运行结束后已原位还原。审查探针副本另存于 `…/pr125-review/` 审查产物目录。
