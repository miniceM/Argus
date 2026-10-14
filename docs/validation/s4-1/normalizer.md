# S4.1 字段归一化与标量脱敏

核验日期：2026-10-13。对应 #5、#128；依赖 [测量合同](contract.md)。

本批按已绑定 SourceProfile 的静态字典键路径归一化单条 Observation，生成 `NormalizedObservation` 候选。ID、trace 和显式 parent 必须合法；冲突别名不选赢家，错误 trace、缺 parent、未知必填语义或不满足来源 predicate 均拒绝形成 Step。普通 TOOL 类型不能补造执行阶段或成功状态。预期 Runner HTTP Root 使用方案规定的 CUSTOM/EXECUTED，status 为 UNKNOWN，不能证明业务工具覆盖。

工具身份仅由获准 profile ID/version 和物理工具名的映射决定。获准名称保留，其余自由文本使用固定 `unapproved-name`，并记录安全 warning；不保存或哈希敏感原名。UTC 时间由明确带时区字段转换，错误时间为 null；显式 duration 精确解析为 Decimal，缺失、float、字符串和非法数字均为 null，禁止按时间差补造。时长问题只降低 timing，不删除有效工具身份。

统一 `project_json` 在归一化前解析精确 JSON，拒绝重复键、非有限值、原生 float、超预算深度/字符串/数字。只投影获准标量路径，容器整选为 INVALID，避免嵌套 token/headers 随获准对象泄漏；不会把源对象直接交给证据。整个参数未采集或 API 字段为 NULL 时 visibility 为空且能力降级；已采集的 JSON 文本 `null` 是 INVALID 形状。完整对象中确实缺键才是 MISSING，已隐藏为 REDACTED，非法输入为 INVALID。隐藏祖先优先于子路径可见值。参数与结果独立投影，HTTP Root 使用 input/output 策略，工具使用 argument/result 策略。

数组路径只为实际存在的获准值投影，索引达到 1000 时 INVALID，不按任意路径整数分配内存；空洞为占位 NULL，后续消费者必须先检查对应 JSONPath 的 visibility，不能将未获准占位作为真值。该界限属于本版投影算法；升级须改变冻结实现版本并校准。小于硬上限的合同预算实际应用于 JSON、显式 duration、名称与工作流引用。

workflow/event/call 引用仅保留 32 位十六进制 opaque ID，sequence 和终态清单有类型及数量上限。显式前驱暂存为 event 引用，Step 的 causal_predecessor_ids 保持空；不从 parent、index、时间或全局 sequence 生成因果。终态安全清单可进入 result_summary，但它仍是待核对声明，不能凭单条事件证明闭包或完整度。

本批不提供 qualified factory，不从 Trace 自报覆盖 AgentVersion 来源，不认证 HUMAN，不接入 Worker、Manifest、证据存储或门禁。Assembler 后续负责父链闭包、兄弟/旧 Attempt 隔离、事件引用及请求/执行参数关联；Acquisition 负责分页和收敛。不能把单条候选的字段能力当作完整轨迹资格。

验证：

- RED：两个新模块缺失；之后 Root 策略误用、较小 duration 预算及 SDK 对象比较新增 3 项失败；非法 visibility 容器和较小字符串预算再复现 3 项失败。
- 独立只读审查两项 Required：非法 visibility 容器已由主代理新增 RED 后修复；文本 null 形状再补一项 RED 并修复。Optional 输出错误使用稳定 `OUTPUT_SUMMARY_UNAVAILABLE` warning 标明，保留有效工具身份，不持久化原始错误内容。
- 真实执行 span 缺 input/visibility 时应保留未采集状态，补一项 RED 后修复；GREEN：53 项新增测试，与合同/schema 共 204 passed。自有隔离 PostgreSQL 17 定向 57 passed / 0 skipped；最终 `make validate` 通过：1049 Python、847 Console、0 PostgreSQL 跳过，契约无 drift、Token lint/typecheck/build/Compose 通过。31 项真实 Langfuse 测试由独立门禁执行；本批尚未将归一化接入真实采集。测试容器已清理。
- 图谱未覆盖新文件，以当前源码与实测验证，无索引重建。

复验命令：

```bash
python -m pytest -q tests/test_trajectory_normalizer.py tests/test_trajectory_redaction.py tests/test_trajectory_contract.py tests/test_trajectory_schema.py
make validate
```
