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
