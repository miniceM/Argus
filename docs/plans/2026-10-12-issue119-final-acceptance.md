# Issue #119 / #120–#124 最终验收状态

> 状态快照：2026-10-12（Asia/Shanghai）
> 本地验收树：`db7c01f`（`origin/main` 已本地合入；后续本文档提交不改变被测代码）
> GitHub 远端与本地工作区分别记录，不能用本地绿灯替代远端 PR/Issue 状态。

## 结论

LaunchDetail 重构及 PR #126 第四轮审查修正已在本地实现；当前集成树通过 `make validate` 与全量 `make console-e2e`。但父 Issue 的最终交付状态仍为 **BLOCKED**：本地修复尚未推送；远端 #126 仍有本轮新增 P2 review thread 未解决且当前 PR 状态不可合并；最新本地代码还没有对应的 GitHub CI。#119–#124 在 GitHub 均仍为 OPEN。

远端 PR 快照：

- #125：OPEN，head `a46a008`，GitHub 报告 mergeable；CI run `37874732546` 未全绿。Code Quality、Full Python Tests、Docker / Compose Validation 通过；Console Quality & E2E 在 `launch-detail-navigation.spec.ts:106` 断言精确文本 `v1` 失败（页面呈现 `v1 (Candidate)`）；Langfuse Cloud E2E 为 skipped。该失败与此次 #126 本地修复及新增后端契约测试分开处理。
- #126：OPEN，远端 head `3e9c5aa`，GitHub 报告不可合并。run `38191395127` 在该 head 上 5/5 jobs 成功，但其后新增的三条第四轮 P2 review 尚未解决。当前本地 `3d3f539` 已修复这三项；本地合入 `origin/main` 的 merge commit 为 `db7c01f`，以上提交均未推送。

## #119 父 Issue 的 EAC 状态

| EAC | 状态 | 本轮证据与未覆盖边界 |
|---|---|---|
| EAC1 信息架构、首屏与终态布局 | **BLOCKED** | 三 Tab、操作入口和响应式行为已实现；E2E 覆盖 360/390/768px 及 1024/1280/1440px 操作几何。但没有独立证据证明 1280×800、1440×900 下所有目标状态均满足“首屏无需滚动进入对比/Case”。需补精确视口的业务内容几何断言或可复核实测。 |
| EAC2 固定 `(launch_id, snapshot_id)`、S1/S2 隔离 | **PASS（本地）** | `pr126-r05-r13.spec.ts` 的 R05 断言重评发现 S2 后 URL 和报告继续固定 S1，且不重复请求 S1 详情；历史归属、固定链接及契约测试纳入本地套件。 |
| EAC3 Unknown-safe、无伪 PASS/回归 0/成本 0 | **PASS（本地）** | Console 语义回归、后端快照/统计契约测试及 `test_pr125_launch_detail_contract.py` 通过；真实 PostgreSQL 专项未运行（`TEST_POSTGRES_URL` 未配置）。 |
| EAC4 Case 筛选、冻结证据与 Trace 定位 | **PASS（本地 UI/API 测试）** | 页面测试验证冻结条目、筛选、输出状态与快照身份隔离；此次浏览器用例不能代替真实 Langfuse Observation/Trace 服务验证。 |
| EAC5 审计字段、Revision 与真实 JSON 导出 | **PASS（本地）** | Playwright 校验 JSON 文件名、可解析内容及选定 Revision；本地修复补入冻结 Manifest，恢复旧 Revision 缺失计数语义，并让缺失 schema 版本显示未知而非猜测。远端 review thread 尚未因本地修复自动关闭。 |
| EAC6 Baseline 确认、资格与 CAS 409 | **PASS（本地契约/组件测试）** | 测试覆盖固定 Snapshot、expected revision、409 冲突刷新/阻止旧 revision 重提；真实 PostgreSQL 行锁专项仍为 skipped。 |
| EAC7 run/cancel/resume/retry 与恢复闭环 | **PASS（本地 UI/API 测试）** | R13 验证操作受 `allowed_actions` 限制及终态评测恢复进度；后端 recovery 测试验证所有评测 settled 后再物化新 Revision。未将 mock UI 测试描述为线上 Worker/Agent 实测。 |
| EAC8 Langfuse 深链与多类同步状态独立 | **BLOCKED（当前本地树）** | 远端 #126 的 `38191395127` 在旧 head `3e9c5aa` 上 Langfuse Cloud E2E 通过；当前本地新增修复/合并 main 后没有新的远端 CI，不能把旧 head 结果当作当前树的全链路证据。 |
| EAC9 响应式、键盘、焦点与 Token | **PASS（本地）** | `make console-e2e` 中 a11y 与 responsive 用例通过；`make validate` 的 Token lint 通过。精确首屏验收单独列于 EAC1，不能由无横向溢出断言替代。 |
| EAC10 子 Issue、API/契约及交付门禁 | **BLOCKED（总体验收）** | 本地 `make validate` 与 Playwright 全量通过；最新本地树未推送、无同 SHA GitHub CI；4 项 PostgreSQL 测试跳过；真实 Worker/Agent 的部署链路未在本轮单独实测。 |

## 子 Issue 进度

| Issue | 本地功能与自动化证据 | 当前交付状态 |
|---|---|---|
| #120 页面骨架、三 Tab、URL/键盘/响应式 | **PASS（本地）**：导航/深链、键盘与响应式 E2E 通过。精确首屏矩阵仍受 EAC1 限制。 | **BLOCKED**：Issue 仍 OPEN；代码未推送到对应 PR。 |
| #121 KPI、比较与 Unknown-safe 口径 | **PASS（本地）**：Vitest、Python 契约及统一验证通过，缺失值、UNKNOWN、真实 0 与指标方向保留独立语义。 | **BLOCKED**：Issue 仍 OPEN；需同 SHA CI/PR 生命周期完成。 |
| #122 Case 筛选、冻结证据与 Trace | **PASS（本地 UI/API 测试）**：固定修订行、筛选、失败状态和 Trace 链接回归通过。 | **BLOCKED**：真实 Langfuse 输出/Trace 服务端链路未在当前本地树实测；Issue 仍 OPEN。 |
| #123 审计、Revision 与 JSON | **PASS（本地）**：详情校验、Manifest、历史计数、schema 版本语义和下载回归通过。 | **BLOCKED**：三条最新远端 P2 review 尚未确认解决，Issue 仍 OPEN。 |
| #124 动作、终态进度与 Baseline CAS | **PASS（本地 UI/API 测试）**：R13、409、allowed_actions 和评测恢复的自动化回归通过。 | **BLOCKED**：当前版本没有真实 Worker/Agent 线上链路验收；Issue 仍 OPEN。 |

## 本地验证结果

| 验证 | 结果 | 边界 |
|---|---|---|
| `make validate` | **PASS**：8/8；Console 48 files / 782 tests；Python `759 passed, 4 skipped`；OpenAPI/schema 同步、typecheck、Token lint、build、Compose 校验通过。 | 4 项跳过均因 `TEST_POSTGRES_URL` 未配置。存在既有 Vite bundle-size 提示和 Python/Starlette deprecation warnings。 |
| `ARGUS_E2E_API_PORT=28080 ARGUS_E2E_CONSOLE_PORT=28083 make console-e2e` | **PASS**：111 passed。 | 隔离端口；LaunchDetail 的 R05/R13 使用 Playwright 路由测试桩。此结果不是线上 Agent/Worker/Langfuse 全链路证明。 |
| `tests/test_pr125_launch_detail_contract.py` | **PASS（5 项）**，并包含在上述 `make validate` 的 Python 全量测试中。 | 验证 Runner API 冻结快照/比较契约，不等同真实 PostgreSQL 锁测试。 |

## 剩余工作与边界

1. 单独补齐并运行 1280×800、1440×900 的首屏决策可达性断言，覆盖不同 Launch/结果状态。
2. 在允许推送后，将当前本地分支更新推送到 PR #126，运行当前 SHA 的 GitHub 全量 CI；逐条确认三条 P2 review 后再处理 review 状态。
3. 单独决定 PR #125 的 Console E2E 版本标签断言归属与修复，不把其失败归因到后端契约测试；检查 PR 重叠后再做合并/关闭决策。
4. 配置 PostgreSQL 测试环境并运行 4 项 skipped 测试；按需要补充当前 revision 的真实 Worker/Agent/Langfuse 验收证据。
5. 在以上门禁完成前保持 #119–#124 OPEN；本轮没有推送、合并远端 PR、回复/解决 review thread 或关闭 Issue。
