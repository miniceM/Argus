# S4.1 外部 JSON 与批次配置边界

本增量基于全部前置实施 PR 已合入的 main@4c33a251ca72ac1d53768ed757e826539b11717e，关联 #128；尚未激活生产采集、完成资格或冻结证据。

外部来源的 input/output 先校验为精确 JSON 值，再进入 canonical codec。SDK/Pydantic 对象及嵌套对象被标记 INVALID，不能执行其序列化方法或把对象转换成“完整已采集参数”。别名比较同样先校验 JSON 值；仅时间字段允许精确 datetime，保持原有 UTC 时间语义。受控领域模型仍可由现有 codec 序列化。

时间别名经类型化校验并按 UTC 时刻比较；有效时刻冲突仍拒绝该行，无效或无法转换的时间仅置空并降低 ordering，显式 duration 与工具身份不变。HTTP Root 的非法输入保留诊断，其工具能力仍为 NONE。

`ObservationNormalizer(contract)` 在创建时重新校验完整合同和 AgentVersion 已绑定的来源，独立持有映射、来源及脱敏策略快照。采集调用方可复用其 `normalize(row, scope, index=...)`，每行继续校验 trace、parent、语义、JSON 与 index 限额。不保留原始行，不证明 scope 闭包或采集完整度；来源配置变更必须创建新实例。现有 `normalize_observation` 保持签名，仍在每次调用时重新校验调用方配置。

这里只提供批次内部复用原语。完整 `normalize_observations`、Assembler、累计字节/分页/总时限、结构父节点资格以及 Acquisition 仍由后续增量交付；不能用 index 上限代替整体采集预算，也未将性能测试换算为生产 SLA。

## 验收与证据

- RED：SDK 序列化五项回归实际失败；复用配置实例尚未实现，九项边界测试实际失败。
- GREEN：相关 Normalizer/redaction/contract/schema 共 229 项通过。包括 1000 行仅一次完整合同校验、配置与结果隔离、未绑定/非法配置拒绝、trace/index 边界、SDK 序列化不被调用、精确 datetime 别名、无效/极端时间的字段降级及 Root 工具能力隔离。独立审查两项 Required 已补回归并修复；新时间断言按冻结 profile 保持 timing=PARTIAL，不把有效 duration 错当来源完整资格。
- 命令：`python -m pytest -q tests/test_trajectory_normalizer_context.py tests/test_trajectory_normalizer.py tests/test_trajectory_redaction.py tests/test_trajectory_contract.py tests/test_trajectory_schema.py`。
- 审查修复后最终完整 `make validate`：1126 项 Python 测试、847 项 Console 测试通过，PostgreSQL 专项零跳过；Token lint、类型检查、构建、OpenAPI/生成类型与 Compose 校验通过。隔离 PostgreSQL 定向 82 项通过、零跳过，测试容器已按所有权清理。原有真实 Langfuse 门禁保持独立执行，Mock 和单元结果不计为新增真实链路资格。
