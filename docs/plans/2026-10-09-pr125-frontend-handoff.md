# PR #125 前端修改交接与验收标准

日期：2026-10-09（Asia/Shanghai）

## 依据与执行边界

- 本文是上一轮 PR #125 审查的职责拆分，不是本轮重新完成产品验收。
- 审查 head：`2acad659ebf8526145f1072cbf94b2dc4198b1ba`；测试合并提交：`2d212eb4a1079a975278aa7974e8e3b84962b052`。
- 原始证据：[审查报告](/Users/hosea/.codex/visualizations/2026/10/08/01a11ae7-9963-77e1-a7f6-e6df6eb9acac/pr125-review/review.md)。F1–F12 是该报告的问题编号，不是新 GitHub Issue。
- 本文代码定位以独立审查 checkout `/Users/hosea/.codex/worktrees/pr125-review/Argus` 为准；实际实施前核对 PR 最新 head，并重新定位，不能在旧聊天 checkout 中假定已有这些组件。
- 本轮只新增交接文档，没有修改产品代码、启动实施代理、创建 Issue、提交、推送或发布。
- 对应验收范围：#120 页面/版本上下文；#121 KPI/比较；#122 Cases/证据；#123 审计/导出；#124 操作/运行生命周期；#119 汇总验收。

## 双方共同遵守的规则

1. 报告身份统一为 `(launch_id, snapshot_id)`；Header、KPI、Cases、Comparison、结果导出全部绑定该身份。Live 执行状态、同步状态另行标注，不冒充冻结结果。
2. S2 出现不能改写 S1；显式无效/跨 Launch ID 不能回退 latest。没有指定 ID 时可按既有产品规则选默认版本，但必须先解析并固定身份。
3. 质量 PASS/FAIL/UNKNOWN、证据 COMPLETE/DIAGNOSTIC、同步状态、Baseline eligibility 是不同维度。
4. 全量质量通过率是 PASS / 全部 Cases；已判定样本比率、可比 cohort 比率如展示，单独标明口径。UNKNOWN/缺失不变成 PASS 或真实零值。
5. 规则来自冻结 Policy；正式比较来自权威 comparability/formal verdict。禁止为了 UI 新增 90% 或 500ms 门禁。
6. 不复制 Langfuse Trace/Output，不修改业务 Agent 的评测 SDK 边界，不扩大到 Registry、调度器、新存储或数据库迁移。
7. 后端负责 API 合约及其生成快照；前端不得手写重复 API 类型或手改生成文件。只在确有合约改动时同步 OpenAPI/schema。

## 前端负责人目标

修复已确认的页面数据混用、错误质量裁决和操作闭环，完成布局/状态/键盘验收。不是仅重新排版三个 Tab。

### 所有权

- Console 实现、页面 view-state、Vitest、Playwright。
- 关键定位（以下是审查 checkout 的绝对路径）：
  - `/Users/hosea/.codex/worktrees/pr125-review/Argus/services/console/src/features/launches/LaunchDetail.tsx`
  - `/Users/hosea/.codex/worktrees/pr125-review/Argus/services/console/src/features/launches/LaunchHeader.tsx`
  - `/Users/hosea/.codex/worktrees/pr125-review/Argus/services/console/src/features/launches/SetBaselineModal.tsx`
  - `/Users/hosea/.codex/worktrees/pr125-review/Argus/services/console/src/features/launches/ExecutionProgressPanel.tsx`
  - `/Users/hosea/.codex/worktrees/pr125-review/Argus/services/console/src/features/launches/tabs/GateComparisonTab.tsx`
  - `/Users/hosea/.codex/worktrees/pr125-review/Argus/services/console/src/features/launches/tabs/ManifestAuditTab.tsx`
- 后端接口已能提供的冻结数据先直接消费；不要等待一个没有证据表明必要的“统一新接口”。如有字段缺口，向后端提供具体显示项、现有响应、冻结来源要求。
- UI 改动必须阅读实际实施 checkout 的设计系统 Skill，复用组件与语义 Token。

## FE-01 [P1] 统一冻结报告数据与身份校验

对应：F1/F2；#120/#121/#122/#123/#124。

**问题**：KPI/Cases/Header 消费 live 数据，而比较/输出消费选中 Snapshot；错误 snapshot ID 又允许最新版本进入 Header/绑定操作。

**修改方向**：页面维护唯一已校验的 Result View Context；冻结明细与 summary 是报告来源，Live 状态只用于独立运行区。查询缓存/异步结果按身份隔离，切换后不得短暂展示旧身份为新身份。没有冻结质量结论字段时，先按已有冻结统计展示，不从 live quality_conclusion 补值、不另造正式 Release 判定。

**验收**：
- [ ] 固定 S1=0 PASS/2 FAIL，live/S2=2 PASS；Header、KPI、Case ID/状态、展开输出、比较和导出始终属于 S1，通过率为 0%，不能显示 S2 的 100%。
- [ ] S2 新增、轮询、刷新、复制 URL 再打开，都不改变固定 S1；明确切到 S2 后所有报告区一起切换。
- [ ] Snapshot 加载中、404、无权限、跨 Launch 时，展示相应状态；不显示 latest 报告，不允许绑定 Baseline 或导出该结果；确认按钮不能发出写请求。
- [ ] 快速 S1→S2 切换、慢响应乱序、跨 Launch 导航，不串数据。
- [ ] 没有 Snapshot 的运行中页面仍能展示明确的实时进度，但不能伪装成已有正式报告。

## FE-02 [P1/P2] 修正 KPI、门禁与比较语义

对应：F3/F4/F5/F6；#121。

**修改方向**：去掉固定 ≥90%、<500ms 和默认正向文案；由冻结规则、formal verdict、delta 和 metric direction 驱动。按类型/单位格式化，未经契约声明不得将 numeric 当百分比。

**验收**：
- [ ] Baseline 70%→Candidate 80%，没有 90% Policy：不出现“达到 90% 要求”；没有 latency Policy 不出现 500ms 门禁。
- [ ] 100%→50%：显示下降 50 个百分点；若正式 verdict 为 REGRESSION，则摘要、表格、颜色和 Cases 入口一致，不出现“提升/达标”。
- [ ] 33.3%→100%：显示约 +66.7 个百分点，不把百分点写成相对百分比。
- [ ] `call_cost <= 0.2` 显示原数值及已有单位，不显示 20%；0.3→0.1 在 lower-is-better 语义下是改善。方向未知时中性展示，不推断改善。
- [ ] UNKNOWN、无 Baseline、Policy 不同、证据不完整：明确原因，不出现未受支持的发布通过结论；cohort 诊断不得当成全量质量指标。
- [ ] 缺失 cost 为未采集/不可用，真实 0 保留 0；partial coverage、币种/方法不兼容明确说明，不拼接成全量总成本。
- [ ] summary/comparison/Baseline 的 loading、合法 empty、403/404/503 error 区分；错误可重试，不能显示“暂无可比数据”掩盖请求失败。

## FE-03 [P2] Cases 筛选、证据与定位闭环

对应：F1/F11 及补充缺口；#122。

**验收**：
- [ ] 每个筛选操作改变真实 Case ID 集合和计数；筛选无结果有明确空态，清除筛选恢复完整集合，50+ Cases 不遗漏。
- [ ] KPI/Regression 入口不仅切 Tab，还携带目标类别/Case 定位；目标不存在时有明确反馈，不错误展开另一 Case。
- [ ] input 来自冻结 Manifest，output 通过冻结 observation_id/trace_id 懒加载；显示真实 typed results、阈值和 policy explanation。
- [ ] 无 Baseline 时 Candidate 输出仍可用；Baseline-only、Candidate-only、双方均存在/均缺失分别展示正确证据和原因，不能只实现 Candidate 一侧。
- [ ] Trace 链接指向当前 Case 的实际证据目标，不是 Langfuse 首页或最新 observation。
- [ ] 懒加载错误、重试、切 Snapshot 的旧请求不会污染当前展开区。

## FE-04 [P2] Baseline CAS 与破坏性操作确认

对应：F2/F7；#124。

**验收**：
- [ ] 确认弹窗展示实际目标 Snapshot 和当前已读 Baseline revision；请求携带该 `result_snapshot_id` 和用户看到的 `expected_revision`。
- [ ] 取消/关闭弹窗发出 0 次写请求；提交时禁用重复动作。
- [ ] 并发修改导致 409：明确冲突，刷新 Baseline，展示新 revision，并要求重新确认；不静默覆盖、不自动重试写入。
- [ ] 关闭重开不会一直拿旧 revision；用户重新确认后使用新 revision 成功。
- [ ] 目标为无效/非 eligible Snapshot 时不可提交；Live 重试期间是否允许历史 Snapshot 绑定，依据该 Snapshot 的资格而不是简单用 live status 禁止。
- [ ] Cancel Launch 必须二次确认：取消确认不发请求，确认后仅一次请求并反映后端状态；按钮遵循 allowed_actions。

## FE-05 [P2] 结果导出与复制的真实成功/失败

对应：F8；#123。

**验收**：
- [ ] Manifest 导出和 Result Snapshot 导出明确区分；结果明细加载中或失败时禁用结果导出，不以 Manifest 替代结果。
- [ ] 下载 JSON 可解析，含选中 Snapshot 身份，内容与冻结明细对应；切换 S1/S2 后各自下载正确，不仅检查“按钮存在”。
- [ ] 模拟详情 503：没有伪结果文件或成功提示，恢复后可重试。
- [ ] Clipboard Promise 成功后才提示成功；拒绝/权限失败有可见失败提示。下载触发失败也有反馈。

## FE-06 [P2] 首屏层级、窄屏与 Tab 状态/键盘

对应：F9/F10/F11 及补充缺口；#120/#123。

**验收**：
- [ ] 完整 ResultSnapshotPanel 摘要、digest、历史/审计说明归入 Audit；全局仅留紧凑版本身份与必要历史提示；移除原型 A/B 演示选择器。
- [ ] 1280×800 默认首屏能看到业务 Header、四 KPI 和三 Tab 导航；不能先铺满审计元数据后才进入决策区。
- [ ] 360/390/768px，包含 Baseline/Retry/Langfuse 等完整终态动作组合：主页面无横向溢出；使用紧凑控件/More 菜单，所有操作仍可键盘访问。表格/JSON 可局部滚动。
- [ ] 主按钮遵循 32px 与既有设计体系；lint:tokens 通过，无新增裸颜色。
- [ ] 同一身份切 Tab 保留筛选/展开；切 Launch/Snapshot 按身份清理旧选择，不携带旧 Case。
- [ ] 非法 tab 安全回退到合法默认 panel；URL/前进后退与可见 panel 一致。
- [ ] Tabs 支持 ArrowLeft/ArrowRight/Home/End、正确焦点与 roving tabIndex；键盘切换行为和 aria-selected/tabpanel 一致。不能用 Axe 通过代替这些断言。

## FE-07 [P2] 进度、重试与同步生命周期

对应：F12；#124。

**验收**：
- [ ] 无手动覆盖时 RUNNING→终态进度默认收敛；终态→execution/evaluation recovery 能展示正在进行的阶段，不因首次 useState 固化。
- [ ] 用户主动展开/折叠的覆盖规则明确且有测试；评价恢复和同步状态不被“Launch 已完成”隐藏。
- [ ] 轮询在相关执行/评测/同步仍活跃时继续，全部终结后停止；Live 更新不改写冻结报告。
- [ ] evaluation-only retry 不导致另一轮 Agent invocation；通过后端请求/fixture 与集成证据验证，不只观察按钮颜色。

## 测试与实施顺序

1. 为 FE-01 写失败回归并运行 RED，修共享身份与冻结取数。
2. FE-02→FE-03：修实际生产 Tab 的语义和用例证据；迁移旧 ComparisonReport 测试的关键业务断言到实际被渲染组件，不能只继续测废弃路径。
3. FE-04→FE-05：补 409 恢复、取消零请求、JSON 解析、clipboard rejection。
4. FE-06→FE-07：键盘/响应式/身份状态/运行转移 E2E。
5. 对历史导航的 `v1` vs `v1 (Candidate)` 定位做合理迁移，保留版本身份断言，禁止简单删除测试。
6. 实施授权后按上述自洽单元做小步本地 commit，提交前检查 diff 不含他人/无关改动；push/发布不属于本文授权。

在实际实施 checkout 根目录执行：

```bash
pnpm --dir services/console typecheck
pnpm --dir services/console lint:tokens
make console-test
pnpm --dir services/console build
make console-e2e
make validate
```

本交接没有运行上述修复后门禁。上一轮原有 526 个前端测试通过，但 11 个定向探针失败，局部 Playwright 32 通过/1 失败；不能引用历史绿色作为修复后完成证据。真实 Worker→Agent→Langfuse 没有在该轮重新全链路验证。

## 前端交付清单

- [ ] 每个 FE 编号列出修改文件、回归测试和 PASS/FAIL/BLOCKED/NOT RUN。
- [ ] 对照 F1–F12 及额外 AC 缺口逐项关闭，给出实际运行证据。
- [ ] 提供桌面/窄屏、退化/UNKNOWN、409、下载等操作结果，而不只提供静态截图。
- [ ] 远端 Console CI 恢复绿色；联合验收时明确真实 API E2E 与受控 mock 的界限。
- [ ] 没有向后端要求放宽 CAS、创造门禁阈值或将 UNKNOWN/缺失映射成通过。
