# Cloud E2E 全自动准入与发布门禁

本文描述 Argus Cloud Integration Policy 的自动准入契约。整个 PR CI 流程**不依赖人工 Environment 审批、人工填写 SHA 或手动触发 Cloud 候选工作流**。仓库 YAML 不会自动设置 GitHub 环境/Ruleset：管理侧配置只需初始化，日常 PR 无须人工干预。

## 事件与可信执行边界

1. `.github/workflows/ci.yml` 在 PR 上运行五项常规检查：`Code Quality`、`Full Python Tests`、`Docker / Compose Validation`、`Console Quality & E2E`、`S4 Local Langfuse Integration`。PR 的 `Langfuse Cloud E2E` 只报告 `skipped_by_policy`，不读取 Cloud Secrets。
2. `.github/workflows/cloud-integration-policy.yml` 在**可信 main** 上通过 `workflow_run: CI completed` 运行。只读 Evaluate/Publish 负责刷新所有当前 PR 的 `Cloud Integration Policy`。只有专用 Dispatch Job 持有 `actions:write`（无 Langfuse Secrets），在上游 CI 成功且属于当前 main/base、same-repository PR、风险分类为 `high` 后，使用 `GITHUB_TOKEN` 自动 `workflow_dispatch` 候选验证。低风险跳过真实 Cloud；失败、fork、旧 SHA 和不完整身份一律不会派发。
3. `.github/workflows/cloud-e2e-trusted.yml` **只接受由 GitHub Actions bot 触发的候选**；冻结 PR/head/base、测试 merge SHA、CI run/attempt、可信 main workflow revision；再次从 GitHub API 检查。候选 Cloud Job 在单独测试 Environment 中运行真实 Cloud；Environment **不要求人工审批**。候选流程内只授予 API 读权限，并在先完成候选镜像构建后才导入测试凭证。
4. `scripts/ci/cloud_e2e_receipt.py` 由 main 的可信控制代码生成 schema v2 receipt，要求 bot 自动派发身份、当前 PR identity、Prepare/Cloud Job + 必需 Steps success、真实 Cloud artifact（managed-cloud + async-link，v1 2/6，v2 6/6），并记录 `authorization.mode=trusted_ci_automatic` 与 CI run/attempt。**不再读取 GitHub Environment approvals API**；缺少或不匹配任何其他证据仍 fail-closed。
5. 候选工作流在 `receipt` Job 完成后，额外执行隔离的 `refresh-policy` Job，通过 `GITHUB_TOKEN` 发出显式 `workflow_dispatch`，要求可信 `cloud-integration-policy.yml` 重新评估当前 PR。这个回调在收据成功或失败时都执行，避免停留在旧的 Pending。GitHub 对 `GITHUB_TOKEN` 引发的普通事件有限制，不能依赖自动派发候选的 `workflow_run` 事件继续链式触发；`workflow_dispatch` 是受支持的例外。Callback 只持有 `actions:write`，不运行 PR 代码、不接收 Langfuse Secrets。为避免 callback 自身尚未退出时 Policy 抢先读取到候选 `in_progress` 导致再次 Pending，刷新请求携带 `candidate_run_id` 和 `candidate_run_attempt`，Policy 的只读 Evaluate Job 最多等待 240 秒确认精确候选 Run 已 `completed`，然后才重新评估，超时 fail-closed。`publish_cloud_policy.py` 在回调的 `workflow_dispatch` 中重新核对当前 main、PR head/base/merge、CI attempt、candidate artifact 与经过 GitHub API 重算的 receipt，一致才向当前 head 写 `Cloud Integration Policy=success`。旧人工触发收据（v1 或 actor 非 bot）不能绕过新策略。
6. `.github/workflows/langfuse-i18n.yml` 仍独立验证当前 main 的 `Main Cloud Acceptance` 与真实 Cloud 证据；它不接受 PR 的跳过记录代替正式发布测试。

## 策略行为

| 当前 PR / CI 状态 | Cloud | Policy |
|---|---|---|
| 五项常规检查未完成 / CI 身份未确定 | 不派发 | pending |
| 任一常规检查失败、取消、skip、超时 | 不派发 | failure / error |
| 已分类低风险、五项全部通过 | skipped_by_policy | success |
| 高风险、五项通过、可信候选尚未派发或运行中 | 自动派发 / 运行中 | pending |
| 高风险、Cloud 测试或必需证据不通过、错 SHA、错 actor、错 artifact/attempt | failed | failure / error |
| 高风险、可信当前 CI + Cloud 实测 + 自动 receipt v2 都成功 | passed | success |
| main Cloud 没有跑完整 | 不用于发布 | Main Cloud Acceptance 阻止正式发布 |

每次提交/更新 PR 都会运行 CI。main 前进导致 PR base/merge 身份变化时，旧收据作废，不能复用旧 run；PR 在新 base 上重新运行 CI 后，控制器会自动重新派发。旧失败 Run 不要通过重跑旧 attempt 代替新身份验证。

## 环境与凭证配置（一次性）

- 在 `langfuse-e2e` Environment 配置 main Cloud 的 `LANGFUSE_BASE_URL`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`，仅指向专用非生产测试 project。main Cloud 由 `LANGFUSE_E2E_ENABLED=true` 控制。
- 在 `langfuse-e2e-candidate` **单独**配置三项同名测试 Secrets；只允许默认分支 `main` 的可信控制工作流使用，**不启用 Required reviewers**。无审批不意味着可使用生产账号。
- **安全代价**：同仓库 PR 的候选代码会在独立 runner 内执行，其应用和测试进程可能接触 Cloud 测试凭证；如果允许不受信任提交者直接向同仓库分支推送恶意代码，长期有效凭证可能被读取或外传。必须使用隔离、最小权限、可随时轮换的测试 project / token，不允许生产/共享敏感项目，并优先采用短期凭证。对 fork PR 保留 Cloud 自动验证 fail-closed，而不是向 fork 暴露 Secrets。
- GitHub Ruleset 应在新 Context 完成真实验证后要求五项常规检查 + `Cloud Integration Policy`，strict=true、无 bypass；旧 `Langfuse Cloud E2E` context 要在新检查可靠后再移除。Workflow/CI 源码仍需分支保护，不能为了自动化移除强制测试。

## 验收标准

- [ ] 同仓库低风险 PR：常规 CI 通过后 Policy 自动成功，无候选 Cloud run。
- [ ] 同仓库高风险 PR：不需要手动 dispatch/审批；CI 成功后自动出现 bot 发起的候选 Run；Prepare、Cloud、Receipt、Refresh 四个 Job 全成功；v2 receipt 的 `authorization.mode=trusted_ci_automatic`，独立 `Cloud Integration Policy (workflow_dispatch)` 回调完成后当前 PR 的 Policy 自动成功。
- [ ] 无 Required reviewers 时 `GET /actions/runs/{id}/approvals` 为 `[]` **不影响** Cloud 结果。
- [ ] CI 失败、旧 base/head、fork、伪造 actor、修改工作流路径、错 CI attempt、缺 Cloud 步骤或 evidence 均不能产生成功的 Policy。
- [ ] 管理员在 GitHub 实际确认 Workflow permissions 支持 Dispatch Job 的 `actions:write`，`GITHUB_TOKEN` 的新工作流派发已触发事件；若被组织策略限制，必须在组织权限侧解决，不采用 PAT 硬编码。
- [ ] 确认真实 Ruleset provider、required contexts、Environment Secrets/分支限制和正式 main Cloud 准入均符合文档；未实测不写成已部署。

2026-10-14 的历史 Run #38551530777 曾因没有 Required reviewers 而错误失败；旧 schema v1 强制 approval 的逻辑已由新自动 receipt v2 替代。旧 Run 无法通过重跑升级可信 workflow revision。