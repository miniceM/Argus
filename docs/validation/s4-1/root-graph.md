# S4.1 冻结 Root 图处理

关联 #128，依赖 PR #164；核验日期 2026-10-13。

`graph.assemble_root_graph(scope, observations, contract)` 是 Assembler 的内部拓扑阶段，返回 `RootObservationGraph`。它验证批次的完整 scope、完整合同摘要、绑定 profile ID/版本与分页/来源字节限额，并独立持有校验后的记录。后续 `assemble_trajectory` 消费这一阶段，再处理因果、terminal inventory、完整度、制品预算和摘要；本图不发布 `StandardTrajectory`，不声称采集收敛或 `OBSERVED_COMPLETE`。

## 归属与异常

- Root 必须是冻结 ID 对应的唯一有效观测。同 Trace 中指定 Root 之外的兄弟 Item、旧 Attempt 子树不进入图；Root 的物理上级是闭包之外的边界锚点。
- 只有无争议的真实 parent 链能归属当前 Root。拒绝行仍保留安全身份；同 ID 内容冲突不选赢家。冲突行的 Trace/parent 一致时，只保留其归属线索，独立可信后代仍可保留；父关系冲突不能任取一条边。
- 同 ID 的不同 Trace 也构成冲突；foreign Trace 的 parent ID 不能把它拉入当前范围。没有可证明归属的缺父、未知 Trace/parent、孤立节点、父环产生稳定原因码，不被解释为已知在范围外。
- 确定在兄弟树的拒绝行诊断隔离。归属当前闭包的拒绝行原因保留；异常组件不会删除其它独立工具证据。
- Root 缺失/被拒绝、Root ID 冲突、Root 自身父环分别明确抛出 `TRAJECTORY_ROOT_UNAVAILABLE` / `TRAJECTORY_ROOT_CONFLICT` / `TRAJECTORY_ROOT_PARENT_CYCLE`，不补造 Root 或输出假图。后续 Acquisition 必须按冻结截止/错误策略处理这些原因，不能将其当完成。

## 确定性与容量

去重比较脱敏候选、业务标记和安全身份的 canonical 内容；临时 index 和原因码数组的排列不产生冲突。无争议重复仅增加操作指标 `deduped`，不改变图语义。parent 深度用有界迭代父链计算，不依赖递归栈。Step 按 UTC 时间、显式 null 排序位、parent 深度、ID 稳定排序；parent/time/index 不生成因果边。子节点时间超出父区间记 `GRAPH_CLOCK_SKEW`，不修改时间。

`max_steps` 在去重和 Root 闭包之后应用，先保留 Root，再按稳定展示顺序选取其他候选；超限行只保留安全身份和 `GRAPH_STEP_LIMIT`，不保留其业务事件证据。批次的截断、来源计数不完整与来源预算原因继续显式传递。展示边、能力、完整度和 terminal inventory 仍须由最终 Assembler 校验，不能直接把内部图的 parent 深度当作业务因果或完整性。

## 验证

初始 RED 为缺失 `graph` 模块；未知 Trace/不可证明 parent、所有 parent 变体均在兄弟树的两项边界回归各有一次真实 RED；非法和全零 Trace 不能当作已知外部 Trace 的两项回归另有真实 RED。最小修复后 30 项图测试与现有契约/归一化/脱敏/隔离测试共 316 项通过，ruff 通过。包含 1000 层深链、重复/冲突、Root 保留、到达顺序、完整 scope 与配置预算重新验证。

最终自有隔离 PostgreSQL 定向测试 309 项通过、零跳过，容器按 ownership 清理。完整 `make validate` 八步通过：1285 项 Python、847 项 Console，零 PG 跳过，OpenAPI/TypeScript 快照无漂移；真实来源测试单独选择。

真实门禁 40 项通过、零跳过，耗时 102.55 秒；helper 使用生产 Reader→Normalizer→Root 图，检查原生 OTel 三次、Pydantic AI 三次、HTTP-only 十次的实际闭包、深度和反转到达顺序后的逐字节一致性。JUnit 核对零失败、零错误、零跳过；不将静态/Mock 测试计为真实来源证明。

实测图节点数/最大 parent 深度：原生 OTel 为 7/2，Pydantic AI 为 13/4，HTTP-only 为 1/0；中间框架父节点得到保留，HTTP-only 不补造业务工具节点。这些深度描述结构，不证明执行顺序。

单次独立只读审查无 Critical/Required。两项 Optional 延期：父节点 Trace 非法时，子节点仍安全地保留 UNKNOWN，但原因码可细分；可直接补充“Trace 未知、parent 已证在外部树”的交界回归。当前外部树与未知 Trace 已分别验证，未把延期项计为新增测试证据。完整 Assembler/Acquisition 不在本切片审查或验收范围内。

```bash
python -m pytest -q tests/test_trajectory_root_graph.py tests/test_trajectory_normalized_batches.py
make trajectory-test
make validate
```
