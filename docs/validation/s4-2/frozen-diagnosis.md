# S4.2 固定 Snapshot 的 Case 轨迹诊断

核验日期：2026-10-14。关联 #5、#129；交付到汇总 [PR #181](https://github.com/miniceM/Argus/pull/181)。前置为 [T3 正式规则执行](frozen-rule-execution.md)和 [T4 required 证据资格](required-evidence.md)，对应 [T5 实施计划](../../plans/2026-10-13-s4-trajectory/08-implementation-plan.md#9-t5固定-snapshot-的-case-诊断闭环)。

## 固定输入与有限错误

`GET /api/v1/experiment-launches/{launch_id}/trajectory` 必须携带 `snapshot_id` 与 `dataset_item_id`。详细类型以 [OpenAPI](../../openapi.json) 为准。

服务端先校验 Snapshot 属于 Launch、Case 属于该 Snapshot，再通过既有受控 Loader 检查冻结引用、实际 Artifact、Item/Attempt/generation/trace/root 归属与 digest。数据只来自固定 Snapshot 和本地冻结 Artifact；不会访问在线 Langfuse、调用 Agent、回读 checkpoint、选择 latest 或修补历史。

| 场景 | HTTP / 诊断 |
|---|---|
| 缺必填 query | 422 |
| 外部 Launch/Snapshot/Case | 404，不暴露外部身份 |
| 重复 Case、checksum 错误、已加载新契约中的资格、整体结论或 provenance 不一致，测量/Step 证据引用其他 Artifact | 409 `EVIDENCE_CONTRACT_INVALID` |
| `1.0/1.1/1.2` 或没有轨迹契约的旧 `1.3` | 200 / UNAVAILABLE / `HISTORICAL_TRAJECTORY_UNAVAILABLE` |
| 不支持的未来 Manifest 版本 | 200 / UNAVAILABLE / `TRAJECTORY_INPUT_MANIFEST_INVALID` |
| 制品缺失、损坏、引用错配或采集不可用 | 200 / 受控不可用原因；当前逐规则结论 UNKNOWN，无伪造 Step |
| 制品存在且可核验 | 200 / READY，显示冻结 completeness、规则和 Step |

诊断端点允许解释当前不可读取的历史证据；正式 Snapshot、Baseline 和 Gate 仍采用 T4 的严格资格检查。诊断读取不改变历史 `evidence_state` 或 JSON。

## 测量、冻结结论与当前可核验性

每条轨迹规则显示固定 Evaluator/Binding 版本、typed status、boolean value、required/critical、reason code 和来自实际 Artifact 的 `evidence_step_ids`。`succeeded + false` 是已证违规 FAIL；`failed/skipped/no_result` 或无值是 UNKNOWN，不补 0。

所有有测量值或 Step 证据的轨迹结果都须绑定同一个冻结 Artifact 引用，包括 optional Binding；不能只凭正确的 Binding provenance 或相同 Step ID 借用其他来源的测量。

`conclusion` 是后端提供的当前可核验结论；`frozen_conclusion` 保留冻结时策略结论。若制品后来丢失，当前状态为 UNKNOWN，旁注历史结论，二者不会混淆。PARTIAL 中的确证违规仍为 FAIL。整体质量标注为 Snapshot 的冻结值，required 状态标注为冻结时完整度；浏览器不重算 QualityPolicy。

## 数值与 Console 交互

JSON 响应中的轨迹数值保持 JSON number，包括大于 `2^53` 的整数与高精度 Decimal。只读展示 DTO 为 Decimal 显式导出 number schema，领域模型、Artifact codec 和 digest 不变。响应复用 canonical encoder；`step_argument_display_json` 来自同一已验证 Artifact 的参数。浏览器直接展示该字符串，不用 `JSON.parse` 后的浮点值重新序列化。

现有 CaseDetailPanel 展开时懒加载。Query key 包含 Launch、固定 Snapshot、Dataset Item；无 Snapshot 不请求，响应归属、渲染字段的运行期结构及引用摘要校验通过后才进入成功缓存。残缺字段或 null 数组项归一为契约错误，不在 React 渲染期崩溃。页面切换 Snapshot 会收起 Case，重新展开后读取新身份；旧请求迟到不会展示为新 Case。

PASS/FAIL/UNKNOWN 同时带文字和语义 Token；REQUESTED/EXECUTED/BLOCKED 分开呈现。Step 按显式因果边分层，同层说明“无已证先后”；编号、时间戳与 parent 不冒充可信顺序。规则到 Step、Step 到规则均使用可聚焦按钮，重复激活同一目标也重新定位；参数使用可键盘展开的原生 details。Case 行和诊断区都标注 Trace 为在线内容，不参与冻结结论。

## 验收入口

```bash
python -m pytest -q tests/test_trajectory_api.py
pnpm --dir services/console exec vitest run src/features/launches/__tests__/CasesTraceTab.trajectory.test.tsx
python scripts/export_openapi.py
pnpm --dir services/console api:generate
pnpm --dir services/console lint:tokens
make validate
make console-e2e
python -m pytest -q tests/test_trajectory_rule_integration.py -m trajectory_local
```

正式数据库验证提供隔离 `TEST_POSTGRES_URL`；开发时 PostgreSQL skip 不计验收。真实消费者测试沿用已校准的本地 Langfuse 和两个实际 Agent，完成正常执行、恢复、eval-only、optional no_result 后直接读取诊断端点，逐字节规范化比较 API 与 Artifact，并核对参数显示字符串。

初始实际 RED 为 API 13 项、结果完整性 3 项、版本分类 2 项、数值 OpenAPI 1 项，以及 Console 5 项。契约生成工具的类型错误与夹具错误单列，不计行为 RED。

独立阶段审阅一次：Critical 0 / Required 3 / Optional 2。Required 补修的实际 RED 为 optional 引用 2 项、重复聚焦 1 项、残缺响应 5 项（含 3 个渲染异常）；一次修复后目标测试转绿。同时完成两项 Optional：Case 行可见在线标签、OpenAPI 404/409 响应声明。额外保护整体质量字段一致性和错误 schema，实际 RED 2 项。浏览器 axe 的 ARIA 实测失败也已修复；两项旧 Baseline 测试使用明确按钮名替换宽泛匹配，Snapshot 切换测试按既有收起行为断言，均保留验收条件。

最终 PostgreSQL 定向回归 1240 passed、0 failed、0 skipped，其中本端点为 SQLite 24 + PostgreSQL 24，另有 2 项 OpenAPI 检查。`make validate` 八步 PASS：Python 1944 passed、0 skipped、84 项真实采集由独立门禁管理；Console 53 files / 858 tests passed，Token lint、typecheck、build、OpenAPI/types 同步及 Compose 通过。Console 全量 E2E 128 passed，含键盘重复定位、三态、历史原因、Snapshot 切换、axe 和 390×844 几何检查；独立端口已释放。

真实诊断消费者 17 passed、0 failed、0 skipped，107.43s。每份来源报告都确认 API/Artifact 规范化轨迹往返及显示字符串一致，覆盖 native OTel、Pydantic AI、HTTP-only、迟到、未准入、两处恢复和 eval-only/optional UNKNOWN。其余采集与 90 样本校准沿用 [T4 完整 84 项真实门禁](required-evidence.md)；这些领域/Reader/codec/持久化模块本批未修改。

截图为合成用例：[桌面诊断](frozen-diagnosis-desktop.png)、[390px 窄屏诊断](frozen-diagnosis-mobile.png)。为完整截取内部滚动容器，截图视口分别扩高至 1280×2000、390×3200；窄屏几何断言另在 390×844 实测。已人工核对三态、冻结身份、因果层、精确参数与在线标签。最终截图修订后重新执行全量 128 项浏览器回归通过。

版本、原生命令、断言、源码 SHA-256、JUnit/日志与来源矩阵摘要见 [机器可读验收记录](frozen-diagnosis-receipt.json)。原始日志、JUnit 和 metadata-only 来源报告保留在任务验收目录。本批完成 T5；后续为 T6/T7/T8/T9 和 79+10 统一审计，不关闭完整 S4 目标或 #129。
