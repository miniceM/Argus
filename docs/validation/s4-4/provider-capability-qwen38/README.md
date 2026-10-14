# 本地固定模型的结构化输出与 tokenizer 探针

核验日期：2026-10-14；Argus 源码基线 `bfbe723`，oMLX 运行版本 `0.7.0`。
**这是 Provider 能力预检记录，不是 Judge 正式准入或独立留出集校准。**
两个不同请求各调用两次，共四次真实推理；重复调用不计独立用例。

## 身份与观测

目标 `Qwen3.8-27B-MLX-4bit` 的本地模型目录包含 13 个文件、16,081,498,220 字节，
逐文件完整 SHA-256 和有序清单见 `model-artifact.json`。清单规范 JSON 的摘要为
`sha256:0b75f2500abe6118648946792a95ee9accd57001479663f42409f7ec937fbbf3`。
文件哈希前后检查 size/mtime/inode 未变化，未使用目录名称替代制品摘要。
该观测未构建部署 attestation，也未证明远端长期使用同一制品。

请求固定 `temperature=0`、`top_p=1`、`max_tokens=512`、`seed=0`、
`enable_thinking=false` 和对应 `chat_template_kwargs`，
使用当前 Argus `judge_output_schema`，包括 Step ref 和受控 reason 枚举。
输入是人工构造的格式能力探针；没有业务轨迹、客户数据或人工业务标签。
仅请求本机回环服务，凭据经已有官方配置链在内存中使用，未进入任何归档。
本次为直接 HTTP 能力探针；当前生产 `OpenAICompatibleJudgeProvider` 尚未发送或冻结
上述 `seed` / thinking 选项。这两个探针的计数不能直接套用到当前 Adapter 的预算：
必须先完成 execution profile 与 Adapter 接线，再按正式请求复验。

| 请求 | 观察 | 本地 tokenizer / 服务输入 tokens | 输出 tokens | 首次 / 归档复测秒数 |
|---|---|---|---|---|
| basic | HTTP 200、固定 model、finish=stop、无 Warning、解析成功 | 85 / 85 | 70 | 19.658 / 2.715 |
| format_injection | 同上；要求 Markdown 的输入仍返回有效 JSON | 86 / 86 | 69 | 2.815 / 2.610 |

首次 basic 的 usage 包含模型加载时间 15.13 秒；这只证明该次请求发生自动加载，
不代表后续延迟或长期并发表现。没有调用管理端加载/卸载或修改配置。
模型给出的 PASS 只是格式探针产物，**不能作为业务质量证据**。

tokenizer 使用模型目录中的配置和模板，`local_files_only=true`、
`trust_remote_code=false`；库版本、关键实现文件摘要和提示词摘要见
`tokenizer-observation.json`。Transformer/tokenizer 的版本与模型源文件均是本次实际读取值。
同一个简单输入在 thinking=true/false 下的本地计数分别为 76/40，
说明模板选项会改变输入，不能把另一种调用格式的 token 数直接用作预算上界。

## 归档与完整性

- `basic.request.json`、`format_injection.request.json`：实际发送的 UTF-8 body。
- 对应 `*.response.json`：第二次调用收到的原始 HTTP response body，未重排或重新序列化。
- `wire-receipt.json`：上述原始字节摘要、HTTP/解析/usage 断言。
- `requests-and-local-counts.json`：完整请求及离线计数和渲染摘要。
- `initial-observations.json`：第一次调用的解析对象和当时观测到的 wire hash。
  **第一次未保存原始 response 字节，其 hash 不能通过对象重新序列化复核**；
  因此补做两次同 payload 的原始字节归档，不把第一次记录冒充完整 wire 制品。

`wire-receipt.json` 的 request/response digest 可按归档文件的原始字节复核。
本次未把自由 rationale 保存为生产 Typed Result；此处归档仅含受控合成探针的响应，
不是 Snapshot、Score 或真实客户数据。

## 正式准入尚缺的条件

实际 oMLX OpenAPI 只提供 Anthropic 格式 `/v1/messages/count_tokens`；
其实际源码有模板失败后字符串拼接的 fallback，不能直接作为当前 OpenAI 请求的可靠计数证明。
OpenAI `response_format` 在 grammar 不可用时，实际源码也允许提示词降级并返回 Warning；
本次两种请求均没有 Warning，但不能据此证明全部 Schema/输入均受支持。
Provider server 源文件摘要保存在回执中，未以 OpenAPI 声明替代上述实测。

后续仍须冻结实际 Provider execution profile、所有影响生成的选项与部署身份，
对实际每份请求执行获准模板/tokenizer 的有界计数检查，拒绝 fallback/漂移及超量输入，
并由正式 Registry/Launch/Worker/recovery 消费该证明。
还须完成全量 required cohort 预算准入、独立人标 holdout、
critical 同风险独立来源和联合校准。
本次没有更新 `release_eligible`，不计任何 S44 校准标准通过。
