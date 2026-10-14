# T8 冻结 Judge 定义与确定性触发

日期：2026-10-14。关联 #131；依据已批准 S4.4 §3/§5。依赖[输出与校准统计](contract-progress.md)，仍不表示真实 Judge 已完成。

## 实现与解释

`FrozenJudgeDefinition` 严格限定四类 Judge。Provider/API/Resolved Model、模型制品摘要、Prompt/Rubric 原文与 SHA-256、实现/依赖制品、解码和输入/Launch 预算、触发/覆盖、校准集/策略与有效输出 Schema 全部进入 canonical definition_digest。未知字段、无效 kind/risk、常见未解析模型标记、错误文本摘要、非有限/非法参数及多次重试均拒绝。max_attempts 首版必须是整数 1，bool 不作为整数。

模型制品摘要是对本地目录名不能单独证明不可变身份的显式补充；实际 Provider 必须证明模型解析、字节制品和 API 能力与冻结记录一致。这里的类型/摘要验证只声明合同，不能代替 Provider 准入，也不声称词法检查能识别全部别名。不得以 Schema 通过或任意填写 SHA 获得正式执行资格。

响应 Schema 显式要求 JSON number 置信度，避免 Pydantic Decimal 的通用 number/string Schema 暗示允许字符串。风险 reason enum、额外字段禁止与引用上限从冻结输出策略生成；本地仍会独立验证响应。

诊断触发使用有限的 `DETERMINISTIC_FAIL_OR_UNKNOWN` 策略，并允许冻结阈值下的确定性低置信触发；空目标或全部高置信 PASS 为零调用。Judge 输出 min_confidence 与确定性触发 threshold 分开配置。Case、seed、definition_digest 使用有界 canonical JSON 构造抽样身份，避免直接字符串拼接歧义。SHA 区间与 sample_rate 通过整数比精确比较，不依赖 Worker 的 Decimal context。该结构化编码明确实现批准方案的 `seed || case || definition` 身份组合，不引入在线随机种子。

release 分支不使用诊断触发或 sample_rate。启动前必须验证冻结 Dataset Case 的完整、无重复 ID 集及 digest，只有 ALL_DATASET_CASES；缺/重复 Case 或不同 cohort 拒绝。实际 required Binding 注册、Launch 最坏预算预检及 Worker 覆盖验证仍须接入，不能仅调用触发函数就声称正式全量评测完成。

## 验证

初始新测试 RED 34 failed（冻结定义/触发入口缺失），实现阶段又实测发现输出 Schema 内 float 与 Decimal codec 不匹配，修复为有界 JSON 解码的有效 Schema。输出 Schema 本地断言验证 numeric strict 与 rubric reason enum。

确定性低置信 API 得到 RED 6 failed，再接入有限、对齐、严格 Decimal 置信值校验。精度漂移回归实际 RED 1 failed：Worker 的 Decimal precision=2 改变 Case 抽样；整数比修复后结果与原精度完全一致。目标测试最终 86 passed（新 42、既有 44），ruff、git diff --check 通过。独立只读审阅未发现本切片阻断问题；未审未实施的 Provider/Launch 准入。

最终源码 `make validate` 8 步通过：隔离 PostgreSQL 的 Python 2420 passed / 0 skipped / 86 real deselected（159.49s），Console 864 passed；OpenAPI/schema 同步、Token lint、typecheck、build 与 Compose 校验通过。真实 Judge 推理、完整真实集成及最终发布 CI 仍为独立必需门禁，不能用本记录代替。

```bash
python -m pytest -q tests/test_trajectory_judge_definition.py tests/test_trajectory_judge.py
make validate
```

## 下一步

安全请求与 pre-adjudicated criteria、获准异步 Provider/模型解析、跨 Worker 持久预算、Worker/recovery 执行器、critical 联合来源、真实独立 holdout 及 89 项整体验收仍未完成。当前新增函数尚未成为正式 Worker 的调用链，不改变历史 builtin 或已有质量结论。
