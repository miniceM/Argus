# T8 Judge 集成预检

日期：2026-10-14。批准方案：[S4.4](../../plans/2026-10-13-s4-trajectory/06-s4-4-judge.md)。本预检仅记录当前实现与接入约束，**不表示 Judge 已实现或校准通过**。

## 当前实现与接入位置

已直接核对 `evaluator_binding.py`、`worker.py`、`evaluation_recovery.py` 与 `evaluators.py`。图谱 generation 为 2026-10-01，不能覆盖这些 S4 增量；相关结论以当前 worktree 源码为准。

| 接口 | 当前事实 | T8 必须落实的接入 |
|---|---|---|
| `evaluate_frozen_item` | 同步入口已有 keyword-only `context`，只注册 builtin 执行器，轨迹规则在 builtin 中受控执行 | 增加 `trajectory_judge` 执行器与 keyword-only `prepared_results`；仅消费预先准备的结构化结果，不在同步入口开启事件循环 |
| Worker 成功执行路径 | 先采集轨迹，再在 `asyncio.to_thread` 中装载 Context 并评测 | 短事务装载固定输入与预算身份；先运行确定性触发检查，再异步准备 Judge，最后交给已有同步评测/质量聚合；等待 Provider 时不持有 DB 锁 |
| eval-only recovery | 同步 claim / prepare / evaluate / merge / finalize，复用原响应与固定轨迹，目标只含失败或缺失 Binding | 拆出 prepare / async await / finalize，保留既有同步路径兼容；异步路径只能重评目标 Binding，不能调用 Agent |
| recovery 目标集合 | 当前 `only_binding_ids=needed_ids or None`；空集合会被解释为全量评测 | Judge 异步准备必须保留“空集合”和“未指定”的差别，全部结果可复用时不调用 Provider，成功结果原样保留 |
| finalize | Worker 与重评分别有 lease / generation / cancellation fencing | Judge 的迟到响应沿用相同 CAS；失去租约、取消或旧 evaluation generation 不得写入新结果 |

## 实施与 RED 顺序

1. 严格 Judge 配置、冻结 Provider/API/Resolved Model/Prompt/Rubric/实现与依赖身份；拒绝 alias、fallback、未知字段和非有限预算。配置变化进入正确测量摘要。
2. 仅从受控冻结制品构造模型输入，按配置允许的 criteria 取值；不向 Provider 传任意业务 output/expected_output、Header 或原始参数。超量不截断为可通过输入。
3. 验证 PASS/FAIL/UNCERTAIN、置信阈值、有限值、枚举、长度及本地 Step 引用。UNCERTAIN/低置信为 no_result，传输或结构失败为 failed；均不补零、不伪造 FAIL。
4. 追加 migration 020 的 nullable Launch 预算状态，旧 NULL 原样保留；row lock 下按 `(item_id,evaluation_generation,binding_id)` 原子预留，发送标记在网络等待前提交。发送后未知用量保留保守预留，重复 delivery 不二次发送，显式新重评 generation 仍受 Launch 总预算约束。
5. 测试跨 Worker 并发、两阶段崩溃、超时、取消和迟到 CAS；Provider 并发有界。诊断的 all-pass 场景零调用，正式 required 使用全量冻结 Dataset cohort。
6. 接入 Worker 与异步 recovery，自动验证重评不调用 Agent、空目标不调用 Judge、旧结果不被覆盖、缺准入 UNKNOWN。
7. 固定 development / 独立 holdout / near-duplicate 身份、风险切片、两次独立运行及 Wilson 单侧上界。每切片至少 30 个违规样本；示例阈值 0.10 下，零 false acceptance 的上界约 0.0827，1/30 的约 0.136 不能准入。
8. critical 必须绑定同风险的确定性规则 + Judge，或两个不同冻结来源并完成联合校准；重命名和重复 Prompt/Model 身份不能充当独立来源。仅有诊断采样、缺校准或未获准 Provider/Model 时不可发布。

以上内容逐项对应批准方案的 S44 验收标准，目标验证包括真实 Provider / 固定模型与独立留出集，不能用 mock Provider 测试替代校准。

## 前置状态

T7 确定性增量已在本地提交并合入 `main@49aaaa0`，`make validate` 2326、Console 864、E2E 129 项通过；固定 `23807ee7d4f2dcb2c2e2b9ccca25257063b87078` 的完整真实 86 项已全部通过（960.23s），运行期间未改其生产源码或 HEAD。

首次推送被拒后核验：仓库维护者将 `main@5d643c7` 的 Cloud CI 修复以 `cb447f9` 合入 PR 分支。真实进程结束后已合入该远端提交，保留双方提交与测试；新增 Cloud 脚本门禁复验后推送同一 PR #181，不强推。

主线新增测试的 import 排序作最小修正后，最终 `make validate` 为 2331 passed / 0 skipped / 86 real deselected；真实运行的 101 个源码指纹不变。

T8 已开始实施严格输出验证与校准统计，见[初始契约证据](contract-progress.md)。真实 Provider / 固定模型、独立校准制品、异步 Worker 与预算以及 T9 的最终发布证据仍需落实；本预检不代替这些验收。

## 本地 Provider 只读探针

2026-10-14 已按本机环境与 oMLX 手册，使用官方配置链在请求内取得凭据，仅探测回环服务的模型目录和状态，未打印凭据、修改配置或发送推理请求。

- oMLX 状态返回 version=0.7.0、active_requests=0；模型目录 HTTP 成功，包含 KAT-Coder-V2.5-Dev-OptiQ-4bit、Qwen3.6-35B-A3B-MLX-6bit、Qwen3.8-27B-MLX-4bit 等显式文本目标。
- 探针时只加载了 Embedding 模型；目录可见性不证明文本模型推理可用，也不证明 Judge 校准通过。
- 这些目录名不能单独充当不可变 Resolved Model 身份。后续真实验证必须固定模型制品版本/摘要、Provider API 版本、采样与 Prompt，验证响应身份并拒绝 alias/fallback；实际模型调用与 holdout 校准仍待实施。
- 本机端点、鉴权方式与资源约束只用于隔离验证，不作为 CI 或生产的隐式默认配置。
- 服务自身 OpenAPI 的 `ChatCompletionRequest` 声明 `model/messages/temperature/top_p/max_tokens/seed/response_format/chat_template_kwargs/stream` 等字段，默认非流式；`model` 仅声明字符串，未提供不可变制品身份保证。Schema 声明不等于结构化输出实测，Provider 仍必须验证实际响应与冻结身份。
