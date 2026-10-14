# T8 单次有界异步 Judge 传输

日期：2026-10-14。关联 #131；依据 [S4.4 §4–§5](../../plans/2026-10-13-s4-trajectory/06-s4-4-judge.md)，依赖[安全请求准备](input-progress.md)。本切片完成 OpenAI-compatible HTTP Adapter 与准备来源边界，不表示真实 Provider/模型准入、正式 Worker 执行或留出集校准已经完成。

## 传输与生命周期

`JudgeProvider` 异步协议返回已解析的安全结果、可选 usage、固定调用摘要和耗时，不返回原始响应/rationale。`OpenAICompatibleJudgeProvider` 仅接受 Runner 配置的固定端点与 Provider/API/Model/模型制品摘要声明；声明必须匹配冻结 Definition，响应 model 必须完全匹配。声明一致不能证明实际模型制品，正式准入仍须验证真实解析与结构化能力。

单次 POST `/chat/completions`，非流式模型输出、冻结 JSON Schema 与严格本地二次解析；不重试、不回退。HTTP 响应以异步 stream 有界读取，固定 128KiB，拒绝压缩、非 JSON、重复键、非有限数、过深 JSON、多 choice、非 stop 或 refusal。使用 UTF-8 JSON 原生精确 Decimal 解码参数，避免 float 改变冻结值。无有效结果不补零、不产生 FAIL。

进程内 semaphore 上限 2；等待槽与请求读取都在 `asyncio.timeout` 的冻结总预算内，HTTP connect/read/write/pool 各自限时。CancelledError 上传，response context 关闭并释放 slot；HTTP、网络、超时和结构异常仅返回稳定原因码，不携带异常原文、响应体或凭据。Provider usage 缺失保持 None，下游持久账本须保留最坏预留；本切片不把费用记录当预算限额控制。

## 准备来源证明

独立审阅发现，公开 input_digest 可由调用者重算，不能证明消息经过安全准备。先实测复现“添加 Bearer 字段并重算公开哈希仍发送”，随后加入进程随机 HMAC seal。复审又发现纯 builder 签发 seal 会绕过 DB/source；该路径也实测 RED 后修复。

只有 `prepare_judge_request` 在验证冻结 Manifest、Checkpoint/Artifact 归属及权威 criteria 后签发 seal；纯 builder 返回无 seal 的中间值。seal 绑定 scope/ref、definition/criteria、完整消息摘要和本地 Step 集，Provider 出站前验证 seal、身份、消息边界和精确顶层字段。seal 不参加可复现 input_digest，不显示在 repr、不落库、不跨进程复用；恢复需从原证据重新准备。它证明本进程来源准备完成，不替代最终 lease/generation CAS 或 Provider 模型准入。非法摘要元数据在失败 outcome 中为 None，不回显任意原文。

## 实测

- 初始目标测试 RED **26 failed**（Provider 模块缺失）。首次 GREEN 后发现 fixture 的 `json=` 预读取正文与实际异步响应流不一致，改为未消费的 AsyncByteStream，没有放宽生产读取限制。
- Decimal 精确发送回归 RED **1 failed**：`0.1234567890123456789012345678` 被 float 舍入。改用 canonical 数值编码后保留原值。
- 公开摘要注入与非法摘要回显 RED **2 failed**；HMAC 来源与稳定元数据过滤后 GREEN。
- 纯 builder 绕过实际 DB/criteria 来源 RED **1 failed**；seal 仅在实际准备入口签发后 GREEN。所有正常 Provider 路径使用真实 SQLite Checkpoint/Artifact 准备，无伪造 owner 或测试专用 seal。
- 极端 JSON 指数导致 Decimal 实现溢出的 Provider 外层/内层回归 RED **2 failed**；响应 parser 与共享 canonical codec 捕捉 DecimalException，转成稳定无原文错误；另有独立 codec 原因码断言。
- 有效结构化单次调用、各类异常、并发 2、事件循环心跳、等槽超时、取消、响应超限与未知 usage 均覆盖。两个真实回环 TCP 测试分别取消与超时，服务端观察 EOF，证明客户端关闭实际连接；它们不是模型推理或校准测试。
- 来源修复后的六个相关模块 **250 passed / 0 skipped**（29.98s）；后续极端指数修复进一步扩展 codec 兼容回归，结果见最终门禁。ruff 与 diff 检查通过。

```bash
python -m pytest -q tests/test_trajectory_judge_provider.py tests/test_trajectory_judge_input.py tests/test_trajectory_judge_definition.py tests/test_trajectory_judge.py tests/test_trajectory_evidence.py tests/test_trajectory_inputs.py
make validate
```

最终源码 `make validate` 8 步通过：隔离 PostgreSQL 的 Python **2492 passed / 0 skipped / 86 real deselected**（168.53s），Console **864 passed**；契约同步、Token lint、typecheck、build 与 Compose 校验通过。独立复审确认两项来源绕过已关闭，另实测验证 DecimalException 类型及修复语义，未发现新的确认阻断；复审未独立跑测试。先前 2488/2489 项完整门禁分别始于最后来源修复/极端指数修复前，不作为本切片最终版本证据。

共享 wire codec 修复改变 `reader_artifact_digest`。扩大回归的 13 模块运行记录为 **557 passed / 17 failed**：17 项均因旧 Reader 校准批准身份与当前制品摘要不符被拒绝，不能计作通过；这正是 fail-closed 准入行为。需固定源码重跑 9 组各 10 个真实样本，生成新校准证据后再更新批准记录并跑完整真实门禁，不能手工替换批准哈希。该运行晚于六模块测试，包含两项极端指数 GREEN；独立 codec 原因码另有 1 passed。

## 未完成的门禁

尚需真实 Provider/API/固定模型制品与 Schema 能力准入、可靠 token 上界、跨 Worker 持久预算、注册执行器、Worker/recovery 接入、迟到 CAS、critical 联合来源及真实独立 holdout。现有传输测试不能替代这些验收；继续在 PR #181 完成 T8/T9 和 89 项完整标准。
