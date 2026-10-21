# Issue #219 Console Managed Evaluation 交付与验收记录

## 当前结论

**本地功能、真实 Console E2E 与完整回归通过；已提交独立 [PR #227](https://github.com/miniceM/Argus/pull/227)。**
远端精确 head 的 CI / Cloud Policy 结果在 PR 最终验收评论中单独记录，不以本地结果代替。
实现基于 `0f721a7`，功能提交 `72b0861`，真实服务运行身份 `2a14d9d`（与功能提交的 UI 源码一致）。
分支 `codex/issue219-managed-console`。验收后新增的内容是 harness、回归测试与脱敏证据，
没有重写 #213～#218 的后端。真实、受控故障注入、网络替身和远端 CI 的边界如下。

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
| `make validate`（独立 PG） | **PASS** | Python 1669 passed / 2 skipped / 31 deselected；OpenAPI / 生成类型无 drift；Compose PASS |
| 全量 Playwright | **137 passed，1.9m** | 覆盖原有 Builtin / 真实 API 创建，以及 6 条新增 Managed UI 场景；无失败或跳过 |
| PostgreSQL 全量专项 | **1669 passed** | 独立 argus219-postgres；2 项只在 SQLite 参数组跳过 MVCC/行锁探针，相应 PG 参数组已执行 |
| S4 真实本地采集 | **31 passed，190.72s** | `make trajectory-prepare && make trajectory-test`，独立固定镜像，不使用 Cloud 凭据 |
| 真实 Flowise→双 EVAL Score→Frozen Snapshot→Gate | **PASS** | 当前分支独立 Runner + Console；真实 Calculator/Judge/Code；三条 Console 链路见下 |
| 独立 PR / 精确 head CI | PR #227 已提交 | 精确 head / CI / Cloud Policy 见 PR 最终验收评论；未合并 |

首轮新 Playwright 的 6 项失败来自 fixture 漏填必需 `system/info.build_id`，
导致 AppShell 读取 undefined；补齐 API 真实必需字段后 6/6 通过。随后修正 mock
Snapshot 的 `EVIDENCE_COMPLETE` 为服务端实际 `COMPLETE`，避免把假诊断报告当正式证据。

## 已归档本地证据

- [结构化结果与精确源码哈希](evidence/local-validation.json)
- [完整 Playwright 输出](evidence/playwright-local.log)
- [make validate 结果摘录](evidence/validate-local-summary.txt)
- [独立 S4 结果摘录](evidence/trajectory-local-summary.txt)
- [文件 SHA-256 索引](evidence/index.json)
- 网络替身截图：[等待评分](evidence/mock-ui/managed-pending.png)、
  [冻结质量 FAIL](evidence/mock-ui/frozen-fail.png)、
  [缺分 UNKNOWN](evidence/mock-ui/frozen-unknown.png)、
  [固定 Gate PASS](evidence/mock-ui/frozen-gate-pass.png)、
  [360px Score](evidence/mock-ui/managed-360.png)、
  [360px Gate](evidence/mock-ui/gate-360.png)。

截图已目视检查；手机用例在 360/390×844 下执行控件几何、键盘与 axe 验收，
仅截图时扩大高度以完整显示单个 Score 面板，不把长截图冒充固定高度首屏。

## #212 父 Issue 验收覆盖矩阵

以下逐项引用父 Issue 原有 9 条验收，保留真实与 mock 的边界。

| # | 父 Issue 要求 | 已有上游交付 | #219 本次证据 / 剩余项 |
|---|---|---|---|
| 1 | 无 SDK 的真实 HTTP Agent，真实模型和工具，无框架 Adapter | #213 / PR #220：Flowise 3.1.2 + Calculator + HTTP Tool | PASS：原生 Calculator 得到 1266，模型/AgentVersion retries=0，Checkpoint 精确关联 |
| 2 | OTLP Experiment/Item、真实 Judge + Code source=EVAL | #214 / PR #221：原生 Rule 双评分 | PASS：Scores v3 独立只读回查；Judge/Code 各一条 source=EVAL，与冻结证据一致 |
| 3 | Scores v3 准确关联，跨 Launch/重复/同名 Online/旧 generation 隔离 | #216 / PR #223；#217 / PR #224、#225 | 前端只消费已核验 Typed Results，不按名称推断；现有后端与 PG 全量回归通过 |
| 4 | pending/成功/失败超时；不重复 Agent；缺分 UNKNOWN | #217 异步生命周期及重启证据 | PASS：真实 UI pending→recovered、受控缺分→UNKNOWN；各 Attempt=1；down/recover 属网络替身/后端故障测试 |
| 5 | 双评分→既有 Typed Result/QualityPolicy/Snapshot/Gate PASS/FAIL；缺分不可 PASS | #218 / PR #226 | PASS：三条真实 Console 创建/Run/Gate POST/GET，分别为 PASS/FAIL/UNKNOWN；后两者输入/Rule 为受控注入 |
| 6 | Rule 变更不改变既有 Snapshot/报告/Gate；后续 latest 如实披露 | #218 冻结后端回归 | PASS：暂停本次自有 Rule 后，三份 Snapshot/Gate API及浏览器文本/链接保持不变；不伪称冻结远程实现 |
| 7 | Builtin/历史/Demo/S3.1 不变；429/超时/缺失/迟到/类型回归 | #213～#218 原有自动化及真实 Demo 基线 | PASS：无 Langfuse 配置的独立真实 HTTP Demo v1=2/6、v2=6/6；完整回归及PG参数组通过 |
| 8 | Console 来源/等待/证据不足/有效链接；明确真实、mock、已知限制 | 本项负责 | PASS：6条新增 UI测试、真实 Score/Gate截图；匿名导航显示权限提示；已登录Chrome实际打开双Score页，未向浏览器传递密钥 |
| 9 | make validate、API/DB/前端/Playwright 及运行证据 | 本项负责收口 | 本地 validate / 888 Vitest / 137 Playwright / 1669 Python / 31 S4 / 真实 Console E2E通过；PR #227 精确head CI/Cloud policy单独记录于PR |

## 本分支真实 E2E（2026-10-21）

证据根目录：[issue219-20261021t083455z](evidence/issue219-20261021t083455z/receipt.json)。
浏览器没有 `page.route`，不是复用旧服务：新 SQLite、新 Runner 进程、新 Console preview；
创建 Launch、Run、计算/保存 Gate 全部经 Console 控件完成。AgentVersion 与已有 ReleasePolicy
属于 API 测试前置；独立审计的 Checkpoint SQL 不是用户流程，产品操作不要求 SQL。

| 场景 | Agent / Rule | 真实采纳 | Frozen Gate | Attempt |
|---|---|---|---|---|
| PASS | 原生 Flowise 3.1.2 / Calculator / 真实模型 | Judge=true、Code=true，均 source=EVAL | PASS，可发布 | 1 |
| 质量 FAIL | **受控错误 HTTP 输出 text=0**；真实 Langfuse Evaluator | Judge=false、Code=false | FAIL，不可发布 | 1 |
| 缺分 UNKNOWN | **受控错误输出 + 暂停本次自有 Code Rule** | Judge=false；Code缺失，不捏造零分 | UNKNOWN，不可发布 | 1 |

真实 Scenario身份：

| 场景 | Launch | Snapshot | Gate |
|---|---|---|---|
| PASS | `61b065da-48de-46da-9348-f888acad12e7` | `2b5907b7-b922-4634-84b6-6560f57fc7e2` | `b9733567-fa32-4d83-832e-bdc6cc6e6c2c` |
| FAIL | `d7675d54-37cc-46d8-98ba-b87b40c19273` | `3ad4779b-c553-4afb-9ec0-d9d4f9762feb` | `61eb6056-d595-47a6-91b5-9c8f21bc297f` |
| UNKNOWN | `dda672de-023d-40bf-97c1-0a490e5e2944` | `e678f8c9-dd72-4e53-9827-91fb2965327e` | `9f867ccf-0b8a-4de2-a590-ed972cacd99e` |

### 可核查证据与截图

- [独立 Scores v3 回查](evidence/issue219-20261021t083455z/independent-score-readback.json)：不加 source 筛选，能发现重复 API 回写；5条原生 EVAL与冻结采纳一致，1条必选 Code确实缺失。
- [真实 v4 Experiment/Item/TOOL Observation回查](evidence/issue219-20261021t083455z/remote-experiment-readback.json)：目标身份与Frozen Item一致。
- [已登录浏览器外部导航](evidence/issue219-20261021t083455z/authenticated-browser-readback.json)：真实Chrome中打开同一Trace/Observation的Scores页，Judge/Code均显示True；未导出Cookie。
- [原生模型/工具 Checkpoint审计](evidence/issue219-20261021t083455z/native-tool-checkpoint.json)：Calculator 工具输出1266，模型 retries=0，trace/observation/item精确相同。
- [无配置 Builtin HTTP 回归](evidence/builtin-http-regression.json)：与托管环境隔离，没有任何 Langfuse凭据；v1=2/6、v2=6/6。
- [真实 Console 等待评分](evidence/issue219-20261021t083455z/real-ui/pass-pending.png)
- [真实双 Score及来源](evidence/issue219-20261021t083455z/real-ui/pass-score-evidence.png)
- [真实 PASS Gate](evidence/issue219-20261021t083455z/real-ui/pass-fixed-gate.png)
- [真实质量 FAIL](evidence/issue219-20261021t083455z/real-ui/fail-score-evidence.png)
- [缺分 UNKNOWN](evidence/issue219-20261021t083455z/real-ui/missing-score-evidence.png)
- [UNKNOWN Gate拒绝发布](evidence/issue219-20261021t083455z/real-ui/missing-fixed-gate.png)
- [自有 Rule清理确认](evidence/issue219-20261021t083455z/owned-rules-paused.json)：仅暂停本次创建的两条 Rule；原有 Evaluator/Rule未改动。
- `*-history.json`：Rule变更后三条报告的可见值、Score链接和Gate保持不变。
- `*-browser-audit.json` / `request-audit.json`：Console API动作与远程Rule操作日志；零 Score写接口、零手动 collect。

### 复现

先按[用户指南](../../langfuse-managed-evaluation.md)准备隔离 Flowise、Langfuse Dataset 和两个既有 Evaluator。
当前 harness显式使用 #214 的隔离测试数据身份和 localhost Flowise端口18091；不要指向生产环境。
凭据从已有隔离 Runner容器读入内存，不打印、不写 env文件。需要Python虚拟环境、pnpm、Chrome、Docker和构建后的Console。

```bash
pnpm --dir services/console build
.venv/bin/python docs/validation/issue219-managed-console/real_console_acceptance.py
# 默认 DRY RUN；下一条会产生真实模型费用：
.venv/bin/python docs/validation/issue219-managed-console/real_console_acceptance.py --execute
.venv/bin/python docs/validation/issue219-managed-console/real_builtin_regression.py
# 独立只读工具审计：runtime.db路径取本次隔离运行目录，evidence-dir取本次输出目录
.venv/bin/python docs/validation/issue219-managed-console/verify_tool_checkpoint.py --runtime-db <本次runtime.db> --evidence-dir <本次证据目录>
```

### 证据边界、失败记录与限制

- 初次真实浏览器harness遇到必填标签 `*` / aria-label差异、异步页面展开时机和隐式 BrowserContext限制；这些运行不作为成功验收。
  已修复测试驱动，失败临时证据保留在本机 `/tmp/argus219-harness-failed-evidence`，没有降低业务断言；其中两次已有原生模型执行，费用不伪称为零。
- harness最后只作了 Ruff导入/变量排版修正；初始脚本hash在 `preflight.json`，UI源码与真实服务commit不变。
  历史截图的区域截图增强在对应history阶段执行，避免整页截图只看到嵌套滚动容器首屏。
- 独立无会话浏览器的外部链接得到Langfuse权限提示（HTTP 200 + Sign In），并非已登录Score截图。
  随后通过用户现有已登录Chrome会话，实际打开同一链接并在Scores页核对Judge/Code=True及计算解释；
  与独立API回查一致。没有读取/导出Cookie，浏览器未接收公私钥。
- 无真实生产Langfuse停机；down/recover、429、类型不符、歧义与迟到/旧代通过单元/集成/网络替身覆盖。
  FAIL/UNKNOWN的注入条件明确如上，不冒称自然模型失败。
- 自有Rule退出时已暂停。合成Experiment/Score保留供审计；隔离本地Runner/Console已停止，报告中的localhost固定链接是本次运行地址，不冒充长期部署。
- PR/远端精确head CI及Cloud policy在交付时单独核验；未合并、未发布、未修改Ruleset/Environment。
