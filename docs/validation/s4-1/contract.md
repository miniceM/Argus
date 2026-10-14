# S4.1 测量配置合同

核验日期：2026-10-13。对应 #5、#128；依赖 [AgentVersion 来源声明](agent-profile.md)。

本批提供 `TrajectoryContract`、`SourceProfile`、`ToolMapping`、`RedactionPolicy` 和 `TrajectoryLimits` 的严格配置模型。完整来源集合、映射、脱敏路径、必需能力、算法版本和预算纳入 canonical 摘要；字段顺序、集合顺序和重复 allowlist 路径不改变摘要。来源由不可变 AgentVersion 的顶层声明选择，缺失或未获准 ID 拒绝绑定；本次选中的分支是执行绑定，不进入测量摘要，因此两个框架可共享同一测量口径。

SourceProfile 路径是静态字典键数组，点分 metadata 键按字面读取；不执行表达式或访问对象属性。阶段与状态不能复用类型或原始名称路径。工作流清单协议须提供 scope、事件 ID、sequence 和终态 count/IDs/final sequence 的映射；这些映射仅说明输入在哪里，实际闭包和完整度由后续 Assembler/Acquisition 验证。自报 HUMAN 类型拒绝，human 能力声明需提供 authority ID，声明仍不构成独立权威证明。

来源别名、metadata predicate 键和脱敏路径统一拒绝凭据字段及 headers 容器，包括 snake_case/CamelCase 变体。脱敏仅允许静态非根路径；后续运行时 redactor 还须验证被选值及嵌套对象，字段名防护不能替代实际脱敏。合同模型不是已获准 profile 工厂；测试采用合成配置，不宣称真实来源资格。

起始 `max_pages=20`、`deadline_seconds=60` 是待校准默认值。本版有限硬上界分别为 100 页、300 秒，单请求仍最多 5 秒、并发最多 4；校准改配置必须产生新合同摘要。来源 8 MiB、制品 2 MiB、1000 Steps、100 条/页及 JSON 深度/字符串/数字限制沿用方案。可调的路径段数实际约束来源路径和 redaction JSONPath；合同自身 canonical 总量最多 2 MiB，独立于采集制品的可调预算。实际限流、截止和 JSON 内容检查由后续采集消费者执行。

`frozen` 不深冻结容器。绑定时复制并重新验证配置，读取选中 profile 返回新副本，摘要计算重新校验当前内容。持久化消费者仍须在写入边界重新验证并核对摘要。本批不接入 Manifest 1.3、Worker、证据写入、Snapshot 资格、Console 或门禁，旧 Manifest 行为不变。

验证：

- RED：模块缺失；随后凭据变体、predicate 侧门、语义别名冲突、较小路径预算、终态字段缺失及合同容量新增 23 项回归全部失败。
- GREEN：66 项合同测试与 85 项 schema 测试，151 passed。覆盖双框架摘要一致、配置变化身份变化、未知选择拒绝、乱序稳定、复制隔离及可变容器重新校验。
- 独立只读审查的三项 Required 已修复，两项 Optional（协议字段与合同总量）也已补回归。图谱无新文件索引，以当前源码与实测为准。
- 自有隔离 PostgreSQL 17 下定向 70 passed / 0 skipped；完整 `make validate` 通过：996 Python、847 Console、0 PostgreSQL 跳过，OpenAPI/生成类型无 drift，Token lint/typecheck/build/Compose 通过。31 项真实 Langfuse 测试由独立门禁执行；本批没有采集运行态变化。测试容器已按精确 ownership 清理。

复验命令：

```bash
python -m pytest -q tests/test_trajectory_contract.py tests/test_trajectory_schema.py
make validate
```

这是 T1 配置基础增量。真实 profile 集合仍需完成重复采样、预算校准及 qualified factory；字段 Normalizer、Root 闭包、因果与能力降级、完整度状态机及恢复接入继续按实施计划推进。
