# S4.1 冻结范围的批量输入

关联 #128，依赖 PR #163。本增量提供 Assembler 所需的有界 `NormalizedObservations`；图闭包、去重、因果、terminal inventory、采集收敛与制品冻结仍由后续组件实现。

## 接口修订依据

原蓝图的 `normalize_observations(rows, contract)` 没有 scope；现有 `ObservationNormalizer.normalize` 必须用冻结 scope 检查 Trace 并识别 HTTP Root。真实两框架与 HTTP-only 门禁已证明这两个身份不能由普通字段猜测，因此批量入口增加 keyword-only `scope: TrajectoryScope`，复用调用方的同一冻结身份，不增加 trace/root/attempt 等旁路参数。旧单行接口保持不变。

每批只校验并独立持有一份完整合同和选中来源，返回 scope、合同摘要与 profile ID/版本。每行经过 JSON-wire 类型/容量检查、字段映射和脱敏；拒绝行也保留能确认的 ID/Trace/parent 线索，缺失或冲突 alias 不挑选赢家。所有行诊断留在各自 record 中，不能在尚未判断父链归属时用兄弟树错误污染当前 Attempt。无原始 JSON、SDK 对象或敏感原文进入输出。

## 限额与后续消费

- 只接受 list/tuple JSON-wire 行；不消费任意迭代器，不调用外部对象的序列化方法。
- 整行使用 wire codec 的类型、深度、字符串和数字硬限额；合同中更低的字段/投影预算由单行归一化处理。可选 duration 超预算或未使用 metadata 超过投影预算，不抹除独立确认的工具身份。
- 输入行数不超过冻结 `page_size * max_pages`；累计成功编码的 canonical UTF-8 字节不超过 `max_source_bytes`。超限保留已处理记录并显式 `truncated`。
- `source_bytes_complete=false` 表示有不可编码或未处理来源，计数仅是下界；不是 Reader 的原始响应字节数，不能据此证明采集完成。
- Step 临时 index 固定为 0；重复行与拒绝行保留。Assembler 先按指定 Root 去重和验证真实闭包，再应用 `max_steps` 并分配稳定展示 index。
- 重建批次再次验证 Step 与安全身份/parent/Trace scope 一致。后续消费者还须核对传入 scope、完整合同和选中来源，不仅比对 Trace ID。

## 验证

核验日期：2026-10-13。

RED：首轮 15 项因批量入口缺失失败；重建批次的 foreign Trace Step 一项单独回归失败；主代理追加的低字段预算三项回归证明整行预检查会误丢工具身份。最小修复后相关 Normalizer/结构角色/Schema/合同/脱敏/隔离测试 286 项通过，ruff 通过。

隔离 PostgreSQL 定向测试 279 项通过、零跳过；完整 `make validate` 的八步通过，1255 项 Python、847 项 Console 测试通过，OpenAPI/TypeScript 快照无漂移。完整门禁单独排除 40 项真实来源测试，再以真实门禁执行：40 项通过、零跳过，耗时 96.62 秒；其中批量入口覆盖原生 OTel 三次、Pydantic AI 三次和 HTTP-only 十次。真实采集父链、脱敏结果、合同摘要与 canonical 来源计数均保留，不能把这个证明扩展为 Assembler 完整性证明。

单次独立只读审查无 Critical/Required；已同步类型签名与 list/tuple 边界。原有弃用警告不影响断言结果，未通过跳过或放宽断言获得绿色。

复验命令：

```bash
python -m pytest -q tests/test_trajectory_normalized_batches.py tests/test_trajectory_normalizer.py tests/test_trajectory_normalizer_context.py tests/test_trajectory_structural_roles.py tests/test_trajectory_contract.py tests/test_trajectory_schema.py tests/test_trajectory_redaction.py tests/test_trajectory_fixture_isolation.py
make trajectory-test
make validate
```
