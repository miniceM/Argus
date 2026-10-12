# Issue #119 / #120–#124 最终验收状态

> 状态快照：2026-10-12（Asia/Shanghai）
> PR #126 本地验收代码：`62ee7d7e150b10cb0596486646d321f0580d64fe`（包含本地合入 `origin/main` 的 `db7c01f`；本文档提交不改变被测代码）。
> PR #125 本地 E2E 候选：`f5ebd3a110fea95c6defea1cfac3167453f8d7a2`，基于远端 head `a46a008`，仅修复候选版本标签断言。
> GitHub 远端与本地工作区分别记录；本地通过不能替代同 SHA 远端 CI、Review 或真实服务链路。

## 结论

本地已完成 #126 三项新审查意见的修复及针对性复核；精确首屏用例在 1280×800、1440×900 通过；使用一次性 PostgreSQL 17 容器后，`make validate` 全部通过且 4 项 PostgreSQL 测试不再跳过。PR #125 的旧 E2E 断言已在独立本地分支完成 RED→GREEN，并且该候选分支的完整 Console E2E 通过。

**总体远端交付仍为 BLOCKED**：这些本地提交未推送；PR #126 远端仍有 3 条未解决 P2 review 且 GitHub 判定不可合并；最新本地代码没有对应 GitHub CI。#119–#124 仍全部 OPEN。本地 UI/API 与测试桩也不等于真实 Worker→Agent→Langfuse 全链路验收。

## 远端状态（2026-10-12 实时复核）

- **PR #125**：OPEN，head `a46a008d3b8ae67b1d1c993d0e1c97af438867cc`，GitHub 判定 mergeable。CI run `37874732546` 未全绿：Console E2E 的 `launch-detail-navigation.spec.ts:106` 期待精确文本 `v1`，实际页面标签为 `v1 (Candidate)`；Langfuse Cloud E2E skipped。独立本地提交 `f5ebd3a` 更新了断言并运行完整 Console E2E（86 passed），**尚未推送**。
- **PR #126**：OPEN，远端 head `3e9c5aaf1b65767d953052c950bfe30171181cf8`，GitHub 判定不可合并。CI run `38191395127` 是该旧 head 的 5/5 jobs 通过，不代表当前本地代码。远端 3 条最新 P2 review（评论 ID `4244031307`、`4244031312`、`4244031316`）仍未解决；本地 `3d3f539` 已实现对应修复；被测代码 SHA 为 `62ee7d7`，当前 HEAD `91493f8` 仅更新本文档，均未推送。

## #126 新增 P2 Review 的本地状态

| 评论 | 修复与本地回归 | GitHub 状态 |
|---|---|---|
| `4244031307`：快照导出缺冻结 Manifest | detail API 返回记录自身冻结的 `manifest`；Audit 复制/下载使用匹配 `(launch_id, snapshot_id)` 的详情；API/OpenAPI/schema、fixture 与导出回归已同步。 | 未解决；等待授权推送及对应 SHA CI 后回复/resolve。 |
| `4244031312`：旧 Revision 计数被伪装为 0 | 目录缺失计数保持 nullable；优先冻结 summary，其次同 Revision 的 items，再使用可信目录值；真实 0 保留，UNKNOWN/缺失不误算。 | 未解决；等待授权推送及对应 SHA CI 后回复/resolve。 |
| `4244031316`：缺 schema 版本被猜成 v1.0 | 仅使用冻结 Manifest 中实际记录的 `schema_version` / `manifest_version`；都缺失时显示“Schema 版本未记录”。 | 未解决；等待授权推送及对应 SHA CI 后回复/resolve。 |

## #119 父 Issue 验收状态

| EAC | 状态 | 本轮证据与未覆盖边界 |
|---|---|---|
| EAC1 信息架构、首屏与终态布局 | **BLOCKED（精确视口子项 PASS）** | 当前本地 Playwright 用例在 1280×800、1440×900 校验固定 `snap-v2`（Revision 2）的 Cases 首屏：`scrollY=0`，三 Tab 与回归 KPI 入口完整可见，点击后固定 `snapshot_id` 导航至 Compare；两尺寸均通过。此证据基于路由桩且只覆盖该场景，未覆盖父 Issue 要求的成功/失败/取消/证据不足/运行中全状态首屏组合。 |
| EAC2 固定 `(launch_id, snapshot_id)`、S1/S2 隔离 | **PASS（本地）** | `pr126-r05-r13.spec.ts` 的 R05 验证重评发现 S2 后 URL/报告仍固定 S1、发现后不重复请求 S1；固定链接、归属校验及契约回归纳入当前树测试。 |
| EAC3 Unknown-safe、无伪 PASS/回归 0/成本 0 | **PASS（本地）** | 全量 Vitest、Python 契约与 PostgreSQL 实测均通过；全 UNKNOWN、缺字段、真实 0 与正式/诊断证据边界有回归覆盖。 |
| EAC4 Case 筛选、冻结证据与 Trace 定位 | **PASS（本地 UI/API 测试）** | 固定修订、Case 筛选/展开、输出状态和 Trace 目标有自动化覆盖；真实 Langfuse Observation/Trace 服务端链路未在本轮运行。 |
| EAC5 审计字段、Revision 与真实 JSON 导出 | **PASS（本地）** | 选定 Revision 的下载可解析；本地修复增加冻结 Manifest、旧 Revision 计数恢复和缺失 schema 的未知语义。远端 Review 仍未解决。 |
| EAC6 Baseline 确认、资格与 CAS 409 | **PASS（本地契约/组件/PostgreSQL 测试）** | 固定 Snapshot、expected revision、409 冲突与行锁相关测试通过；测试 PostgreSQL 为一次性本地容器。 |
| EAC7 run/cancel/resume/retry 与恢复闭环 | **PASS（本地 UI/API 测试）** | R13 验证 `allowed_actions` 与终态评测恢复进度；Python recovery 测试验证 sibling evaluation settled 后才物化新 Revision。未声称线上 Worker/Agent 实测。 |
| EAC8 Langfuse 深链与多类同步状态独立 | **BLOCKED（当前本地树）** | 自动化覆盖同步状态隔离与真实目标类型；当前本地修复/集成树没有同 SHA GitHub Cloud E2E。远端旧 run 的 Langfuse job 状态不能替代本地新代码证据。 |
| EAC9 响应式、键盘、焦点与 Token | **PASS（本地）** | 当前 `make console-e2e` 通过；覆盖响应式操作几何、键盘、焦点恢复及无障碍；`make validate` 的 Token lint 通过。EAC1 的全状态精确首屏矩阵另行阻塞。 |
| EAC10 子 Issue、API/契约及交付门禁 | **BLOCKED（远端交付）** | 本地 `make validate` 8/8，Python 763 passed / 0 skipped，Console 48 files / 782 tests；Console E2E 111 passed。但当前 HEAD 未推送、无同 SHA GitHub CI，真实 Worker/Agent/Langfuse 端到端未单独验收。 |

## 子 Issue 进度

| Issue | 本地功能与自动化证据 | 当前交付状态 |
|---|---|---|
| #120 页面骨架、三 Tab、URL/键盘/响应式 | **PASS（本地）**：导航/深链/键盘/响应式通过；1280×800 与 1440×900 有固定 Revision 首屏实测。 | **BLOCKED**：完整状态矩阵未覆盖；Issue 仍 OPEN，代码未推送。 |
| #121 KPI、比较与 Unknown-safe 口径 | **PASS（本地）**：Vitest、Python 契约与 PostgreSQL 测试通过，缺失值、UNKNOWN、真实 0 和指标方向分开处理。 | **BLOCKED**：Issue 仍 OPEN；需同 SHA 远端 CI 与 PR 生命周期完成。 |
| #122 Case 筛选、冻结证据与 Trace | **PASS（本地 UI/API 测试）**：固定修订行、筛选、失败状态和 Trace 链接回归通过。 | **BLOCKED**：真实 Langfuse 输出/Trace 链路未在当前树实测；Issue 仍 OPEN。 |
| #123 审计、Revision 与 JSON | **PASS（本地）**：Manifest、历史计数、schema 未知语义和 JSON 导出回归通过。 | **BLOCKED**：三条最新远端 P2 review 未解决；Issue 仍 OPEN。 |
| #124 动作、终态进度与 Baseline CAS | **PASS（本地 UI/API/PostgreSQL 测试）**：R13、409、`allowed_actions`、恢复和 PostgreSQL 锁测试通过。 | **BLOCKED**：真实 Worker/Agent 线上链路未验收；Issue 仍 OPEN。 |

## 本地验证结果

| 验证 | 结果 | 边界 |
|---|---|---|
| `TEST_POSTGRES_URL=<一次性 Postgres 17> make validate` | **PASS**：8/8；Console 48 files / 782 tests；Python `763 passed, 0 skipped`；OpenAPI/schema 同步、typecheck、Token lint、build、Compose 校验通过。 | PostgreSQL 仅为本地一次性容器，测试结束后已停止并删除；未使用生产数据。保留既有 Vite bundle-size 与 datetime/Starlette deprecation warnings。 |
| `ARGUS_E2E_API_PORT=28080 ARGUS_E2E_CONSOLE_PORT=28083 make console-e2e`（#126 本地 HEAD `62ee7d7`） | **PASS**：111 passed。 | 隔离端口；LaunchDetail R05/R13 及首屏用例使用 Playwright 路由测试桩。不是线上 Agent/Worker/Langfuse 全链路证明。 |
| `ARGUS_E2E_API_PORT=28135 ARGUS_E2E_CONSOLE_PORT=28136 make console-e2e`（#125 候选 `f5ebd3a`） | **PASS**：86 passed。 | 基于 `a46a008` 的独立本地候选；包含 test-mode 本地 API 与路由桩，不等于 GitHub 新 head CI。 |
| PR #125 旧断言 RED→GREEN | **PASS**：未修改时 `getByText("v1", { exact: true })` 复现失败；改为精确检查 `v1 (Candidate)` 后，目标测试及完整 Console E2E 通过。 | 修复保存在本地分支 `codex/pr125-e2e-candidate-label`，commit `f5ebd3a`；未推送。 |
| 冻结快照/API 契约 | **PASS**：`tests/test_pr125_launch_detail_contract.py` 五项场景包含于 Python 全量通过结果。 | 契约测试不等同真实 Langfuse 服务端测试。 |

## 剩余工作、需要授权的决策

1. **交付路线**：当前 #126 本地分支 `codex/fix-launch-detail-fourth-review` 比远端 `fix_launch_detail_ui` 多 24 个提交，包含 main 集成和 #125 验证资产；不能未经审阅直接推送。需要先决定整理/重放哪些提交以及目标分支。
2. **GitHub 写操作**：推送、PR #126 Review 回复/resolve、合并 PR、将 #125 标记为 superseded、关闭 #119–#124 都尚未执行；需要明确授权和交付策略。Issue 不会因本地测试通过自动关闭。
3. **最后的产品验收**：精确首屏用例还需覆盖成功、失败、取消、证据不足及运行中等父 Issue 状态组合；需要时再执行真实 Worker/Agent/Langfuse 链路验收，并明确环境、数据与执行成本。
4. 在上述项目完成前，保持 PR #125/#126 与 Issue #119–#124 的 GitHub 状态原样。
