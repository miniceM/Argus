# Issue #213：Flowise 原生 HTTP E2E 验收记录

日期：2026-10-19（Asia/Shanghai）。父项 #212；只验收 1/7 HTTP 执行，不验收 #214～#219 的托管评分与 Gate。

## 身份与边界

- Runner / Console 源码基线：main `265c55014b4810d5f481aa67b37f047a658fe3ac`（#211 输出消歧修复）；本次重新构建独立镜像，不使用 P0 的旧 18090 实例。
- 独立 Compose 项目 `argus-flowise-e2e`，API 19090、Console 19093、Flowise 19091、工具 19094；测试数据与其他任务隔离。
- Flowise 3.1.2，镜像 `flowiseai/flowise@sha256:cf880b17b1290fbaebebc76c33954bde67b3775aff95feec72a1d060cc81e43d`。
- 原生 Start → Agent，模型 `deepseek-v4.1-flash`，OpenAI-compatible 网关。网关不承诺不可变模型快照，记录的是本次 serving ID，不伪称精确冻结 Provider 实现。
- Calculator + 原生 requestsGet（evidence_lookup）。业务工具独立生成随机 receipt、服务端记录请求，不模拟模型、不代理 Prediction。
- 不改 Argus services/config/API/Worker/Executor；不添加 Flowise Adapter；不改 Flowise 业务代码；不新增或启用 Argus/Langfuse 业务 SDK 接入。上游发行版自带的可选 integration 不代表本示例启用了它们。
- `max_retries=0`、非幂等、concurrency=1；每 Item 独立 sessionId，Memory=false。只使用合成测试数据，Ground Truth 未传给 Flowise。

部署、注册与执行脚本见 `examples/flowise/README.md`；原始结构、ID、Attempt 和独立 receipt 见同目录 `evidence.json`。

## 实际执行矩阵

命名空间：`i213-d4651ca246b8`。五条逻辑用例，分四个 Launch；不可达场景复用 timeout 逻辑用例，不把它算作第六条独立测试内容。

| 场景 | Launch | Item / Attempt | 实际结果 |
|---|---|---|---|
| plain + tools + structured | `b3a3f3ed-1b9e-4315-920d-274757f062d6` | 3 Item，各 1 Attempt | 全部 HTTP 200 / succeeded；Launch COMPLETED |
| 无效 Flowise token | `33d2d50a-a1d4-4232-b3a2-c24d8f66186b` | 1 Item / 1 Attempt | 原生 HTTP 500 / HTTP_5XX / execution failed；Flowise 日志 Unauthorized，未进入模型 execution |
| 一秒 HTTP read timeout | `e6aad580-3ca1-4668-a949-77fdf463f79c` | 1 Item / 1 Attempt | http_status=null；AMBIGUOUS_OUTCOME / MAY_HAVE_BEEN_SENT；1006ms；不自动重发 |
| 模型不可达（单独原生 Flow，loopback:9） | `f5bbaccc-73be-48d7-b386-b8f5badb6ece` | 1 Item / 1 Attempt | HTTP 500 / HTTP_5XX / execution failed；原生 execution ERROR |

### 两个必须保留的语义限制

1. Flowise 3.1.2 Prediction 把其内部 `Unauthorized` 包装成 **500**。这不是标准 401/403；Argus 如实记录而没有修改上游状态。证据同时包含 Unauthorized 服务日志、无 auth session 原生执行及一次失败 Attempt。不能声称本次 Flowise 验证了 HTTP 401。
2. 非幂等请求发送后的 read timeout 是 **结果不确定**，不能称作“Agent 未执行”。Flowise 继续完成了同一 timeout session；Argus 只有一条 Attempt、原生 Flowise 也只有一次对应执行，没有重发。

三条正常响应保留原生 `text`，没有静默改写为 `answer`。因此 builtin `answer_present@1.0.0` 全为 false、质量 FAIL；这不影响 HTTP execution succeeded，也不是将业务质量判为 PASS。

## 真实模型、工具、隔离与次数核验

- plain 原生 `text=READY`；structured 原生 text 可解析为 `{"status":"ok","value":42}`。
- tools 的原生 execution ID：`8042ded9-bd56-44f2-9312-3b42770db200`。
- 工具调用记录包括 `evidence_lookup` 与 Calculator；Calculator toolOutput=`1266`。
- 独立业务工具 receipt：`fe17955b066c8d4e92d2fc24`，case_id=`i213-d4651ca246b8-tools`；服务端日志、原生 usedTools.toolOutput、最终原生 text 三处一致。
- 原生模型 usageMetadata.total_tokens=`2383`（包含工具调用过程的 Flowise 聚合；不把它冒充 Argus 已提取 usage）。Argus 未配置原生嵌套 usage selector，Console token KPI 保持 unknown，不伪造费用。
- tools 原生 `input.messages` 中没有 plain 的 `secret-marker-i213-d4651ca246b8`；不同 Item 原生 sessionId 不同，Memory=false。并非仅凭模型声称 NO_PREVIOUS_MARKER 判隔离。
- 正常 Flow 的 plain/tools/structured/timeout 各 1 条 native execution；不可达 Flow 的 timeout session 1 条 ERROR；auth 为 0。两种 Flow 合计 5 条，独立工具请求 1 条。
- `validate_native_evidence()` 自动核验原生 text、结构化 JSON、usage、receipt、marker 不泄漏、execution/session 次数；单测同时覆盖缺 receipt、重复执行、历史 marker 泄漏等拒绝路径。

## Console 核验（真实环境，非 UI mock）

在 19093 打开的当前源码 Console 显示 Build `265c5501`：

- 列表：正常 Launch COMPLETED 3/3、质量 FAIL；auth / timeout / unreachable 为 FAILED，而非模拟成功。
- 正常详情：冻结 3 Item、成功 3、失败 0，总调用 3、重试 0；逐 Item 可见冻结 Input JSON。
- tools 展开后：Candidate Output AVAILABLE，显示完整原生 JSON、`text`、sessionId、executionId、usedTools、业务工具 receipt；与 API comparison/case 和独立服务日志一致。
- timeout 详情：FAILED / UNKNOWN，1/1 execution failure，诊断快照，不允许作为正式 Baseline；总调用 1、重试 0。用例页提供“执行失败/超时 (1)”筛选。
- 终态 Snapshot 的 Attempt 按钮禁用并提示“历史快照不包含实时 Attempt 时间线”，这属于既有历史快照隔离行为；逐条实时 Attempt 已由 API 独立读取，不把禁用按钮说成已查看完整时间线。
- 初始未配置 Dashboard 导致 Run 链接显示暂不可用；Item Trace 及原始输出仍可访问。示例 Compose 已补 `ARGUS_LANGFUSE_DASHBOARD_URL` 配置，不通过 UI 代码修复。

UI 自动化单独运行现有 `agents.spec.ts`：1 passed，真实隔离 API/Console，端口 19180/19183。该用例只证明 Registry UX 回归，不代替上述真实 Flowise 页面检查。

## 原有服务回归（当前源码重新构建）

回归命名空间：`i213-reg-75f256dffc`。不是复用旧服务绿色记录。

| 回归 | Launch | 结果 |
|---|---|---|
| 既有 Demo v1，6 条合成银行 Dataset | `5198204d-93d5-4aa3-8518-1c0ea3d82f1f` | overall_pass=2/6，符合原基线 |
| 既有 Demo v2，同 Dataset | `efc9fabe-d2b2-461c-915a-065c7096212e` | overall_pass=6/6，符合原基线 |
| #198 Real LLM Agent，同 6 条合成 Dataset | `4612efbf-8c3f-4ca3-b5f7-611514520594` | 6/6 execution succeeded、6/6 answer_present true、每条一次 Attempt；usage_coverage=1.0，usage_tokens_total=3070；金额 unknown |

Demo 使用原有 `/experiments/run` 路径；#198 使用 Registry → Version → Dataset → Launch，复用原服务实现。

## 自动化与门禁

- 相关原有 Executor / #198 单测：72 passed。
- 示例契约测试：9 passed，包括 mapping 不传 Ground Truth、逐 Item session、原生工具配置、单次 Attempt、独立 HTTP 工具 receipt、证据缺失/重复/串历史拒绝及真实终态识别。
- 最终 `make validate`：PASS，Python 1384 passed、7 skipped、31 deselected；Console typecheck/Vitest/build、OpenAPI 与 Compose 检查通过。未配置 TEST_POSTGRES_URL 的真实 PostgreSQL 专用测试标为 skipped，不宣称已执行。
- 无 API/生产代码变更，不需要修改 OpenAPI 或 Console schema；门禁检查同步契约没有生成差异。

## 初次失败记录（没有删除或冒充成功）

- 第一批 `i213-2434b33f8618`：Flowise 默认 HTTP Security 拒绝私网 evidence-tool，Launch `1647b493-8c79-469b-ad46-ca4582ea98ff` PARTIAL_FAILED。另发现 Dataset 写后可见性延迟，使 Manifest 只冻结 2 项；此批不计正式多用例成功。
- 第二批 `i213-680f1f892a78`：重建 Flowise 后凭证无法解密，Launch `2bb744fc-31fb-48a4-83a1-f43b15b653a7` FAILED。原示例误挂 `/root/.flowise`；已改为持久化 `/home/node/.flowise` 并由一次性 init 容器赋予 node 用户目录权限。
- `i213-db7765a3282a`：准备阶段可见性检查误查仅有元数据的 Dataset v2 接口，没有提交 Launch；已改为分页 Dataset Items 查询。
- 正式批的三个正常 Item 均已完成后，旧收集器未识别 COMPLETED；修复终态集合后使用 `--resume` 只读恢复收集，没有再次调用已提交的 Agent。
- 本地测试为允许受控私网工具设置 `HTTP_SECURITY_CHECK=false`；这是降低测试实例出站防护的显式配置，禁止用于共享生产实例。没有修改 Flowise 源码来绕过策略。

## 非结论

不把本项描述为 Langfuse managed Score 回收、Snapshot/Gate 闭环已实现；不保证长期稳定性、生产隔离或 Provider 实现可复现。本项也没有将 HTTP 500 Unauthorized 伪装为 401，或把超时仍运行的模型任务称为被取消。
