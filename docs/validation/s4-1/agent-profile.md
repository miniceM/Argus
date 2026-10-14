# S4.1 AgentVersion 来源声明

核验日期：2026-10-13。对应 #5、#128；依赖 [017 存储基础](storage-schema.md)。

本批把可空顶层 `trajectory_source_profile_id` 贯穿 AgentVersion Spec、数据库注册/读取、DTO、YAML bootstrap 和 HTTP 创建/查询/列表/归档响应。复用轨迹 Identity 约束：1–64 个可打印 ASCII 字符，拒绝空白、控制字符、非 ASCII 和类型转换。

非空 profile 纳入 `spec_digest`，更换来源须创建新 AgentVersion。同版本重注册仍拒绝；YAML 同摘要重复导入跳过，改 profile 后拒绝覆盖。未声明或显式 NULL 使用原有摘要算法，metadata 中同名键不会成为来源声明。旧兼容 listing 在 NULL 时保持原字典形状；新 HTTP DTO 有可空字段，OpenAPI 和 Console 生成类型已同步。

声明本身不证明来源已获准或观测完整。获准 source_profile_set、选择算法、Manifest 1.3 冻结及采集激活由后续 T1 切片完成；本批不改变旧 Manifest 1.2 的执行、Snapshot 或门禁资格。客户端不能通过该字段获得 COMPLETE 结论。

验证：

- RED：缺失规范化字段、profile 不影响摘要、非法 ID 未拒绝，4 failed；GREEN：14 项新 profile 测试与17项已有 Registry 测试，31 passed。
- 旧输入摘要黄金值来自修改前代码；未声明/NULL/metadata 自报三种情况保持同值。覆盖严格类型、顺序稳定摘要、新版本/旧版本不可变、YAML 幂等及 HTTP 201/查询/列表/归档/409/422。
- 定向测试同时执行专用 PostgreSQL 回归，18 passed / 0 skipped；完整 `make validate` 在自有隔离 PostgreSQL 17 下通过：930 Python、847 Console，PostgreSQL 专项无跳过。31 项真实 Langfuse 测试仍由独立 CI 门禁执行。
- 契约无 drift，Token lint/typecheck/build/Compose 通过；测试容器及临时 schema 已清理。

复验命令：

```bash
python scripts/export_openapi.py
pnpm --dir services/console api:generate
python -m pytest -q tests/test_trajectory_agent_profile.py tests/test_agent_registry_contract.py tests/test_registry.py
make validate
```

部署需先应用 017。没有独立 Console 配置控件；本批可由现有注册 API 或 YAML 设置。回退至不理解该字段的应用版本前，应停止新增非空 profile 版本，保留已注册版本与证据，不自动改写旧 `spec_digest`。
