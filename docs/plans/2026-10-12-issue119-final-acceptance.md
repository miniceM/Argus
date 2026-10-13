# Issue #119 / #120–#124 最终验收状态

> 状态快照：2026-10-12（Asia/Shanghai）
> 本地已验证源码树：`5cef65e779793b8be04f2c3301188741dbad0757`（#126 主线；包含 #125 后端契约测试、当前 main 集成、三条 P2 修复及首屏状态矩阵）。
> 本文的本地结果不能替代推送后精确 HEAD 的 GitHub CI、Review 与 PR 合并状态；GitHub 生命周期状态应以 PR #126 的实时页面为准。

## 结论

本地合并验收通过：`make validate` 8/8，Console 48 个测试文件 / 782 项通过，Python 763 项通过且 0 skip；全量 Playwright E2E 121/121 通过；PR #125 后端契约专项 5/5 通过。新增首屏矩阵覆盖成功、失败、取消、诊断证据不足、无快照运行中五类状态，并在 1280×800 与 1440×900 两个视口验证决策入口首屏可达。

发布前仍需：将该精确源码树推送至 PR #126，等待同一 head 的 GitHub CI 全绿，回复并解决三条最新 P2 Review；随后由 PR #126 关联并关闭 #119–#124，最后将 PR #125 标记为被取代并关闭。以上 GitHub 写操作截至本地验收记录生成时尚未执行。用户已明确授权按此顺序完成。

## 验收时远端基线（推送前快照）

- **PR #126**：OPEN；当时远端 head `3e9c5aaf1b65767d953052c950bfe30171181cf8`。该 SHA 的 workflow run `38191395127` 为 5/5 成功，但不是本次整合后的 `5cef65e`，不能作为最终同 SHA CI 证据。GitHub 当时报告不可合并；三条最新未解决 P2 评论为 `4244031307`、`4244031312`、`4244031316`。
- **PR #125**：OPEN，原始 head `a46a008d3b8ae67b1d1c993d0e1c97af438867cc`。其有效后端契约测试已通过 `d7001bb` 整合进 #126。独立候选 `f5ebd3a` 只修改另一个 E2E 版本标签断言，不属于本次指定的后端契约测试整合，也不计入本轮验收证据。
- **Issue #119–#124**：推送前均 OPEN；只在 #126 同 SHA CI 与 Review 门禁通过并完成合并后，由 #126 的 `Closes #119` 至 `Closes #124` 关联关闭。

## #126 三条 P2 Review 修复

| 评论 | 修复与回归 | 本地状态 |
|---|---|---|
| `4244031307`：快照导出缺冻结 Manifest | Snapshot detail 返回该 Revision 冻结的 `manifest`；复制/下载绑定经过身份校验的 `(launch_id, snapshot_id)` detail，不从实时 Manifest 拼接。 | **PASS（本地）**；待精确新 head CI 后回复并 resolve。 |
| `4244031312`：旧 Revision 计数伪装为 0 | 目录计数字段保持 nullable；真实冻结零仍是 `0`。Console 依次读取身份匹配的冻结 summary、同修订冻结 items、可信目录值，缺失结论按 UNKNOWN。 | **PASS（本地）**；待精确新 head CI 后回复并 resolve。 |
| `4244031316`：缺失 Manifest schema 版本被猜为 v1.0 | 只显示实际冻结的 `schema_version` / `manifest_version`；均缺失时显示“未记录”。 | **PASS（本地）**；待精确新 head CI 后回复并 resolve。 |

## #119 父 Issue 验收

| EAC | 本地结果 | 证据与边界 |
|---|---|---|
| EAC1 信息架构、首屏与终态布局 | **PASS** | `launch-detail-navigation.spec.ts` 的新增矩阵覆盖成功、失败、取消、证据不足、运行中 × 1280×800 / 1440×900（10 项）；检查首屏 Tab、固定 Revision（有快照时）与质量决策入口，不强制整个内容面板起点处于首屏。全量 Playwright 通过。 |
| EAC2 固定 `(launch_id, snapshot_id)`、S1/S2 隔离 | **PASS** | R05 及快照深链/归属校验回归验证产生 S2 后仍固定 S1、切换时身份一致，无跨 Launch/Revision 缓存污染。 |
| EAC3 Unknown-safe、无伪 PASS/回归 0/成本 0 | **PASS** | Vitest、后端契约及 PostgreSQL 测试覆盖全 UNKNOWN、缺字段、真实 0、不可比与正式/诊断证据边界。 |
| EAC4 Case 筛选、冻结证据与 Trace 定位 | **PASS（本地 UI/API）** | 全量 Playwright、Vitest 与 API 契约覆盖筛选、冻结输入/输出引用、Trace 目标、单侧缺失和失败语义。未把本地路由桩说成线上 Langfuse Observation 实测。 |
| EAC5 审计字段、Revision 与真实 JSON 导出 | **PASS** | JSON 导出、冻结 Manifest、历史 Revision nullable 计数恢复、缺失 schema 未知语义均有回归。 |
| EAC6 Baseline 确认、资格与 CAS 409 | **PASS（本地 API/PostgreSQL）** | 固定 Snapshot、expected revision、409 冲突及数据库锁相关测试通过；使用一次性隔离 PostgreSQL 17 容器。 |
| EAC7 run/cancel/resume/retry 与恢复闭环 | **PASS（本地 UI/API）** | R13 校验 `allowed_actions` 与终态评测恢复进度；后端恢复测试校验所有 sibling evaluations settle 后才物化新 Revision。未声称线上 Worker/Agent 实测。 |
| EAC8 Langfuse 深链与同步状态分离 | **PASS（本地 UI/API 测试）** | 全量 Playwright / Vitest 覆盖链接类型、目标与同步状态语义；没有把本地 fixture 或旧 SHA Cloud E2E 等同于当前 SHA 的线上链路证据。 |
| EAC9 响应式、键盘、焦点与 Token | **PASS** | 全量 Playwright、a11y、视口操作几何与键盘/焦点用例通过；Token lint 通过。 |
| EAC10 子 Issue、契约与门禁 | **本地 PASS；远端交付待完成** | 契约同步、typecheck、Token lint、build、Vitest、Python 与 Playwright 全绿；必须再取得 `5cef65e`（或包含相同测试源码的最终提交）对应 GitHub CI 和 Review 通过证据。 |

## 本地验证记录

| 验证 | 命令 / 结果 | 边界 |
|---|---|---|
| 后端契约专项 | `.venv/bin/python -m pytest -q tests/test_pr125_launch_detail_contract.py`：**5 passed**。 | 已在整合后的 #126 工作树执行。 |
| 仓库主门禁 | `TEST_POSTGRES_URL=postgresql+psycopg://… make validate`：**PASS（8/8）**；Console **48 files / 782 tests**，Python **763 passed / 0 skipped**。 | 使用本地一次性 PostgreSQL 17 容器，绑定回环地址端口 25432，测试结束后已停止并删除；未连接生产数据。OpenAPI 导出、schema 同步、typecheck、Token lint、build 和 Compose 校验均通过。 |
| 首屏矩阵目标测试 | `ARGUS_E2E_API_PORT=29080 ARGUS_E2E_CONSOLE_PORT=29083 pnpm --dir services/console exec playwright test e2e/launch-detail-navigation.spec.ts --grep 'retains a usable decision entry above the fold'`：**10 passed**。 | 隔离端口；该矩阵场景使用 Playwright 路由 fixture。 |
| 全量 Playwright | `ARGUS_E2E_API_PORT=29180 ARGUS_E2E_CONSOLE_PORT=29183 make console-e2e`：**121 passed**。 | 使用独立 Playwright API/Console 服务与 test-mode SQLite；套件中有真实本地 API 零路由 Mock 场景，也有明确使用路由 fixture 的 UI 场景。不是生产 Worker→Agent→Langfuse 全链路证明。 |
| OpenAPI / schema 与 diff hygiene | `make validate` 中导出/生成检查通过；随后 `git diff --check`、缓存 diff 检查通过，`docs/openapi.json` 与 `services/console/src/api/schema.d.ts` 无额外未提交 drift。 | 当前 PR 修改没有未同步的 OpenAPI/schema 变化。 |

已知非阻断警告：既有 Vite bundle-size 建议、`datetime.utcnow()` / Starlette deprecation，以及测试运行器的 npm 环境提示；上述门禁均以成功退出结束。

## 交付顺序

1. 保留 `docs/plans/2026-10-09-pr125-backend-contract-mapping.md` 作为有效字段映射；PR #125 前端/后端历史交接已移至 `docs/plans/archive/2026-10-09/`。
2. 推送 #126 整合提交，并确认 GitHub PR head 与本地已验证源码一致；精确 head 的全部 CI 必须通过。
3. 在 #126 回复并 resolve 三条 P2 Review；确认没有新的未解决阻断意见。
4. 给 PR #126 增加 `Closes #119`、`Closes #120`、`Closes #121`、`Closes #122`、`Closes #123`、`Closes #124`，合并后核实六个 Issue 均关闭。
5. 最后在 PR #125 记录由 #126 取代及最终交付位置，再关闭 PR #125；不合并 #125，避免重复交付。

---

## 2026-10-12 补充：Issue #119 main 剩余差距修复（U1–U6，dated supplement）

> 本补充不覆盖上方的 #126 主线快照；上方历史结果保持原样。本节的"本地 PASS"仅指本工作树实测。
> 实施分支：`codex/fix-issue119-main-acceptance`，基于 `origin/main` == `4b043739c63357ecc10131e1ba54bc1bc53d862a`（与方案审查基准一致）；最终本地 HEAD：`6f5f6a2`。
> 方案来源：`docs/plans/2026-10-12-issue119-main-gap-luna-guide.md`；验收门禁见方案 §8。

### 修复单元与提交

| 提交 | 内容 | RED 证据 | GREEN 证据 |
|---|---|---|---|
| `a211b18` | U1：统一 Comparison / Case 响应身份校验（`launchComparisonQueries.ts`，双元 / 三元身份、身份不符入错误分支不入成功缓存） | 前序会话已验证 RED（源码 stash） | 本 HEAD 全量 Vitest 复跑通过；`comparisonResponseIdentity.test.tsx` 17 项 |
| `3eed3bc` | U2：缺失回归计数 unknown-safe（真实 0 与 missing/null/非法值分开） | 前序会话已验证 RED（源码 stash） | 本 HEAD 全量 Vitest 复跑通过；`LaunchKpiOverview.test.tsx` 新增 8 项 + `Pr126FourthReview` R119-U2 |
| `0c39679` | U3：运行动作按页面会话隔离（点击时冻结目标 Launch，晚响应 / ABA / unmount / 跨 Revision 防重复 POST） | 前序会话已验证 RED | 本 HEAD 全量 Vitest 复跑通过；`launchActionIsolation.test.tsx` 13 项 |
| `257d422` | U4：窄屏紧凑主动作 + 可键盘操作"更多操作"菜单（`LaunchActionMenu.tsx`，单一定义列表驱动两布局） | 本会话实测 RED：stub 菜单下 17/18 行为失败（另有一次 import 不存在 RED） | 本会话实测 GREEN：`launchActionMenu.test.tsx` 18 项 + responsive 窄屏 E2E 12 项 |
| `6f5f6a2` | U5：质量 / 证据 / 资格 / Release Gate 语义分离（冻结质量命名、三态证据徽章、去掉签名与发布依据宣称） | 本会话实测 RED：旧文案下 6/8 失败 | 本会话实测 GREEN：`launchHeaderClaims.test.tsx` 8 项；旧文案断言仅改名，未删任何 fail-closed 断言 |

边界：只修改 `services/console`（5 个 Console 源文件、5 个测试文件、`e2e/responsive.spec.ts`）；未动后端 API、数据库、评分算法、冻结快照语义、Baseline 存储与 Worker 状态机；未引入新 UI 库或平行数据访问体系；`docs/openapi.json` 与 `services/console/src/api/schema.d.ts` 无 drift（`make validate` 步骤 5/6 通过）。

### LG01–LG10 验收矩阵

| 项 | 状态 | 证据与边界 |
|---|---|---|
| LG01（U1 身份校验） | **PASS（本地）** | `comparisonResponseIdentity.test.tsx` 17 项：KPI、分页比较、Case 行内展开与 Drawer 共用 fetch/校验边界；身份缺失/错误响应进入错误分支、可解释可重试，不进入成功缓存。 |
| LG02（U1 缓存形状） | **PASS（本地）** | 同文件：普通查询与分页查询保持不同数据形状；相同 Case 的两消费者同 key 共享；S1/S2 各自缓存；合法空数组保留；无 latest 回退。 |
| LG03（U2 unknown-safe） | **PASS（本地）** | `LaunchKpiOverview.test.tsx` 8 项：missing/null/字符串/负值/小数/非有限 → unknown，仅非负整数为正式值；formal 与 comparable 同时成立才展示数值；真实 0 显示"正式比较未发现回归"；KPI 与回归摘要一致。 |
| LG04（U3 目标隔离） | **PASS（本地）** | `launchActionIsolation.test.tsx` 13 项：run/cancel/resume/retry_failed/retry_evaluation 点击时冻结目标；late success/failure、ABA 返回、unmount 后旧反馈不写新页面；缓存刷新只失效原 Launch。 |
| LG05（U3 请求锁） | **PASS（本地）** | 同文件：同一 Launch 同一动作 pending 中切换 Revision 不重复 POST；旧锁不误释放新请求；恢复轮询与 S2 bounded discovery 保留。 |
| LG06（U4 窄屏菜单） | **PASS（本地）** | `launchActionMenu.test.tsx` 18 项 + responsive.spec 12 项 E2E（360/390/768）：More 键盘打开/导航/选择/Escape/Tab/焦点恢复；菜单权限、pending、确认入口与桌面一致；cancel/retry 确认前 0 POST、确认各 1 POST，目标为 `/api/v1/experiment-launches/{launch_id}/cancel` 与 `…/retry-failed`；不允许动作无可执行项。 |
| LG07（U4 六视口几何） | **PASS（本地）** | 全量 Playwright 124 项：360/390/768 紧凑行 + 菜单浮层实际几何（控件/文本 box、9 点命中、祖先裁剪）通过；1024/1280/1440 两个主动作 32±1px、同行、不换行不退化；`launch-detail-navigation` 首屏矩阵（1280×800 / 1440×900）保持。 |
| LG08（U5 语义） | **PASS（本地）** | `launchHeaderClaims.test.tsx` 8 项：COMPLETE+PASS / COMPLETE+FAIL / DIAGNOSTIC+UNKNOWN / 无快照+RUNNING 文案分离；无"签名已验证 / 门禁准入 / 发布通过 / 可作为 Baseline"宣称；缺质量、缺资格、链接失败不推导 PASS；诊断态"不可作为正式 Baseline"负向声明保留。 |
| LG09（门禁） | **PASS（本地）；1 项 BLOCKED 已记录原因** | typecheck、Vitest（52 文件 / 847 项）、`lint:tokens`、`build`、全量 Playwright（124）、`make validate`（8/8）均通过；后端契约专项 48 项通过。BLOCKED：`make console-e2e` 默认端口 18080/18083 被本机 Docker 占用，改以等价的隔离端口 `ARGUS_E2E_API_PORT=30580 ARGUS_E2E_CONSOLE_PORT=30583 pnpm exec playwright test` 跑同一全量套件。Python 4 项 skip 为 `TEST_POSTGRES_URL` 未配置的真实 PostgreSQL 用例，非失败。 |
| LG10（交付卫生） | **PASS（本地）** | 5 个单一目的 Conventional Commit；`git diff origin/main --check` 干净；diff 仅 Console；无新 Secret / SDK 侵入；OpenAPI 与 schema 无 drift；RED→GREEN、命令、计数与截图见上。 |

### 本地验证记录（本 HEAD 实测）

| 验证 | 命令 / 结果 | 边界 |
|---|---|---|
| Console 类型 / 单测 / Token / 构建 | `pnpm typecheck` 通过；`pnpm test` **52 文件 / 847 项通过**；`pnpm lint:tokens` 通过；`pnpm build` 成功。 | 均为 mock UI 级证据。 |
| 全量 Playwright | `ARGUS_E2E_API_PORT=30580 ARGUS_E2E_CONSOLE_PORT=30583 pnpm --dir services/console exec playwright test`：**124 passed**。 | test-mode SQLite + `vite preview`；含路由 fixture UI 场景与零 Mock 真实本地 API 场景；**不是**生产 Worker→Agent→Langfuse 全链路证据。 |
| 仓库主门禁 | `make validate`：**PASS（8/8）**；Python **759 passed / 4 skipped**（skip 全部为 `TEST_POSTGRES_URL` 未配置）；`docker compose config` OK。 | 使用本机 Python 3.12 venv；未连接生产数据；docker Desktop 占用 18080/18083，未影响 validate。 |
| `make console-test` | `make console-test`：**847 passed（52 文件）**。 | 同上 Console 单测。 |
| 后端契约专项 | `.venv/bin/python -m pytest -q tests/test_pr125_launch_detail_contract.py tests/test_issue_84_eval_only_retry.py tests/test_issue_85_snapshot_revision.py tests/test_api_results_s3.py`：**48 passed**。 | 本地 API/DB 契约；不含真实 PostgreSQL 与真实 Worker/Agent/Langfuse。 |
| 截图证据 | `/tmp/menu-evidence-390-open.png`（390px 菜单展开：更多操作内含 Langfuse 链接、取消评测、重试失败用例、重试评测）；`tmp/menu-evidence-1280-inline.png`（1280px 主动作同行内联）。 | 本地 Chromium headless；截图未入库。 |

### 残留风险

- 断点判定使用 `matchMedia("(min-width: 64rem)")`，浏览器实时切换会同步布局；SSR / 无 matchMedia 环境默认桌面布局（与既有 AppShell 行为一致）。
- 更多操作菜单在超窄视口（≤360px）对超长动作标签采用换行而非截断；如需进一步压缩可再评估，但当前 360/390/768 几何与键盘断言均通过。
- 真实 Worker / Agent / Langfuse 链路未在验证范围内；远端 CI、Review 与 PR 合并不在本地声明之列。

### 远端 CI 结果（PR #134，head `ec3548f`）

推送分支 `codex/fix-issue119-main-acceptance` 后，PR #134 的 5 个 Job 全部通过：Code Quality（26s）、Console Quality & E2E（4m15s，含全量 Playwright）、Docker / Compose Validation（1m19s）、Full Python Tests（1m37s）、Langfuse Cloud E2E（2m40s）。此为推送后同 SHA CI 证据；Review 与合并状态不在本地声明之列。

---

# 补充记录：PR #134 三项 P2 审查缺口修复（2026-10-13）

> 状态快照：2026-10-13（Asia/Shanghai）。本文是追加记录，不修改上方 2026-10-12 的历史验收快照。
> 实施方案：`docs/plans/2026-10-13-pr134-review-fixes-luna-guide.md`（在 aee8 工作树撰写）。
> 实施工作树：`/Users/hosea/.codex/worktrees/409c/Argus`，分支 `codex/fix-pr134-review-gaps`，based on PR #134 head `c1458c9`。
> 最终代码 HEAD：`10b015a0f9704201ba0eeb3a99ac7a30f7bb2a1b`（含 F1 的 StrictMode 反馈修正，见“完成内容”第 1 节“后续修正”）；本验收记录的 docs 提交紧随其后。审查 PR #134 head 仍为 `c1458c9`、base `origin/main` = `4b04373`，未发生变化。
> 本节所有 PASS 均为本地同 HEAD 验证；不能替代推送后精确 HEAD 的 GitHub CI / Review，也不是真实生产 Worker→Agent→Langfuse 全链路结论。

## 完成内容

### 1. F1 统一请求锁生命周期 — `845a1b5` `fix(console): preserve launch action locks across remounts`

QueryClient 级请求锁 store：新增 `launchActionLocks.ts`（模块私有 `WeakMap<QueryClient, Store>`），`begin` 原子获取 `(target,action)` 键锁、`settle` 仅在 owner token 匹配时释放、`useSyncExternalStore` 订阅 primitive 版本快照。`useLaunchActionScope.ts` 改为从当前 provider 取 client，`LaunchActionContext` 新增 `lockOwner` 与 per-mount `viewSessionOwner`，`canWriteFeedback` 增加 mounted + session-owner 校验。锁清理由 mutation 定义级 `onSettled` 完成，不依赖可能随 observer 卸载失效的调用级回调。

**后续修正 — `10b015a` `fix(console): keep launch action feedback after a StrictMode remount`**：`main.tsx` 在 `React.StrictMode` 中挂载整个应用，开发模式下 React 会额外执行一次 mount → unmount → mount；原 `mounted` 标记只在 cleanup 中置 false，导致该模拟卸载永久剥夺**当前活动页面**写反馈的权限，run/cancel/resume/retry_failed/retry_evaluation 的 notice、error 与 Modal 关闭在开发构建中全部静默消失（生产构建只挂载一次，故 E2E 未暴露）。改为在 setup 中重新置 true 即修复，真实 unmount 的隔离语义不变。

- RED：2 项新增用例失败 —— StrictMode 下渲染详情页后，成功 notice 与错误文案均不出现。
- GREEN：`launchActionIsolation` + `launchActionLocks` 25/25；全量 Vitest 895 passed（54 files）；typecheck、lint:tokens、build、全量 Playwright 130、`make validate` 8/8 通过。

- RED：2 项 locks 测试失败（同 client 重挂载锁丢失、反馈隔离缺失）；`/launches` 往返实测观察到第 2 次 POST。
- GREEN：目标测试 23/23；当时全量 Vitest 857 passed（53 files）；typecheck、lint:tokens、build 通过。

### 2. F2 正式比较资格共享判定 — `eab2581` `fix(console): gate formal comparison verdicts and counts on shared eligibility`

根因：`comparability.comparable` 与 `formal.available` 是两个独立资格字段，两个 consumer 各查一部分：

- `comparable=false` + `formal.available=true` + `REGRESSION=3` 时，KPI 已正确扣住数值，摘要却显示“正式比较发现 3 个回归 Case”；
- `comparability` 整个缺失时，KPI 分支链直接落到数值分支，把正式计数（乃至“无回归”零）显示出来。

新增纯函数模块 `comparisonPresentation.ts`，移入 `RegressionCountState` / `readRegressionCount`，并新增严格资格判定 `formalUsable = comparability?.comparable === true AND formal?.available === true`（禁 `Boolean()` 与 `?? true`），以及统一的 verdict 标签 / tone / 说明。KPI、摘要 dot 与说明文案、`comparison-formal-verdict` 裁决块、`comparison-diagnostic-label` 均改为同一 gate；`comparability` 缺失时按 unknown 处理而非“不可比”。真实 0、合法正整数、withheld 解释、合法空数组、普通查询与分页查询的缓存形状均不变；未改动任何服务端比较、评分或 Baseline 资格规则。

- RED（只 stash 两个源文件后同命令复跑）：**5 failed** —— KPI 2 项（comparability absent / null）+ Tab 3 项（false comparable 的 KPI 与摘要、comparability 未返回、缺失资格不得变成绿色零）。
- GREEN：目标测试 37/37；全量 Vitest 883 passed（54 files）；typecheck、lint:tokens、build 通过。

### 3. F3 空菜单键盘可关闭 — `91e9527` `fix(console): restore keyboard dismissal for empty action menus`

根因：菜单只含 note / 全 pending / 无任何条目时，不存在可聚焦的 `role=menuitem`，旧 open effect 未聚焦任何元素，菜单子树收不到 keydown，Escape 无法关闭浮层；trigger 本身也没有 Escape 路径。

- 菜单容器加 `tabIndex=-1` 作为程序聚焦兜底；打开时聚焦首个 enabled item，否则聚焦容器。
- Escape 在容器/trigger 路径均可处理；`open=false` 时不消费 Escape，避免误关页面或 Modal 的 Escape。
- 内容变化按内容身份修复失效焦点（被移除或转为 pending 的条目），普通 rerender 不抢用户已导航的焦点。
- note 仍不是 menuitem，pending 项仍 disabled；placement、断点、`allowed_actions`、标签、force replay 确认、Modal 焦点移交全部不变，未引入新 UI 库或新 Token。

- RED（只 stash 组件后同命令复跑）：**6 项单测失败**（焦点兜底 ×3、trigger 路径 Escape、焦点修复 ×2）；**6 个真实浏览器 E2E 场景失败**（360/390/768px × note-only / all-pending，均报 focus 未落到菜单容器）。
- GREEN：菜单测试 28/28；全量 Vitest 893 passed（54 files）；`e2e/responsive.spec.ts` 43/43（重新 build 当前源码后）；typecheck、lint:tokens、build 通过。

## 本地验证记录（同一 HEAD `10b015a`）

> 上列各修复单元 GREEN 中的全量 Vitest 数字是**该单元提交时**的快照；本节的门禁结果均为最终 HEAD `10b015a` 同 HEAD 复跑。

| 验证 | 命令 / 结果 | 边界 |
|---|---|---|
| Console 单测 | `pnpm --dir services/console test`：**54 files / 895 tests passed** | 相对 `c1458c9` 的 52/847 净增 48 项断言，无删除或放宽。 |
| typecheck | `pnpm --dir services/console typecheck`：**PASS** | — |
| Token lint | `pnpm --dir services/console lint:tokens`：**PASS** | 未新增裸十六进制色值或 Tailwind 任意颜色类。 |
| build | `pnpm --dir services/console build`：**PASS** | test:e2e 之前已重新 build 当前源码。 |
| `make console-test` | **PASS**（等价 Console 单测） | — |
| 全量 Playwright | `ARGUS_E2E_API_PORT=31580 ARGUS_E2E_CONSOLE_PORT=31583 make console-e2e`：**130 passed** | 隔离端口（已先 `lsof` 确认空闲）；Playwright 自带隔离 API + test-mode SQLite。含 mock 路由 fixture 场景与真实本地 API 场景，但**不是**生产 Worker→Agent→Langfuse 全链路证明。 |
| `make validate` | `PATH="$PWD/.venv/bin:$PATH" make validate`：**PASS（8/8）**；Python **759 passed, 4 skipped** | 4 个 skip 均为 `TEST_POSTGRES_URL` 未配置的真实 PostgreSQL 用例，属本机环境限制；其余含 OpenAPI 导出、schema 同步、typecheck、Token lint、build、Compose 校验全部通过。 |
| 契约与 diff 卫生 | `git diff --check`、`git status --short`：**clean** | `docs/openapi.json` 与 `services/console/src/api/schema.d.ts` 无 drift，本任务没有 API / schema / DB / 评分 / Worker 状态机变更。 |

## 证据分层

| 层 | 覆盖 | 结论 |
|---|---|---|
| mock UI（Vitest / jsdom） | F1 锁与反馈隔离（含 StrictMode 重挂载反馈回归）、F2 资格矩阵与两个 consumer、F3 焦点与键盘路径 | **已验证** |
| mock 浏览器（Playwright + 路由 fixture） | F3 真实 Chrome 焦点与键盘 Escape / Tab；六视口几何与菜单可达性 | **已验证** |
| 隔离真实本地 API（Playwright 自带） | 130 项 E2E 中的真实 API / test-mode SQLite 场景 | **已验证（本地隔离环境）** |
| 真实 Worker→Agent→Langfuse 生产链路 | — | **未执行 / NOT RUN**，本任务也未声称 |
| 远端 CI 与 Review（head `91e9527`） | — | **未查询**：分支 `codex/fix-pr134-review-gaps` 未推送，未获推送/评论/merge 授权 |

## R134-01 – R134-10 验收矩阵

| AC | 可观察通过条件 | 本地结果 |
|---|---|---|
| R134-01 | 同目标/action pending 经详情→列表→详情仍仅 1 POST；settle 前按钮 disabled | **PASS** | `launchActionIsolation.test.tsx` 的 `/launches` 往返场景（F1，`845a1b5`）：deferred POST 未 settle 时再次点击，总 POST 仍为 1，返回页面按钮 disabled。 |
| R134-02 | 五动作共享同一锁规则；不同 Launch/QueryClient 隔离；旧 settle 不释放新锁 | **PASS** | `launchActionLocks.test.tsx` 7 项：五动作参数化、同 client 重挂载共享锁、不同 Launch/action 不串锁、不同 QueryClient 隔离、owner-only release、卸载后仍可释放、重挂载不收旧反馈。 |
| R134-03 | 重挂载后旧 notice/error/modal 回调不污染新页面；原目标刷新、S1 保持、S2 discovery 不变 | **PASS** | F1 测试覆盖旧实例不写新页面 notice/error、不关闭新 modal；原有 S1 固定、S2 发现与恢复轮询回归未删除且通过。 |
| R134-04 | false/missing comparability 或 unavailable formal 时，KPI 与摘要都 withheld 正式数值/verdict/tone | **PASS** | F2 RED→GREEN 的 5 项负向断言 + `comparisonPresentation.test.ts` 的资格矩阵（`=== true`，拒 `1`/`"true"`，null/undefined 一律 withheld）。 |
| R134-05 | 两资格 true 时真实 0/正整数正常；非法/缺计数不补 0；质量结论与发布准入仍分离 | **PASS** | 真实 0 → "正式比较未发现回归"；真实 3 → "3 项性能退化" 与“正式比较发现 3 个回归 Case”；`"0"`/`null`/`-1`/`0.5`/NaN/Infinity → "不可用"；冻结质量门禁与 Baseline 相对变化文案分离未变。 |
| R134-06 | note-only/empty/all-pending 菜单键盘 Escape 关闭并恢复焦点，动态 items 有兜底且普通 rerender 不抢焦点 | **PASS** | F3：真实 Chrome 于 360/390/768px × note-only / all-pending 验证 Escape 关闭、焦点回到 trigger；单测覆盖条目被移除 / 转 pending 时的焦点兜底与重建 items 不移焦点。 |
| R134-07 | 正常菜单导航、Tab/Shift+Tab、确认请求数/force、Modal handoff、六视口几何全部保留 | **PASS** | `launchActionMenu.test.tsx` 原 18 项菜单契约测试保留通过；`e2e/responsive.spec.ts` 43/43（含 360/390/768 可达性与 1024/1280/1440 桌面 32px±1px 同行几何）；确认前 0 POST / 确认 1 POST 与 force replay 用例通过；真实浏览器 Tab 关闭且焦点到菜单外控件。 |
| R134-08 | U1–U5 已有回归断言保留，目标测试与全量 Console 测试通过 | **PASS** | `comparisonResponseIdentity`、`launchActionIsolation`、`launchActionLocks`、`comparisonPresentation`、`launchActionMenu` 均无删除断言；全量 54 files / 895 tests 通过（含 StrictMode 反馈回归 2 项）。 |
| R134-09 | typecheck、lint:tokens、build、全量 Playwright、make validate 通过或真实阻塞逐项说明 | **PASS** | 见“本地验证记录”；唯一环境限制为 4 个 TEST_POSTGRES_URL skip，已逐项说明。 |
| R134-10 | 无 API/schema/数据库/评分/Worker 语义漂移；只有任务相关 diff；每单元本地原子 commit 与实际证据可追溯 | **PASS** | `git diff c1458c9..HEAD` 仅 13 个文件（Console 源码 + 测试 + 一个 E2E spec），共 +1248/-127；三个 fix 各自原子提交；OpenAPI/schema 无 drift。 |

## 交付与提交清单

分支 `codex/fix-pr134-review-gaps`（based on PR #134 head `c1458c9`），本地 6 个 Conventional Commit：

| Commit | 类别 | 内容 |
|---|---|---|
| `845a1b5` | fix | F1：QueryClient 作用域请求锁 store，锁跨重挂载存活且仅 owner 释放 |
| `eab2581` | fix | F2：正式比较资格/计数共享判定（`comparisonPresentation.ts`） |
| `91e9527` | fix | F3：空菜单键盘关闭与焦点兜底 |
| `6e85a1d` | docs | 本 PR #134 审查缺口验收记录（dated supplement） |
| `10b015a` | fix | F1 后续修正：StrictMode 重挂载后仍可写运行动作反馈 |
| `67323ba` | docs | 记录 StrictMode 修正与本交付清单（最终 HEAD；CI run `38342450008` 于该 SHA 5/5 通过） |

交付：**PR #135**（https://github.com/miniceM/Argus/pull/135），base 为 PR #134 的分支 `codex/fix-issue119-main-acceptance`（`c1458c9`），因此 diff 只含三项审查缺口的修复；合并后 PR #134 自动更新为含修复的 head。

## 未提交/未授权事项与剩余风险

1. **交付形态与远端 CI**：分支 `codex/fix-pr134-review-gaps` 已推送，并开为 **PR #135**（base = PR #134 的 `codex/fix-issue119-main-acceptance`，即 `c1458c9`），状态 OPEN / MERGEABLE。代码 head `10b015a`（run `38341654775`）与最终 HEAD `67323ba`（run `38342450008`）均 **5/5 Job 通过**（Code Quality、Full Python Tests、Docker/Compose、Console Quality & E2E、Langfuse Cloud E2E）。截至 2026-10-13 核对，PR #135 尚无 Review 评论或 inline thread 需要回复；亦未 resolve / merge（需另行授权）。更早的 5 Job 绿色记录属于 `c1458c9`，不能代表本 HEAD。
2. **真实全链路未验证**：本次证据止于本地 mock UI、mock 浏览器与隔离真实本地 API；未执行真实 Worker→Agent→Langfuse 生产链路，远端 CI 也不构成该链路的证明。
3. **`ComparisonReport.tsx` 残留**：该组件的正式裁决块仍只 gate `formal.available`。它当前**不在** `src/app/router.tsx` 的任何路由中（仅被其自身测试引用），且不含回归计数句子，因此不在本方案声明的修改范围内。若后续接入路由，应按 `comparisonPresentation.ts` 的同一 gate 收口。
4. **前端锁的边界**：跨浏览器 reload、跨 Tab、跨设备、跨 QueryClient 不共享前端锁（方案 §6.1 明确不承诺）；相关边界由后端幂等保障。
5. **锁的快照依赖**：F1/F2/F3 均未引入超时任务管理；请求长期 pending 时 UI 保持 pending。
6. **4 个 PostgreSQL 用例 skip**：`TEST_POSTGRES_URL` 未配置，属本机环境限制，不是回归。
