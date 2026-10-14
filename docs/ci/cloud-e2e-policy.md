# Cloud E2E 凭证、合并策略与发布门禁

本文是 Argus GitHub Actions Cloud E2E 的运行手册。它描述仓库代码实现的准入行为；工作流代码本身**不会**自动配置 GitHub Environments、Secrets 或 Ruleset。未完成管理员配置和真实远端验收前，不得把本地测试结果表述为线上门禁已生效。

## 工作流职责

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

历史实例：[PR #188](https://github.com/miniceM/Argus/pull/188) 修复 Actions API workflow path 误判。2026-10-14 经五项常规 CI 全绿后，采用受控恢复合并（main `b7933f8ae6abf7124992ce7b8863136659ce31b4`）；此前候选 run `38531335635` 的 Cloud job SUCCESS、receipt job FAILURE。此记录只说明控制器源码恢复，不能代替后续 Main Cloud / Environment / Ruleset 的真实远端验收。

## 管理员配置清单

1. 在 `langfuse-e2e` Environment 配置 `LANGFUSE_BASE_URL`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`，均指向专用测试 project；设置仓库变量 `LANGFUSE_E2E_ENABLED=true`。主 Cloud 工作流保留缺值 fail-closed 行为。
2. 单独配置 `langfuse-e2e-candidate` Environment 的同名测试 project secrets，启用所需 reviewer，并将允许部署的 branch/ref 限制到 `main`。不要复制生产密钥，不要把 secret 写入仓库变量或 PR job 环境。
3. 审核并保护 `.github/workflows/**`、`scripts/ci/**` 的 main 变更。候选 Environment 的 reviewer 必须逐项确认 PR、head SHA、base SHA、merge SHA、CI run/attempt、风险分类和测试 project 身份。
4. Ruleset bootstrap 必须无合并门禁空窗：先让五项常规 CI 与 `Cloud Integration Policy` 在真实 PR 上可观察且已为现存 open PR 初始化，再添加新 required contexts；确认无误后才移除旧 PR Cloud context `Langfuse Cloud E2E`。保留 strict、禁止删除 / force-push 和无 bypass 约束。不得仅因某检查显示 success/skipped 就将其视作真实 Cloud 证据。
5. `workflow_dispatch` 候选验证的命令输入必须来自人工审查过的完整 SHA；验证后任何 head/base/main 更新都使 receipt 失效。重跑必须核验最新 run attempt，不能借用旧 attempt 成功。
6. `Cloud Integration Policy` 是 GitHub Commit Status context。必须确认 Ruleset 中绑定正确 provider/integration；若仓库威胁模型要求与 `GITHUB_TOKEN` 区分的身份，应由管理员单独提供专用 gate App，而不是在工作流中创建或硬编码 token。
7. 对 i18n 发布，main push 会等待同 SHA 的 CI。手动发布前应先确保该 SHA 有成功的 main CI / Cloud acceptance；不得用 run-level conclusion、旧 SHA receipt 或人工跳过 gate 放行。

### 2026-10-13 远端配置基线（只读核对）

2026-10-13 对 `miniceM/Argus` 做只读核对，远端 `main` 为 `560af37f68c102ca4f70916e48e1a3a8eef43ed3`。`main-quality-gate`（Ruleset ID `23715475`）为 active、strict、无 bypass actor；required contexts 仍是 `Full Python Tests`、`Code Quality`、`Docker / Compose Validation`、`Langfuse Cloud E2E`，尚无 `Console Quality & E2E` 或 `Cloud Integration Policy`。

同次核对发现：仓库变量 `LANGFUSE_E2E_ENABLED=true`；`langfuse-e2e` Environment 存在，但 `reviewers` 和 `deployment_branch_policy` 均未配置。只读 Secrets API 仅确认该 Environment 有 `LANGFUSE_BASE_URL`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` 三个名称；**未读取或导出任何 Secret 值，因此无法确认它们指向专用测试 project**。`langfuse-e2e-candidate` Environment 不存在（API 404）。本轮未修改 Ruleset、Environment、变量或 Secrets。管理员 rollout 前仍须重新读取；本段是时点记录，不是长期事实。

## 脱敏验收与范围限制

每次真实验收记录 repository、精确 SHA、workflow/run/attempt/job 链接、CI 五项状态、risk、Cloud 状态、审批身份、baseline (`v1 2/6`、`v2 6/6`)、receipt artifact、Ruleset 前后 required context、Environment 限制与执行者。不要复制 Secret 值、Cookie 或未经脱敏的 Langfuse PII。

Managed Cloud E2E 只覆盖其实际测试路径；它不替代 SDK 版本、持久化 / migration、消息与队列、Agent 协议、自托管 Langfuse 镜像升级和部署回滚的专项验收。尤其是 `langfuse-i18n` 发布仍须通过其独立自托管验收；通用 Cloud receipt 不能证明镜像兼容或迁移安全。

U6 的远端动作（人工 Environment / Ruleset 配置、真实 Dependabot actor、真实 Cloud、Secrets 负向验证、PR mergeability 与发布敏感性）需管理员授权并产生实际链接或脱敏配置证据。未执行项应标为 `BLOCKED` 或 `NOT RUN`，不得用本地 fixture、YAML 静态测试或历史 run 代替。
