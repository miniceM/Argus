# S4.1 结构来源与真实归一化

关联 #128，依赖 PR #160。本增量解决框架父节点因缺少业务 phase/status 被丢弃的问题，保持结构关系与调用证据的边界；尚未交付 Assembler、采集收敛、Manifest 1.3 或冻结制品。

## 来源与协议

2026-10-13 在独立本地 Langfuse 4.38.0、Runner SDK 4.16.0、原生 OTel 1.45.1、Pydantic AI 2.55.0 上重新探测。两个真实 Worker 调用通过，实际 Pydantic AI 父链含 `agent run`、`chat function:plan:` 和工具 SDK 包装 Span，不能仅凭物理 TOOL 类型视作业务执行。

新增可选 `SourceProfile.structural_roles`，按 type/name 与 SDK scope/version、service/version 精确匹配；结构候选使用 AGENT/LLM/CUSTOM、EVENT/UNKNOWN、安全展示名及真实父关系，全部六项能力 NONE。不存原始内容，不构造调用或因果证据。业务来源匹配或业务语义字段存在的行不得借此绕过缺失/非法语义检查。空角色声明不进入历史合同序列化；旧测试合同黄金摘要仍为 `sha256:3ee4cf5a05e650ab855fea213528dffbdfb259405a1ca89452a7f9f8233208b2`。

`observed_banking_contract_payload` 是真实门禁使用的版本化示例配置；从数据库的不可变 AgentVersion 声明选择 profile，三个示例分支共享同一完整合同。它不是生产默认来源资格；第三方或 HUMAN 权威来源不得凭相同名称取得资格。Deadline/quiet window 仍是待负载与收敛测试校准的起始值，没有转为 SLA。

## 已验证与待验证

- RED：14 项因结构合同未实现失败；业务来源缺语义回退结构的单独回归实际失败；独立审查发现语义检测映射可被省略，四项回归实际失败。
- GREEN：结构角色、合同、Normalizer、Schema、脱敏与环境隔离共 264 项通过。覆盖精确来源匹配、版本拒绝、语义标记拒绝、TOOL/HUMAN 角色禁止、内容不泄漏、配置独立持有与旧摘要兼容。启用结构角色必须冻结全部九项语义检测路径，缺项在合同构造阶段拒绝。独立整切片审查仅执行一次，Required 已补回归并由主代理验证修复。
- 生产 Reader→Normalizer 真实交叉验证 16 项通过、零跳过：两框架各三次串行调用，HTTP-only 十次。原生 Root 闭包 7 Step（1 结构、4 业务工具），Pydantic AI 13 Step（7 结构、4 业务工具）；结构节点无工具能力，HTTP-only 仅 Root，最终答案评测保持通过。
- 三分支共享合同摘要；实际游标续页、显式 duration 的 Decimal 输入、全部 scope 内父引用可解析。这里只验证受控候选保留了真实父链，不能据此声称生产 Assembler 或 OBSERVED_COMPLETE 已实现。
- 全部真实门禁 40 项通过、零跳过（104.48 秒）；业务报告 39 份（native 18、Pydantic AI 11、HTTP-only 10），SDK/API 两项独立通过。证据摘要见 [source-profile-matrix.json](source-profile-matrix.json)，完整版本/ID/字段矩阵与 JUnit 保存在本地执行制品中；远端 CI 验收另以对应 head 的实际运行制品为准。
- 审查修复后，隔离 PostgreSQL 定向 257 项通过、零跳过；同步最新前置分支后完整 `make validate` 1233 项 Python、847 项 Console 测试通过，PostgreSQL 零跳过，契约同步/Token lint/构建/Compose 校验通过。真实门禁在本地独立执行，普通套件中的 40 项 deselected 不计为通过。

复验命令：

```bash
python -m pytest -q tests/test_trajectory_structural_roles.py tests/test_trajectory_contract.py tests/test_trajectory_normalizer.py tests/test_trajectory_normalizer_context.py tests/test_trajectory_schema.py tests/test_trajectory_redaction.py tests/test_trajectory_fixture_isolation.py
make trajectory-test
make validate
```
