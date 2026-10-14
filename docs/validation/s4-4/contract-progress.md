# T8 Judge 初始契约与校准统计

日期：2026-10-14。关联 #131；依照已批准 [S4.4 §4/§6](../../plans/2026-10-13-s4-trajectory/06-s4-4-judge.md)。本记录只证明初始模块的局部行为，**不证明 Judge 真实校准或整阶段完成**。

## 已实现边界

`trajectory/judge.py` 新增严格输出策略、响应模型及有限解析结果。JSON 经既有有界 codec 解码，重复键、缺字段、额外键、非有限数字、非法 enum、隐式 string/bool 置信度、超长文本均形成 failed / JUDGE_OUTPUT_INVALID；不补零、不伪造 FAIL。PASS/FAIL 必须满足冻结置信阈值与当前本地 Step 集引用，UNCERTAIN、低置信或非法/缺失引用为 no_result。只有 rubric 策略显式允许时可使用空引用。Provider 自由文本 rationale 在不能证明安全前省略，不进入解析结果的 repr 或下游持久化数据。

校准标签固定 Case、近重复 group、轨迹摘要、risk 与 truth；策略显式固定 holdout digest、风险切片、最小违规数、阈值及重复次数。development/holdout 的 Case、group 或轨迹摘要重叠均拒绝；holdout 内重复 Case、group、轨迹摘要也拒绝，不能通过重命名增加独立样本数。缺观测、重复观测、越界 repeat、未知 Case 或 UNKNOWN 不能准入。每个风险、每次重复分别计算误放行与误拒，Wilson 单侧 95% 使用 z=1.644854；不池化重复观测。分歧率以重复判定不一致的 Case 数为分子，并逐风险检查阈值。违规分母为 0 时误放行率及 Wilson 上界保留 null，不制造 0%。

报告保存冻结策略 version/holdout digest、输入覆盖率、样本数、违规数、逐次误放行率/上界/误拒数与 Case 分歧率。这里的 eligible **仅表示统计合格**：实际执行准入仍须验证真实 Provider、冻结 Model/Prompt/实现制品与校准来源；critical 还须同风险独立来源及联合校准。当前尚无执行器消费此报告，不能以手工标签或 mock 观测获得正式发布资格。

Demo 数学夹具明确为 30 个独立违规、两次重复、阈值 0.10：0/30 的上界 0.082724，统计合格；1/30 上界约 0.136，统计拒绝。误拒记录不扩大违规分母。

## RED / GREEN 与审阅

- 初始 40 项 RED：模块缺失。实现后 GREEN 40 项。
- 追加同轨迹换 Case/group 的回归：实际 RED 1 failed；加入 holdout 摘要唯一性后 GREEN 42 项。独立审阅指出相同遗漏，已用该回归关闭。
- 报告身份/覆盖字段、无违规分母不得假 0 分别得到 RED，补齐后最终目标测试 44 passed。
- 独立只读复审未发现其他阻断问题；审阅不代替实测，也未覆盖尚未实施的 Provider 或 critical 准入。
- ruff、git diff --check 通过。最终源码 `make validate` 8 步通过：隔离 PostgreSQL 的 Python 2378 passed / 0 skipped / 86 real deselected（159.56s），Console 864 passed；Token lint、typecheck、build、OpenAPI/schema 同步与 Compose 校验通过。真实 Judge 推理/校准尚未运行，86 个真实集成不属于本地门禁的通过数。

```bash
python -m pytest -q tests/test_trajectory_judge.py
ruff check services/eval-runner/app/trajectory/judge.py tests/test_trajectory_judge.py
make validate
```

## 剩余范围

冻结 Judge Definition/Provider/Resolved Model/Prompt/制品、安全请求构造、确定性触发、正式 required 全量覆盖、异步 HTTP 与取消、跨 Worker 持久预算、恢复/迟到 CAS、Evaluator 注册及 typed 汇总、critical 双来源准入、真实独立留出集校准、Snapshot/API/Console 展示与 T9 89 项审计仍须继续实施。本切片不自动启用 Judge，不改变历史 builtin、Demo 或现有正式门禁。
