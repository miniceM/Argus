# Issue #219 Console Managed Evaluation 交付记录（进行中）

## 当前结论

**Goal 尚未完成。** 当前工作基于合并 #218 后的 `0f721a7`，分支
`codex/issue219-managed-console`。以下结果只证明明确列出的范围；真实 Flowise
全链路、最终完整回归和独立 PR 仍须以本分支新增证据关闭，不能以 #213～#218
旧证据冒充本次验收。

## 实现范围

- 沿用 CreateLaunch / ManagedEvaluationFields，补充未配置环境的 Builtin 指引。
- 在现有用例展开区展示每条必选 Score 的采纳进度、真实类型/值/source、
  Score ID、Rule 引用、安全 Observation 链接和实时未采纳诊断。
- 分开说明 Agent Execution、Evaluation、质量结论和实时 OTLP Sync。
- 冻结视图只展示所选 Snapshot 的 Manifest / Typed Results，拒绝实时回填；
  不请求实时 Managed diagnostics，不连接后续 Attempt 时间线。
- 修正 Manifest 审计固定写“待收集评分”的错误文案。
- 复用 Release Gate 既有 API，在原 Gate 标签页计算/保存/读回固定 Gate，
  明确校验 Launch / Snapshot / Gate 身份；没有新建策略编辑器或后端门禁逻辑。
- 补充 [完整用户指南](../../langfuse-managed-evaluation.md)。

## 当前自动化证据（2026-10-21）

| 范围 | 结果 | 证据边界 |
|---|---|---|
| 初始 RED | PASS（已观察预期失败） | 新组件未实现；冻结投影丢失 managed 原因；Managed 同步方向文案错误 |
| 新增 Playwright 6 场景 | 6 passed，13.9s | 网络替身，非真实 Langfuse；默认 Builtin 创建另由 #215 既有测试覆盖 |
| Vitest / typecheck / token lint / build | **PASS，888 tests / 56 files** | 当前本地实现；远端精确 head CI 另行核验 |
| `make validate`（独立 PG） | **PASS** | Python 1667 passed / 2 skipped / 31 deselected；OpenAPI / 生成类型无 drift；Compose PASS |
| 全量 Playwright | **137 passed，1.9m** | 覆盖原有 Builtin / 真实 API 创建，以及 6 条新增 Managed UI 场景；无失败或跳过 |
| PostgreSQL 全量专项 | **1667 passed** | 独立 argus219-postgres；2 项只在 SQLite 参数组跳过 MVCC/行锁探针，相应 PG 参数组已执行 |
| S4 真实本地采集 | **31 passed，190.72s** | `make trajectory-prepare && make trajectory-test`，独立固定镜像，不使用 Cloud 凭据 |
| 真实 Flowise→双 EVAL Score→Frozen Snapshot→Gate | **NOT RUN（本分支）** | 之前 #218 的真实记录是参考，不代表本分支验收 |
| 独立 PR / 精确 head CI | NOT RUN | 未创建、未推送、未合并 |

首轮新 Playwright 的 6 项失败来自 fixture 漏填必需 `system/info.build_id`，
导致 AppShell 读取 undefined；补齐 API 真实必需字段后 6/6 通过。随后修正 mock
Snapshot 的 `EVIDENCE_COMPLETE` 为服务端实际 `COMPLETE`，避免把假诊断报告当正式证据。

## #212 父 Issue 验收覆盖矩阵

以下逐项引用父 Issue 原有 9 条验收，保留真实与 mock 的边界。

| # | 父 Issue 要求 | 已有上游交付 | #219 本次证据 / 剩余项 |
|---|---|---|---|
| 1 | 无 SDK 的真实 HTTP Agent，真实模型和工具，无框架 Adapter | #213 / PR #220：Flowise 3.1.2 + Calculator + HTTP Tool | 本分支真实 Flowise 执行 NOT RUN；不得仅凭旧报告完成 |
| 2 | OTLP Experiment/Item、真实 Judge + Code source=EVAL | #214 / PR #221：原生 Rule 双评分 | 本次真实 Score / Trace / Observation API 回查 NOT RUN |
| 3 | Scores v3 准确关联，跨 Launch/重复/同名 Online/旧 generation 隔离 | #216 / PR #223；#217 / PR #224、#225 | 前端只消费已核验 Typed Results，不按名称推断；现有后端与 PG 全量回归通过 |
| 4 | pending/成功/失败超时；不重复 Agent；缺分 UNKNOWN | #217 异步生命周期及重启证据 | UI pending→ready、down/recover、UNKNOWN 网络替身已通过；本次真实异步收敛/调用次数待实测 |
| 5 | 双评分→既有 Typed Result/QualityPolicy/Snapshot/Gate PASS/FAIL；缺分不可 PASS | #218 / PR #226 | 新 Gate UI 已通过身份及 UNKNOWN 单测；真实 UI→Gate POST/GET 闭环待实测 |
| 6 | Rule 变更不改变既有 Snapshot/报告/Gate；后续 latest 如实披露 | #218 冻结后端回归 | UI 历史值/链接/Manifest 不被实时变化覆盖已通过；本次真实 Rule 变更/历史回查待实测 |
| 7 | Builtin/历史/Demo/S3.1 不变；429/超时/缺失/迟到/类型回归 | #213～#218 原有自动化及真实 Demo 基线 | make validate、完整 Playwright、独立 PG 通过；本分支真实 Demo 回归待实测 |
| 8 | Console 来源/等待/证据不足/有效链接；明确真实、mock、已知限制 | 本项负责 | 新组件+6 Playwright已通过；截图视觉复核已开始，最终证据需绑定最终源码 |
| 9 | make validate、API/DB/前端/Playwright 及运行证据 | 本项负责收口 | 本地 validate / 888 Vitest / 137 Playwright / 1667 Python 通过；真实 E2E、PR/CI 未闭环，不可宣布父 Issue 完成 |

## 接续验收顺序

1. **已完成**：修复首屏布局，未放宽原断言；typecheck / 888 Vitest / 137 Playwright / `make validate` 全部通过。
2. **已完成**：独立 PostgreSQL 下全部非 trajectory_local 测试；独立 S4 采集 31/31 通过。
3. 本分支提交后启动独立真实 Runner / 当前 Console。使用专用环境和自有 Rule，
   由 Console 创建并运行真实 Flowise Launch；保存 pending / completed / Score / Gate 截图。
4. 独立只读回查 Scores v3，核对真实模型/工具证据、双 source=EVAL、
   Attempt=1、零手写 Score、零手动 collect、固定 Snapshot/Gate。
5. 受控变更本次自有 Rule，回查历史 Snapshot/Gate 字节内容及可见链接不变；
   明确受控错误/缺分是模拟注入，不冒称自然故障。
6. 更新本矩阵、归档脱敏证据、独立 PR、读取当前精确 head 的 CI/Cloud policy。
   不合并、不发布、不改 Ruleset 或 Environment 权限。
