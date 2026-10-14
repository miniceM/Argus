# Cloud E2E 全自动准入与发布门禁

本文描述 Argus Cloud Integration Policy 的自动准入契约。整个 PR CI 流程**不依赖人工 Environment 审批、人工填写 SHA 或手动触发 Cloud 候选工作流**。仓库 YAML 不会自动设置 GitHub 环境/Ruleset：管理侧配置只需初始化，日常 PR 无须人工干预。

## 事件与可信执行边界

| 工作流 | 触发与职责 | 凭证 / 写权限边界 |
|---|---|---|
| `.github/workflows/ci.yml` | 普通 PR 运行五项常规检查；PR 的 Cloud job 记录 `skipped_by_policy`，不取用 Langfuse Secret。main push/dispatch 在五项常规检查成功后运行真实 Cloud E2E，并由 `Main Cloud Acceptance` 聚合实际 Cloud 步骤与常规 job。 | main Cloud 使用 `langfuse-e2e` Environment；只放专用测试 Langfuse project 凭证，不放生产凭证。缺失凭证、关闭开关、Cloud skip/failure/cancel 都不得得到 Main Cloud Acceptance receipt。 |
| `.github/workflows/cloud-e2e-trusted.yml` | 仅 main 上的 `workflow_dispatch`；输入 PR 编号、人工审查过的完整 head/base SHA。先由可信控制代码固定 merge SHA 与 CI attempt，再进入候选 Environment 审批，批准后才运行候选 Cloud；后置可信 job 从 GitHub API 和有界 artifact 重新核实并生成 receipt。 | 候选工作流使用单独的 `langfuse-e2e-candidate` Environment，且仅含测试 project 凭证。候选代码在 Cloud 测试期间能接触该测试凭证，因此人工批准不是沙箱；必须审查候选，绝不可使用生产 Secret。 |
| `.github/workflows/cloud-integration-policy.yml` | `workflow_run` 监听 CI 与 Trusted Cloud Candidate（不按 source branch 过滤）、main push 重算 open PR、main dispatch 计算指定 PR。全局串行；默认分支可信控制代码分页读取文件、CI、审批、候选 receipt，并只在重新核对当前 PR head/base/main 后写 `Cloud Integration Policy`。 | 评估 job 只有 API 读取权限；写入 job 唯一写权限为 `statuses: write`，并仅保留重新核对身份所需的只读权限。不得 checkout 或执行 PR 源码。 |
| `.github/workflows/langfuse-i18n.yml` | PR 仍可本地构建、不发布；main push / main dispatch 的 image job 必须等到精确同 SHA 的主 CI 完成，并核对五项检查、真实 Cloud preflight/E2E 步骤、成功的 `Main Cloud Acceptance` job 和完全匹配的 receipt artifact。main 前进、错 SHA、失败/取消/skip、缺失或不匹配 receipt 均阻断发布。非 main 的手动 dispatch 不发布。 | GHCR 发布权限只在 image job；release gate 只有 `actions:read`、`contents:read`。自托管远端验收仍是独立的 `langfuse-i18n-integration` 流程。 |

## 策略状态矩阵

| PR / 运行状态 | `Cloud Integration Policy` | Cloud 覆盖 | 可否作为合并依据 |
|---|---|---|---|
| 五项常规检查尚未全部完成 | `pending` | `skipped_by_policy` | 否 |
| 任一常规检查失败、取消、skip、缺失或身份不匹配 | `failure` / `error` | `skipped_by_policy` | 否 |
| 窄白名单低风险变更且五项当前身份检查全绿 | `success` | `skipped_by_policy` | 是，但仅当该 context 已由 Ruleset 要求且 provider 正确 |
| 受限中风险：已有普通 Python 测试或 Console 展示组件变更，五项当前身份检查全绿 | `success` | `skipped_by_policy` | 是；无需 Cloud Secret，但必须有常规 CI 证据 |
| 高风险变更、五项全绿、没有匹配的可信 candidate | `pending` | `skipped_by_policy` | 否 |
| 高风险 candidate 失败、错身份、缺审批 / Job / step / evidence / receipt | `failure` / `error` | `failed` | 否 |
| 高风险 candidate 的 head、base、merge SHA、CI attempt、main revision 与当前 PR 完全匹配，审批 / 实际 Cloud / artifact 重算全部成功 | `success` | `passed` | 是，但仅当该 context 已由 Ruleset 要求且 provider 正确 |
| main 的 Cloud 实际步骤未执行或 Main Cloud Acceptance receipt 不存在 | Main Cloud Acceptance 不成功 | `blocked_*` / `not_run_*` | 否；正式 i18n 发布也阻断 |

Unknown、API 限流/超时、分页错误、解析错误、未支持文件格式一律不能分类成低/中风险或沿用旧成功状态。Commit status 绑定 head SHA；publisher 同时比对 base SHA 与当前 main，main 更新时必须重算 open PR。

## 风险分类与自动完成条件

普通 PR 始终运行五项常规检查，PR Job 不获取 Langfuse Secrets。策略风险等级只决定是否**额外**要求人工审批的可信 Cloud E2E，不意味着绕过其他 Required Checks：

- **Low**：纯 `docs/**/*.md` 修改（普通文件，非 symlink），以及经过 manifest + pnpm lockfile 闭包验证的 Console 开发依赖更新。
- **Medium**：仅修改已有的、非可执行 `100644` 普通文件，且全部属于 `tests/test_*.py` 普通测试（排除 CI、Cloud、Langfuse、轨迹、发布、认证、凭证、策略、安全与通过率基线测试），或 `services/console/src/components/**` / `pages/**` 的普通 `.tsx` / `.css` 展示文件（排除路径中涉及 auth、Cloud、Credential、Policy、Release、Gate、Secret、Security、Token、Session 等敏感词）。允许混合正常文档，但任何不符合条件的文件会将整个 PR 升级为 High。Medium 仍要求五项全绿，并不代表代码未经审查可直接合并。
- **High**：所有其他情况，包括运行时/backend、Langfuse 连接、凭证、Release Gate、安全、Actions/workflow、`scripts/ci`、Cloud 测试和策略自身、未知或不完整变更。必须取得与当前 PR head/base/main/CI run attempt 精确匹配、人工批准的可信 Cloud receipt。

中风险是逐步放开“无需 Cloud 真实访问仍可完整验证”的严格路径白名单，**不是**把所有业务代码无条件降级为 Medium；扩展白名单需要实际调用影响分析、测试证据及安全评审。普通 UI/测试日常修改可自动得到 `Cloud Integration Policy` Success；其他仍需要人工 Cloud 授权。

在 PR 检查列表中，`Cloud Integration Policy` 是独立 Commit Status，不是 `Langfuse Cloud E2E` Job。高风险状态 Pending 且没有候选运行链接时，其 target URL 会指向 `Trusted Cloud Candidate` 的手动派发页面。点击 `Run workflow`，选择可信 `main`，填入 PR 编号及审核过的完整 head/base SHA，然后通过 `langfuse-e2e-candidate` 的审批，等待 receipt 和 Policy 两个工作流完成。不要把 `skipped_by_policy` 的绿色 job 作为真实 Cloud 覆盖。

### 可信控制器的受控应急修复（非正常合并捷径）

当可信控制器自身存在缺陷，导致正常候选收据**无法被生成**，且修复 PR 因同一控制器无法获得要求的 status 时，采用一次性的管理员灾难恢复流程，而非在代码中放宽 `high`/receipt 判断：

1. 冻结修复 PR 的完整 Head SHA 与当前 Main SHA；独立双人核对差异仅修复控制器缺陷，无扩大 Secrets、Token 或工作流写权限范围。
2. 要求五项常规 CI 在该精确修复 HEAD 全绿，记录 PR、CI run、失败的旧收据 run/job 以及审查结论。
3. 由有权限的仓库管理员按组织批准的例外变更流程执行**一次**受控合并并登记审计记录；不持久加入 Ruleset bypass、不从 Required Checks 移除 Cloud Context，不将此流程用于正常高风险功能变更。
4. 立刻验证修复后的 main CI、真实 Main Cloud Acceptance 与 Policy publisher；在最新 main 上重新对受影响的高风险 PR 派发 Trusted Cloud Candidate，检验三段 Job（prepare / candidate-cloud / receipt）全部成功，以及当前 SHA 的 Policy Status 更新。
5. 恢复常规严格门禁并复查 Ruleset、Environment 审批和专用测试凭证配置。main 或 PR head/base 变动后旧 receipt 不再有效。

历史实例：[PR #188](https://github.com/miniceM/Argus/pull/188) 修复 Actions API workflow path 误判。2026-10-14 经五项常规 CI 全绿后，通过 GitHub 标准合并 API 合入 main（`b7933f8ae6abf7124992ce7b8863136659ce31b4`），未调用 Ruleset bypass API；此前候选 run `38531335635` 的 Cloud job SUCCESS、receipt job FAILURE。尚未取得独立双人审核、远端 Ruleset/Environment 与最新 main Cloud 验收的完整证据，**不得将此次合并描述为经过全部审批的正式破窗流程**。
1. `.github/workflows/ci.yml` 在 PR 上运行五项常规检查：`Code Quality`、`Full Python Tests`、`Docker / Compose Validation`、`Console Quality & E2E`、`S4 Local Langfuse Integration`。PR 的 `Langfuse Cloud E2E` 只报告 `skipped_by_policy`，不读取 Cloud Secrets。
2. `.github/workflows/cloud-integration-policy.yml` 在**可信 main** 上通过 `workflow_run: CI completed` 运行。只读 Evaluate/Publish 负责刷新所有当前 PR 的 `Cloud Integration Policy`。只有专用 Dispatch Job 持有 `actions:write`（无 Langfuse Secrets），在上游 CI 成功且属于当前 main/base、same-repository PR、风险分类为 `high` 后，使用 `GITHUB_TOKEN` 自动 `workflow_dispatch` 候选验证。低风险跳过真实 Cloud；失败、fork、旧 SHA 和不完整身份一律不会派发。
3. `.github/workflows/cloud-e2e-trusted.yml` **只接受由 GitHub Actions bot 触发的候选**；冻结 PR/head/base、测试 merge SHA、CI run/attempt、可信 main workflow revision；再次从 GitHub API 检查。候选 Cloud Job 在单独测试 Environment 中运行真实 Cloud；Environment **不要求人工审批**。候选流程内只授予 API 读权限，并在先完成候选镜像构建后才导入测试凭证。
4. `scripts/ci/cloud_e2e_receipt.py` 由 main 的可信控制代码生成 schema v2 receipt，要求 bot 自动派发身份、当前 PR identity、Prepare/Cloud Job + 必需 Steps success、真实 Cloud artifact（managed-cloud + async-link，v1 2/6，v2 6/6），并记录 `authorization.mode=trusted_ci_automatic` 与 CI run/attempt。**不再读取 GitHub Environment approvals API**；缺少或不匹配任何其他证据仍 fail-closed。
5. `publish_cloud_policy.py` 在候选 Run 完成的 `workflow_run` 回调中重新核对当前 main、PR head/base/merge、CI attempt、candidate artifact 与经过 GitHub API 重算的 receipt，一致才向当前 head 写 `Cloud Integration Policy=success`。旧人工触发收据（v1 或 actor 非 bot）不能绕过新策略。
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
- [ ] 同仓库高风险 PR：不需要手动 dispatch/审批；CI 成功后自动出现 bot 发起的候选 Run，三个 Job 全成功；v2 receipt 的 `authorization.mode=trusted_ci_automatic`，当前 PR 的 Policy 自动成功。
- [ ] 无 Required reviewers 时 `GET /actions/runs/{id}/approvals` 为 `[]` **不影响** Cloud 结果。
- [ ] CI 失败、旧 base/head、fork、伪造 actor、修改工作流路径、错 CI attempt、缺 Cloud 步骤或 evidence 均不能产生成功的 Policy。
- [ ] 管理员在 GitHub 实际确认 Workflow permissions 支持 Dispatch Job 的 `actions:write`，`GITHUB_TOKEN` 的新工作流派发已触发事件；若被组织策略限制，必须在组织权限侧解决，不采用 PAT 硬编码。
- [ ] 确认真实 Ruleset provider、required contexts、Environment Secrets/分支限制和正式 main Cloud 准入均符合文档；未实测不写成已部署。

2026-10-14 的历史 Run #38551530777 曾因没有 Required reviewers 而错误失败；旧 schema v1 强制 approval 的逻辑已由新自动 receipt v2 替代。旧 Run 无法通过重跑升级可信 workflow revision。