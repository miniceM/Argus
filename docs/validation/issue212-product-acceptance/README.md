# Issue #212：真实端到端产品验收与用户体验复核

## 结论与版本边界

**2026-10-22：本地修复后的工作区整体产品验收 PASS。**

- 仓库：`miniceM/Argus`；基线 / 验收开始时远端 main：`f2a67d6c24617f1c5847e84cb82b98773c755ee0`。
- 当前交付为该基线加本次本地补丁，**不是已合并 main 或新的远端 PR-head 验收**。精确文件哈希与补丁见 [源码身份](evidence/source-identity.json)、[运行时 preflight](evidence/live-final/preflight.json)。
- 已核对 #212、#213～#219、PR #220～#228、#230 的交付；#231 在核对时仍 OPEN，**不作为本次已完成能力**。[上游记录](evidence/upstream-delivery.json)
- 真实收费执行、受控故障、网络替身、PostgreSQL 并发、S4、普通用户人工浏览分别记录，不互相替代。
- 原始测试日志与 `source.patch` 保留工具输出字节（含行末空白），不为消除 diff 告警改写证据；源码及文档另行通过 whitespace 检查。
- 上述本地验收记录生成时尚未 push / 创建 PR；随后用户已授权提交、推送并创建 PR。原始 receipt 与源码身份记录保留验收当时状态，不能作为远端 CI 证据。未 merge、未发布、未修改 Ruleset / Environment，也未关闭 GitHub #212。

## 本次发现并修复的问题

### 托管评分已完成，列表质量却永久停在 UNKNOWN

首轮真实复验中，Item、Frozen Snapshot 与 Gate 已经 PASS / FAIL，而 Launch 列表及按质量筛选仍是 UNKNOWN。
原因：HTTP 执行先终结时 Launch 写入 UNKNOWN；异步评分随后冻结报告，但未刷新 Launch 的列表质量投影。

最小修复位于 `services/eval-runner/app/result_snapshots.py`：在创建或复用**当前结果对应**的 Snapshot 时，
复用 Snapshot summary 与既有 `aggregate_quality_conclusion` 更新 Launch 质量投影。
不重新收集 Score、不重写 QualityPolicy、不改变 Agent 执行状态、不修改任何历史 Snapshot / Gate。
既覆盖新评分完成，也覆盖 Reconciler 复用既有 Snapshot 时修复陈旧投影。

- RED：[PASS/FAIL 错误复现](evidence/launch-quality-red.log)、[既有 Snapshot 投影修复失败](evidence/stale-projection-red.log)。
- GREEN：[126 passed / 2 SQLite 不适用项 skipped](evidence/launch-quality-green.log)。
- 真实复验：[PASS 列表](evidence/live-final/pass-list-quality.json)、[FAIL 列表](evidence/live-final/fail-list-quality.json)、[UNKNOWN 列表](evidence/live-final/missing-list-quality.json)；三类筛选均由浏览器验证。
- API 模型未改变；OpenAPI / 生成 TypeScript 契约没有 drift。

另外将既有真实验收 harness 的 Flowise origin / DB 容器 / Flow UUID / CredentialRef 参数化，保留默认 dry-run；
无效 UUID 在外部调用之前拒绝。旧脚本中已销毁的容器与固定端口不能作为当前环境事实。
准备脚本只克隆独立 Flow，复用已有凭据，不改已有 Flow，不创建密钥。对应 [RED](evidence/harness-red.log) / [GREEN](evidence/harness-green.log)。

## 真实 Flowise → 双 Evaluator → Frozen Snapshot → Gate

最终主链路时间：**2026-10-22 16:40～16:45（Asia/Shanghai）**。
隔离 environment：`issue212-20261022t083959z`；独立 SQLite standalone Runner / Console，未重用开发服务。
该方式是独立验收配置，**不是生产多实例 Redis 协调验收**。

### 三条真实场景

| 场景 | Agent / 注入边界 | Agent Execution | Evaluation | 必选 Score | Quality / Gate |
|---|---|---|---|---|---|
| PASS | 原生 Flowise 真实模型 + Calculator，答案 `1266` | succeeded，Attempt 1 | succeeded | Judge=true，Code=true，均 source=EVAL | PASS / 可发布 |
| FAIL | 受控普通 HTTP 输出 `text=0`；评分仍为真实 Langfuse Judge + Code | succeeded，Attempt 1 | succeeded | Judge=false，Code=false | FAIL / 不可发布 |
| UNKNOWN | 同一受控 HTTP 输出；仅暂停本次自有 Code Rule | succeeded，Attempt 1 | failed（15 次持久收集预算耗尽） | Judge=false，Code 缺失；不填 0 | UNKNOWN / 不可发布 |

FAIL / UNKNOWN 的 Agent 不是第二次真实 Flowise 调用，不宣称是自然生产事故。
三场景均在 Console 完成创建、Run、查看 Score、选择冻结报告、计算/保存/刷新 Gate。
AgentVersion 与 ReleasePolicy 是部署前置，通过既有 API 登记，不要求用户查数据库。

| 场景 | Launch | Snapshot | Gate |
|---|---|---|---|
| PASS | `df1050ac-0f39-4a0c-852e-4193460aa00e` | `16310689-dc61-4c55-af3e-22245dc5d024` | `36fcea91-6820-4cda-80bc-7f15d695f48a` |
| FAIL | `e29fdad1-db61-4d19-b4c3-f9b75e0c1b9e` | `9e4f3cda-550b-4d64-86f6-1a618efd41a0` | `674eda44-613c-4fd5-90ab-0953fa521e7a` |
| UNKNOWN | `62daf415-4fa2-4486-bf6c-efccb80033ba` | `dba8cf16-f79c-43c0-83ab-6ffb48a173d0` | `d9800058-d3c7-45b4-82db-97b7efac9cd9` |

### 独立真实性核验

- [最终 receipt](evidence/live-final/receipt.json)：无 page.route 网络替身、无手工 Score 写入、无手动 collect。
- [真实访问日志白名单审计](evidence/live-final/access-log-audit.json)：3 次 Launch 创建、3 次 Run、3 次 Gate，0 次手动 collect。
- [原生工具 Checkpoint](evidence/live-final/native-tool-checkpoint.json)：Calculator tool message=`1266`，模型 `deepseek-v4.1-flash`、`maxRetries=0`；Argus AgentVersion `max_retries=0`。
- [原生 Experiment / Item / TOOL 回查](evidence/live-final/remote-experiment-readback.json)：Experiment、Dataset Item、Trace、`remote-agent-http` TOOL Observation 身份匹配。
- [独立 Scores API v3 回读](evidence/live-final/independent-score-readback.json)：已采纳 Score 与冻结证据逐字段一致、无重复投影。不是只按名称推断归属。
- [已登录浏览器回读](evidence/live-final/authenticated-browser-readback.json)：原生 Langfuse 页面确有两条真实 Score，非仅登录页 HTTP 200。
- [Experiment 外链回读](evidence/live-final/experiment-link-browser-readback.json)：旧 run URL 在当前部署跳转至正确 Experiment baseline，双 Score 可见；不带认证信息。

### 历史不可变：真实迟到 Score 补证

主链路首先停用两条本次自有 Rule，逐字节比较三个 Snapshot / Gate，并通过浏览器重开历史报告核对 Score 文本、链接、Gate 均未变。

进一步在 **17:05（Asia/Shanghai）**，仅重新启用本次自有 Code Rule，复用现有 projector，重放 UNKNOWN Item 的**相同 Trace / Observation / Checkpoint IO**。
不重调 Agent、不写 Scores API、不手工 collect。Langfuse 原生 Code runtime 随后产生了之前缺失的 `source=EVAL`、BOOLEAN=false Score：
`a5d671bf-1181-5951-a2ae-6e3f71d2e44a`，metadata 同时匹配原 Rule / Evaluator / Trace / Observation。

**此时 Langfuse 远程数据已经变化，Argus 原 UNKNOWN Snapshot / Gate 仍逐字段相同，Attempt 仍为 1，历史 Console 仍只展示原来已采纳的一条 Score。**

- [迟到原生 Score 与不可变断言](evidence/live-final/late-native-score-immutability.json)
- [执行记录](evidence/late-native-score.log)、[一次性探针源码记录](evidence/late-native-score-probe.py.txt)
- [迟到后历史 UI 回查](evidence/live-final/missing-history.json)

补证的 API/UI 回读使用停用 Worker/Reconciler 的本地只读复核服务；自动 dispatcher 的租约、代际、迟到隔离另由真实 PostgreSQL 测试覆盖。
Rule 最终重新停用；没有修改共享 Evaluator，未通过生产停机来制造失败。
Langfuse Rule 执行 latest 实现；本次冻结实际采纳结果，而不是宣称冻结远程实现版本。

## 覆盖矩阵

| 用户要求 / 风险 | 状态 | 当前证据与边界 |
|---|---|---|
| 沿用 Launch / Item / Result Snapshot / Gate 入口，无新管理平台 | PASS | 三条真实浏览器链路；本次生产代码仅修复列表质量投影 |
| Builtin 默认、Managed 显式选择与契约校验 | PASS | `issue-215-managed-evaluation.spec.ts`，真实 Managed 创建截图 |
| 创建、Run、等待进度、冻结、Gate，无手工 SQL | PASS | 三条 browser.json、timeline.json、access-log-audit.json；内部只读审计 SQL 不属于用户操作 |
| 四个独立维度：Execution / Evaluation / Quality / Sync | PASS | 三场景 pending / succeeded / failed 与 PASS / FAIL / UNKNOWN 截图 |
| 真实 Score 类型、source、值、缺失原因、证据 | PASS | 最终 snapshot、managed diagnostics、独立 Scores API v3、原生 UI；false 不伪装成缺值 |
| 评分执行失败与质量 FAIL 区分 | PASS | FAIL 场景 eval=succeeded；UNKNOWN 场景 eval=failed；Execution 均 succeeded |
| Trace / Observation / Experiment 外链安全且可用 | PASS | 自动导航 + 已登录 CUA 回读；未构造独立 Score URL、未在 URL 放 key |
| 列表与冻结报告质量一致、质量筛选有效 | PASS | 本次新增 RED→GREEN 与三场景真实列表筛选 |
| 历史报告不读 latest 回填、链接不漂移 | PASS | Rule 变化后 history；真实迟到 Code Score 后旧 Snapshot/Gate/UI 不变；mock 另测远程值/版本变化 |
| Langfuse latest Rule 语义如实展示 | PASS | Score 面板与 Manifest 审计提示；未声称冻结远程实现 |
| 缺必要 Score / 歧义 / provenance 不可验证不发布 PASS | PASS | 真实缺分 UNKNOWN；类型/歧义/归属等边界由全量单测覆盖 |
| 429 / Langfuse down / 恢复反馈 | PASS（自动化） | 单测持久退避 + Playwright down/recover；**未制造真实服务中断或自然 429** |
| 360px / 390px、键盘、焦点、axe | PASS（mock UI） | 两组控件几何 + wcag2a/aa/21aa；真实桌面另人工核查 |
| 无 Langfuse 配置的 Builtin 正常 | PASS（真实 HTTP） | [v1=2/6、v2=6/6](evidence/builtin-http-regression.json)，独立服务无 Langfuse 配置 |
| PostgreSQL 并发、租约、代际与迟到隔离 | PASS | 真实 PG 参数组实际执行；SQLite 不适用的两项不是未测 PG |
| 完整类型、Vitest、Playwright、make validate | PASS | 见回归表；OpenAPI / 生成类型无 drift |
| 真实 Flowise → 双 EVAL → Frozen Snapshot → Quality Gate | PASS | 最终 live-final，而非首轮或历史 #219 receipt |
| 当前 GitHub PR-head / 新补丁 CI | 待 PR CI | 用户随后已授权推送 / 创建 PR；当前报告只证明本地验收，不把基线 CI 或历史 #227 CI 当作新补丁 CI |
| 生产 Redis 多实例部署、真实公网灾难/长时压测 | NOT RUN | 超出本次独立产品验收，不以 standalone 或 mock 冒充 |

**本次约定的产品验收必需项无剩余 FAIL/BLOCKED。** NOT RUN 项是独立的远端交付/生产运维边界。
UX 改进意见见下，未把“能通过验收”等同于“体验已无优化空间”。

## 完整回归

| 执行 | 最终结果 | 记录 |
|---|---|---|
| Harness 参数化测试 | 5 passed；含预期 RED | harness-red.log / harness-green.log |
| 托管质量 / #228 回归 / 并发生命周期 / harness 定向 | 126 passed，2 SQLite-only skips | launch-quality-green.log |
| `make validate`（隔离 PostgreSQL） | **PASS：Python 1685 passed / 2 skipped / 31 deselected；Vitest 898 passed / 58 files** | [完整日志](evidence/validate-final.log) |
| typecheck / token lint / build / 契约同步 / Compose | PASS，包含在 make validate | 同上；构建有既有 bundle-size 警告，非失败 |
| Managed Playwright 定向 | 7 passed | [日志](evidence/playwright-targeted.log) |
| 最终 `make console-e2e` | **138 passed，0 failed / skipped，1.8m** | [日志](evidence/playwright-final.log) |
| PG / SQLite 并发生命周期单独 verbose 核验 | 72 passed / 2 SQLite-only skips | [日志](evidence/postgres-lifecycle.log) |
| `make trajectory-prepare && make trajectory-test` | **31 passed**，真实采集 | [准备](evidence/trajectory-prepare.log) / [结果](evidence/trajectory.log) |
| Builtin 真实 localhost HTTP 回归 | v1=2/6、v2=6/6 | [记录](evidence/builtin-http-regression.json) |
| Secret 值扫描 / diff 检查 / 证据索引 | PASS | [完整性记录](evidence/integrity.json)、[SHA-256 索引](evidence/index.json) |

Node 为隔离安装的 **22.23.4**，pnpm **10.5.2**，Python **3.12.4**。
宿主已有 Homebrew Node 22 因缺少 simdutf 动态库无法启动；未改宿主库，使用独立 Node 22 完成门禁。
不以 Node 24 的结果冒充 Node 22。

首轮 `evidence/live/receipt.json` 只说明旧 harness 自身断言通过；当时列表质量缺陷尚在，
**不是整体验收 PASS**。最终结论以本地修复后 `live-final`、列表筛选与最终回归为准。

## 关键截图

真实场景（不是 mock）：

- [创建托管 Launch](evidence/live-final/real-ui/pass-create.png)
- [执行成功、评分等待、UNKNOWN](evidence/live-final/real-ui/pass-pending.png)
- [双真实 Score 与冻结证据](evidence/live-final/real-ui/pass-score-evidence.png)
- [Langfuse 已登录双 Score](evidence/live-final/real-ui/langfuse-authenticated-double-score.png)
- [Langfuse Experiment](evidence/live-final/real-ui/langfuse-authenticated-experiment.png)
- [固定 Gate PASS](evidence/live-final/real-ui/pass-fixed-gate.png)
- [评分成功但质量 FAIL](evidence/live-final/real-ui/fail-score-evidence.png)
- [缺分、评分失败、质量 UNKNOWN](evidence/live-final/real-ui/missing-score-evidence.png)
- [列表三态一致](evidence/live-final/real-ui/manual-final-list.png)
- [Langfuse 迟到原生 Code Score](evidence/live-final/real-ui/langfuse-late-code-arrival.png)
- [迟到后历史 Gate 仍为 UNKNOWN](evidence/live-final/real-ui/manual-late-frozen-unknown.png)

网络替身响应式 / 故障测试另存于 [mock-ui](evidence/mock-ui)，不可当真实 Langfuse 证据。
所有展示截图均做目视复核；长面板截图不冒充 844px 手机首屏。

## 普通用户视角：可用，但信息呈现偏工程化

### 合理之处

1. 三个既有详情标签串联用例、Gate、审计，不必再学一个评测管理平台。
2. 终态执行诊断默认收起；最重要的质量通过率、必选 Score 进度和 Gate 可发布结论可见。
3. UNKNOWN 不伪装为 FAIL；等待与失败有文字状态，不能只靠颜色；外链没有携带凭据。
4. 原始证据保留且历史只读，满足审计人员需要，而不是仅提供一个无来源总分。

### 改进建议（不阻塞本次 #212 业务闭环）

| 优先级 | 当前观察 | 建议；不扩建平台 |
|---|---|---|
| 高 | 用例卡片仍用完整 Dataset Item UUID 作主标题；列表也以 Launch UUID 作识别入口 | 一级显示任务名/问题摘要，短 ID 放次级。#231 已提出相近方向，但未合并，不计入本次 |
| 高 | 单个 Score 名称重复两次，首层常驻 Rule / Evaluator ID、binding digest、契约码；两条评分占很长页面 | 首层保留“评分名、值、状态、证据入口”；ID、digest、原始 JSON 收入统一“审计证据”折叠区 |
| 高 | 详情首屏同时出现 COMPLETED、PASS、“冻结质量通过”、两套通过率及很长的解释 | 保留执行/评分/质量/同步四维语义，但减少同维度重复；解释放 tooltip/帮助，不能删除关键状态 |
| 中 | 正常等待评分时，正确的“等待托管评分”Badge 下仍出现红色 pending 错误码 | 正常等待用中性/等待语气；红色留给真实错误，技术码折叠至排障详情 |
| 中 | 本次没有 Baseline 时，KPI 同时显示“暂无基线对比”和“不可比／判定规则或测量版本不同” | 空基线应说明“尚未设置基线”；仅在真有两个版本且契约不同才用“不兼容”原因 |
| 中 | Gate 需要手填 ReleasePolicy 名称/版本；初次使用易卡住 | 部署方预置默认策略或提供已有策略选择器；不新增策略编辑平台 |
| 中 | 审计页 Managed 契约显示两条 Score，但旧 Builtin 模板仍显示“评测门禁指标(0)” | 标清“内置评分器：本模式不执行”，避免与两个托管评分混淆；latest 说明保留 |
| 中 | OTLP、Typed Result、Manifest、acceptance_digest 等研发术语首层混用 | 一级用“执行／评分／质量／云端链路同步”；术语与原始字段留在审计层 |

**判断：架构并未过度设计；主要是将正确但过多的内部证据同时展示给普通用户，信息层级有过度设计。**
优先做渐进式披露与业务标题，不应再建 Evaluator Catalog、Prompt/Code Editor 或 Score Analytics。

## 使用与复验入口

普通用户按 [完整 Flowise → Argus / Langfuse 使用指南](../../langfuse-managed-evaluation.md) 操作。
从注册 Agent、配置原生 Judge/Code Rule、选择 Managed，到等待/排障、查看 Score、冻结与 Gate，均复用既有入口。

复验前准备 Python 3.12、Node 22、pnpm、Chrome、独立 Flowise/Langfuse 测试项目；凭据只在服务端配置。

```bash
# 默认不启动服务、不读取凭据、不产生费用
.venv/bin/python docs/validation/issue219-managed-console/real_console_acceptance.py

# 克隆专用 Flow，复用原 Flow 凭据，不创建密钥；使用自己的受限 env 文件路径
.venv/bin/python docs/validation/issue212-product-acceptance/prepare_flowise.py \
  --execute --env-file /secure/test.env --source-flow-id <existing-flow-uuid> \
  --out /tmp/issue212-flow-receipt.json

# 真实调用会产生模型费用；复用 #219 harness，而不是新建后端流程
.venv/bin/python docs/validation/issue219-managed-console/real_console_acceptance.py \
  --execute --name-prefix issue212 --credential-container <isolated-runner-container> \
  --flowise-origin http://127.0.0.1:19091 \
  --flowise-postgres-container <isolated-flowise-postgres-container> \
  --flow-id <new-flow-uuid> --flowise-credential-ref env://FLOWISE_API_TOKEN \
  --evidence-dir /tmp/issue212-new-live-evidence

# 这些端口用于测试，不复用开发/生产服务；须先完成 Console build
ARGUS_E2E_API_PORT=28080 ARGUS_E2E_CONSOLE_PORT=28083 make console-e2e
TEST_POSTGRES_URL=<isolated-postgresql-url> make validate
make trajectory-prepare && make trajectory-test
```

Harness 引用 #214 的已有合成 Dataset / Evaluator 配置；复用同一隔离测试项目时应先只读确认这些资源仍存在。
不同项目需按用户指南准备自己的原生 Rule / Dataset，不能将历史资源 ID 当作通用默认值。
验收服务是临时的，记录中的 localhost 端口可能已停止；审核优先使用保存的 JSON、截图、日志与哈希索引。
本次创建的规则已通过远程 GET 确认暂停；独立 Flow 保留为测试资源，未擅自删除云端/已有资源。
自建 PostgreSQL 测试容器与临时人工复核服务已清理，未停止原有 Flowise / Argus 栈。
