# S4.1 显式事件因果与调用配对

关联 #128，依赖 PR #166；核验日期 2026-10-13。

`causal.validate_causal_links(scope, observations, contract)` 是最终 Assembler 的内部阶段：先验证并构建冻结 Root 图，再返回 `CausalObservationGraph`，包含独立拥有的 Root 图、已验证 observation/step 前驱映射、request/action 配对、逐 Step 原因码及顺序证据失效范围。它不发布 StandardTrajectory、不核算 terminal inventory 覆盖，不作完整度或质量结论。

## 因果证明

- 仅接受同一冻结 Root/workflow 内、经 profile 字段映射确认的显式 event 引用，解析到唯一当前 observation ID。缺少单行 ordering coverage、来源 NONE、未知 event/workflow/sequence、未准入 HUMAN 不作证明；PARTIAL 来源可保留已独立验证的边，不被升级。
- 同 workflow 内 event ID 或 sequence 重复不挑赢家；身份唯一性与边资格分离，即使单行 ordering 不可用，已确认的 event/sequence/call 身份仍参与冲突检查。跨 workflow 的相同序号彼此独立。terminal 的 inventory 声明不要求工具前驱；有效字段或稳定 `TERMINAL_INVENTORY_INVALID` 诊断均保留其声明身份，仍参与命名空间检查，但不能退回普通顺序节点。
- 在时间/序号筛选前，对原始已解析关系进行迭代强连通分量校验，完整环与自环均剔除相关边。不能在矛盾环中选择一条“序号看起来正确”的半边；独立无环证据保留。
- 每条可信关系同时要求前驱 sequence 小于当前 sequence、两端时间明确且各自区间合法、前驱结束不晚于当前开始。缺时钟或冲突只降低相关顺序证据，不修改时间，不补零，不拉入闭包外节点。
- parent、display index、时间先后和全局单调 sequence 本身均不生成因果边。并行请求没有被构造全序；返回映射使用已观测 ID，可回链来源。

## 调用配对与参数状态

只有同 workflow/call 的唯一 REQUESTED 与唯一 EXECUTED/BLOCKED、同一已映射逻辑工具、且存在已验证的 request→action 关系，才返回 `request_by_execution_id`。调用 ID、工具或数量不确定时不恢复配对；blocked 的原始 phase 保持 BLOCKED。此阶段不传递参数，后续投影须保留来源的 AVAILABLE/MISSING/REDACTED/未采集/INVALID 差异，不能用配对抬高证据覆盖。

真实夹具隐藏参数时省略整个 input，并提供 REDACTED 和 hidden_paths；原投影误丢已知隐藏状态，现仅保留明确覆盖 policy path 的 REDACTED。未覆盖路径仍未采集，不造 MISSING。显式 UNCOLLECTED 不再被当作非法参数或可用参数，即使旁边存在值也不采信。该修复有独立 RED 回归；公开 DTO/枚举不变，无原文恢复或新字段。正式制品/Binding 尚未启用，未来准入工厂须冻结相应算法版本，历史制品不回写。

## 验证

初始 RED 为缺失 causal 模块；缺单行能力、terminal 顺序角色与命名空间冲突、REDACTED 无 payload、UNCOLLECTED marker 分别实测失败。单次独立审查提出两项 Required：身份唯一性不能随边资格一起筛除，无效 inventory 的终结声明不能变成普通顺序节点。主代理同期及审查补充四项实际 RED，最小修复后 34 项因果测试、5 项新增投影测试和既有轨迹测试共 355 项通过；ruff 通过。含 1000 节点迭代环校验、重复/矛盾、并行、跨 workflow、partial 来源和 privacy 边界。没有二次审查，修复由 RED→GREEN 及最终全量门禁证明。

真实 helper 已切生产 Reader→Normalizer→Root 图→因果，并扩展两框架执行状态/参数状态/并行场景；冻结 Registry 来源声明，再核对调用配对与正向边，反转真实 API 到达顺序后逐字节一致。最终 40 项真实集成测试通过，JUnit 为 0 failure / 0 error / 0 skip，耗时 98.34 秒。其中原生 OTel、Pydantic AI、HTTP-only 各 10 条生产消费链：两框架串行/状态/参数场景均为 3 条因果边和 2 对调用，并行为 2 条边和 2 对调用；HTTP-only 为 0 条边和 0 对调用。HTTP-only 不因存在 Root 而获得工具证据。

最终自有隔离 PostgreSQL 定向 348 项通过，JUnit 为 0 failure / 0 error / 0 skip；按 ownership 清理测试容器。`make validate` 八步全部通过：1324 项 Python 测试、847 项 Console 测试、契约快照同步、Token lint、构建及 Compose 静态校验。40 项真实门禁由上述独立运行覆盖，未计入常规 Python 数字。现有依赖废弃告警保留，没有修改断言或跳过用例。

本增量仅交付内部因果阶段及参数来源状态修复。T1 尚需最终轨迹组装、terminal inventory 覆盖、分页/静默收敛证明、能力聚合与制品预算/摘要；整套 S4 未完成，issue 保持开放。CI / Cloud 凭据修复继续由 PR #162 所在任务负责。

```bash
python -m pytest -q tests/test_trajectory_causal_links.py tests/test_trajectory_redaction.py
make trajectory-test
make validate
```
