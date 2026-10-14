# 保留已开发的 Provider seed 与模板选项

核验日期：2026-10-14，基线 `0d83d59`。关联 #131。
用户已将 #131 剩余工作交给维护者，本切片保留交接前已经开发和测试的成果，
不继续扩大为正式 Token/Provider 准入、Judge Worker/recovery 或人工校准。

`FrozenJudgeDefinition.provider_options` 是有限的冻结参数：

- `openai-compatible-v1` 只发送明确 seed，不接受 thinking 选项。
- `omlx-0.7-v1` 固定 API `0.7.0`，明确发送 seed、`enable_thinking` 及对应
  `chat_template_kwargs`；布尔/字符串 seed、超界值、未知键和错误 profile 被拒绝。
- 选项变化影响 Definition digest，进而影响 Binding；没有不受约束的 extras。
  未声明选项保留原 transport 行为，不能以此宣称正式参数准入。

`encode_judge_request` 验证实际准备入口 seal 并构造精确请求 body，
与 HTTP Adapter 共用同一编码入口；Decimal 不经 float 往返。
Provider 响应携带 Warning 时返回受控
`JUDGE_PROVIDER_CAPABILITY_UNAVAILABLE`，不保存 Warning 原文、不造分数。
这只是拒绝降级的保护，不代表成功响应证明了完整部署、Schema 或 tokenizer 准入。

验证：

- 新需求 RED：**16 failed / 84 deselected**（1.07 秒）。
- 五模块 GREEN：**230 passed / 0 skipped**（26.18 秒），使用实际隔离 PostgreSQL，
  覆盖定义、HTTP Adapter、执行桥接、跨 Worker 预算和 QualityPolicy。
- 完整 `make validate`：**PASS**（退出码 0）；Python **2658 passed / 0 skipped / 86 deselected**（189.56 秒），Console **864 passed**，OpenAPI 与生成类型同步、Docker Compose 静态校验通过。86 项真实集成按独立入口另验。
- 独立只读审阅未发现确认阻断；请求编码只证明 HTTP 路径共用，Token 计数尚未接入，不将其计为准入通过。

```bash
pytest -q tests/test_trajectory_judge_definition.py tests/test_trajectory_judge_provider.py \
  tests/test_trajectory_judge_execution.py tests/test_trajectory_judge_budget.py \
  tests/test_trajectory_judge_quality_policy.py
make validate
```

该参数合同与先前直接 HTTP 能力探针的选项相符，但未重跑实际 Adapter 的模型校准。
原始探针证据保留 `bfbe723` 身份，不重标为本切片实测。
完整交接边界见[维护者交接](maintainer-handoff.md)。
