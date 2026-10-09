# PR #125 后端契约验证与支持交接

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

## 后端负责人目标与问题性质

**上一轮没有确认新的后端产品缺陷。** 本任务不是替前端 F1–F12 改业务语义；职责是验证/补齐后端回归保护、给前端契约与真实形状夹具，以及在证实缺少冻结字段时做最小 read-model 改动。

已有测试能满足验收时复用并提交证据，不机械新增重复测试。现有接口满足全部需求时，允许交付“测试/夹具/契约说明，生产代码零改动”。未经过测试不能声称契约已全部通过。

### 所有权与定位

仅负责 Runner API/领域契约、后端测试、契约形状夹具，以及必要的 OpenAPI/schema 生成。

经知识图谱定位的入口（审查 checkout）：
- `/Users/hosea/.codex/worktrees/pr125-review/Argus/services/eval-runner/app/api_results.py`：`get_launch_result_snapshot`、`_get_snapshot`、`get_run_summary`、`get_comparison_case`、`list_launch_result_snapshots`。
- `/Users/hosea/.codex/worktrees/pr125-review/Argus/services/eval-runner/app/result_snapshots.py`：`create_result_snapshot`。
- `/Users/hosea/.codex/worktrees/pr125-review/Argus/services/eval-runner/app/api_baselines.py`：`get_agent_baseline`、`put_agent_baseline`（该函数实际路由为 POST）。
- `/Users/hosea/.codex/worktrees/pr125-review/Argus/services/eval-runner/app/baselines.py`：Baseline 领域校验/CAS。
- `/Users/hosea/.codex/worktrees/pr125-review/Argus/services/eval-runner/app/models.py`、`comparison_contracts.py`、`result_outputs.py`：响应模型、比较契约与输出可用性。

实施前在实际 checkout 重新确认符号/现有测试；本文不假定后端必须新增端点或存储。

## BE-01 冻结结果与 Snapshot 归属契约

支持：FE-01/FE-03/FE-05。

**工作**：核对 Snapshot detail/summary/Manifest 对 Header、KPI、Case 明细和导出的覆盖，明确每个显示项的冻结来源。优先现有 detail.items/summary/manifest，不提供 mutable /items 当历史结果替代。

**验收**：
- [ ] 创建 S1，再更新 live/创建 S2；重新读取 S1 的 items、quality/evaluation、policy explanations、latency/cost、output refs、Manifest 和版本身份仍与最初相同。同步等独立运行态字段另行标注，不要求可变同步元数据字节级不变。
- [ ] Snapshot 属于另一个 Launch 或不存在时明确 4xx（以当前合约为准，通常 404），不能返回本 Launch latest；无权访问遵循现有权限契约，不新造鉴权机制。
- [ ] Snapshot detail、summary、comparison 的请求身份/响应身份可核对一致；input 使用冻结 Manifest 而非 latest Dataset。
- [ ] 给前端一份“Header/KPI/Cases/Comparison/Export → endpoint/field → frozen/live”映射，列出正式质量结论是否存在的事实。
- [ ] 无冻结聚合质量结论时：由前端展示明确的冻结统计或双方确认最小冻结 read-model 方案；不拿 live conclusion 补洞，不借此引入新的 Gate 规则。

## BE-02 比较、数值类型与证据完整性契约

支持：FE-02/FE-03。

**工作**：检查 formal availability/reasons、cohort scope、typed results、阈值/单位/方向及 cost coverage 的现有字段；提供明确语义和边界响应。

**验收**：
- [ ] 80% vs70% 且未配置90%规则，后端不会凭空产生90%门禁；前端展示规则能追溯冻结 Policy。
- [ ] 正式可比且符合既有退化判定条件的100%→50%夹具返回退化裁决；Policy/比较契约不一致、证据不完整时正式 verdict 不可用并有原因，不能靠 cohort 诊断生成正式通过。
- [ ] numeric `call_cost <= 0.2` 保留数值/比较符；方向/单位若来自现有定义或冻结配置，明确来源；无元数据时前端中性显示，不擅自声明百分比。
- [ ] UNKNOWN、未采集cost、真实0、partial coverage、不同币种/方法分别有可区分的响应；禁止用0填未知并参加全量成本比较。
- [ ] 冻结质量全量口径与可比 cohort 口径能分别解释、测试，不能通过改变聚合分母迁就现有 UI。

## BE-03 Baseline CAS/资格与冻结引用回归

支持：FE-04。当前已确认问题是前端409后未刷新/重新确认，不是要求后端放宽冲突检查。

**验收**：
- [ ] 客户端读到revision R1；另一客户端绑定后变成R2；带R1写入返回409，绑定保持R2，不误覆盖。
- [ ] GET返回R2及实际绑定；客户端重新明确确认并带R2提交后成功，revision按合约推进。
- [ ] 不存在、跨业务归属或不符合 eligibility 的 Snapshot 被拒绝，且失败不改变绑定；校验范围遵循真实 Agent/environment/数据契约。
- [ ] Live 重试期间，历史合格 Snapshot 的资格依据该冻结结果，不因 live mutable quality/status 被误判；是否允许绑定遵循既有领域规则。
- [ ] 当前 Baseline binding 改变，不改变既有 Candidate 已冻结的 Baseline 引用和历史比较。
- [ ] 说明409响应形状、fresh GET字段和更新语义；不增加无条件 overwrite、自动取 latest revision 或忽略 expected_revision 的分支。

## BE-04 冻结 Output/Trace 与单侧用例契约

支持：FE-03。

**验收**：
- [ ] 对选中 Snapshot/Case 读取精确冻结 observation_id/trace_id；新增 observation 后历史引用仍不变，不 fallback newest。
- [ ] 无 Baseline 的 Candidate 输出仍可查询；Candidate-only、Baseline-only、双方存在、双方缺失返回准确对应身份和可用性。
- [ ] 过期/不可用/临时故障和业务无证据可区分；可重试故障不包装成正常空输出，前端可根据合约实现重试。
- [ ] Trace deep link 对应真实 Case/Experiment；不引入 Argus 全量 Trace/Output 副本，不扩大敏感数据暴露。

## BE-05 交付前后端共享的真实合约夹具

**工作**：提供 schema-valid 的响应样本/测试 fixture；每个样本注明请求身份、字段来源、预期 UI 断言、是否用于 mock 或真实 API 集成。前端负责组件/E2E适配，不直接争抢同一测试文件。

**最小场景集**：
1. 合格正常结果、FAIL、UNKNOWN、无 Baseline、Policy变化/不可正式比较。
2. S1冻结失败、S2/live通过；重试进行中和完成后；故意让live与历史不同。
3. 100%→50%退化、70%→80%但无90%规则、cost0.2/lower-is-better。
4. 缺失/真实0/partialcost、混合币种或方法。
5. 不存在/跨Launch Snapshot、现有权限拒绝、CAS409、上游503。
6. 50+ Cases、单侧Case、输出不可用和暂时故障。

**验收**：
- [ ] 正常响应样本符合生成模型；错误样本匹配实际API错误形状，不拼凑不存在字段。
- [ ] 夹具含有可信冻结summary/typed evidence，且live/历史差异足以捕获F1，不用都相同的“全绿样本”。
- [ ] 接口差异清单为空，或每项缺口有具体响应证据及双方确认的最小补齐方案。
- [ ] 对每个BE编号给出既有/新增测试位置和实际执行结果，未执行不写PASS。

## 条件性产品修改：仅证实数据缺口后执行

可能需要补充的内容：冻结 Header 结论、数值单位/方向、缺失原因、可重试错误读模型。以上只是待核对项，不是已确认缺陷。

必须先回答：
1. 现有 Snapshot/Manifest/typed result 是否已有该信息？若有，前端直接复用。
2. 是否能从不可变现有字段无歧义展示？若能，不新建接口。
3. 真缺失时，信息应在冻结时保存还是可从既有不可变数据投影？禁止回读 latest 拼“历史”。
4. 是否需改 API？只有答案为是才修改响应模型、导出 OpenAPI、生成 Console schema，并跑一致性门禁。不手改两个生成快照。

字段名/端点/存储设计需双方确认后再实施；不引入未经批准的新领域模型、Release判定、数据库迁移。

## 测试与执行顺序

1. 核对PR最新head和工作区；先输出字段映射与现有测试覆盖，供前端立即并行修FE-01/02。
2. BE-01/02/03优先：缺保护时先写回归并确认RED，再做最小实现；已有行为正确只需验证/补测试。
3. BE-04/05：输出单侧证据与故障夹具，双方联调409、导出、历史结果。
4. 条件性合约改动后在实际checkout根目录执行：

```bash
python scripts/export_openapi.py
pnpm --dir services/console api:generate
```

运行时使用项目配置的Python3.12环境；运行针对实际修改的pytest模块，再执行：

```bash
pytest -q tests
make validate
```

5. 实施授权后将契约/回归保护按自洽单元做小步本地commit；检查生成快照diff及无关改动。本文没有授权push/发布。
6. 最终确认相关远端Python/契约门禁，并与前端联合跑真实API E2E；外部Langfuse条件不足标BLOCKED，不拿mock结果冒充全链路通过。

## 不属于后端修改范围

- F1/F2页面取数/fallback；F3/F4/F5阈值/文案/单位格式；F6页面错误态。
- F7刷新及重新确认；F8下载/clipboard；F9/F10布局；F11view-state；F12进度展示。
- 取消确认、Tab键盘、More菜单、测试选择器迁移。
- 为90%/500ms硬编码UI添加业务规则，放宽CAS，修改聚合分母，把UNKNOWN映射PASS，把缺失cost改0。

## 后端交付清单

- [ ] 字段映射、契约缺口结论、真实形状场景夹具。
- [ ] BE-01–05逐项PASS/FAIL/BLOCKED/NOT RUN与测试证据。
- [ ] 如需产品改动，说明已证实的缺口、最小diff、兼容性及OpenAPI/schema同步。
- [ ] 无产品改动时明确“契约已验证，修复落在前端”，不要为了分工制造后端重构。

---

## 执行记录（2026-10-09，后端角色已完成）

**落地位置**：独立审查 checkout `/Users/hosea/.codex/worktrees/pr125-review/Argus`，分支 `codex/pr125-backend-contract`（基于 PR 测试合并提交 `2d212eb`，PR head 执行前实时核对仍为 `2acad659`）。

- `4a5bd46` — `tests/test_pr125_launch_detail_contract.py`（BE-01/BE-02/BE-05 契约验证，5 用例）
- `b748e86` — `docs/plans/2026-10-09-pr125-backend-contract-mapping.md`（字段映射、三个响应形状陷阱、接口缺口清单为空、场景夹具表、逐项验收状态）
- 仅本地提交，**未 push**；生产代码 diff = **0**（契约已验证，F1–F12 修复落在前端 FE-01–FE-07）。

**逐项状态**：BE-01 PASS / BE-02 PASS / BE-03 PASS / BE-04 PASS / BE-05 PASS / 条件性产品改动 NOT NEEDED（四个候选缺口均被现有字段否定）。

**门禁证据（实际执行）**：新测试 5 passed；ruff PASS；完整 `pytest -q tests` **756 passed, 4 skipped**（4 项均为 `TEST_POSTGRES_URL` 未配置）；`make validate` **PASS（8/8，含 Console 526 vitest 与 OpenAPI 同步）**。

**远端只读回查**：Code Quality / Full Python Tests / Docker-Compose **pass**；Console Quality & E2E **fail**（前端范围）；Langfuse Cloud E2E skipping。

**NOT RUN**：新产物远端 CI（无 push 授权）、前端联合真实 API E2E（前端修复未开始）、真实 Worker→Agent→Langfuse 全链路。
