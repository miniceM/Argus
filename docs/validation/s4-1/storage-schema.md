# S4.1 最小证据数据库基础

核验日期：2026-10-13。对应 #5、#128；整体实施仍按 [T0–T9 计划](../../plans/2026-10-13-s4-trajectory/08-implementation-plan.md) 推进。

## 本批行为与边界

- 追加 `017_trajectory_evidence.sql`：AgentVersion 顶层可空 `trajectory_source_profile_id`、checkpoint 可空 `trajectory_acquisition_json`、最小 `trajectory_evidence_artifacts` 表。
- `payload_json` 用 `TEXT` 保存，局部 canonical encoder/Decimal decoder 精确往返；PostgreSQL 的 `octet_length` 约束限定 1 字节至 2 MiB。SQLite 迁移及 ORM 使用 `length(CAST(... AS BLOB))`，按 UTF-8 字节计数。
- 复合外键验证 Item 属于 Launch、Attempt 属于 Item；`RESTRICT` 阻止普通父记录删除级联抹去证据。新增两个目标唯一索引支持物理外键，artifact 索引支持 Launch/Attempt 查询。
- 唯一键为 Item、Attempt、contract digest、trajectory digest；不同证据或合同可保存新版本。artifact 不引用 checkpoint，checkpoint 到期清理不删除证据。
- 旧行扩展字段为 NULL，旧 digest、Manifest 1.2 与输出内容保持原值。本批只提供存储基础，后续 Spec/DTO/Manifest 接入负责 profile 冻结；T2 写入边界负责模型重新校验、脱敏、digest 验证和 insert-only。数据库表本身不验证完整轨迹 JSON，也不限制管理员 UPDATE/DELETE。

## 验证证据

- RED：缺少迁移和 artifact 表，迁移与 ORM 两条路径分别失败。
- SQLite：迁移和 ORM 两种建表路径覆盖精确数值、字节边界、缺失/跨作用域身份、唯一键、新证据版本、父记录删除限制和 TTL 分离。SQLite 与 PostgreSQL 均先写入 016 的历史 AgentVersion、Launch、Item、Attempt、checkpoint，再升级至 017，验证旧输出、Manifest、摘要及执行代次保持原值，新字段为 NULL；幂等重跑及 checksum 验证通过。
- 真实 PostgreSQL：自有隔离 PostgreSQL 17，随机 loopback 端口与仅存在于进程内的临时凭据；同一组证据约束与现有 final-attempt 外键定向测试 33 passed，0 skipped。测试用独立 schema，结束后清理；未连接本机已有数据库。
- 完整 `make validate`：916 Python、847 Console 测试通过，PostgreSQL 专项无跳过；31 项真实 Langfuse 测试由既有独立 CI 门禁执行。本批未改变采集或业务 Agent 代码。
- OpenAPI/生成类型无 drift，Token lint、typecheck、build 与 Compose 静态校验通过。

复验命令（`TEST_POSTGRES_URL` 须指向专用测试库，既有测试包含 TRUNCATE）：

```bash
python -m pytest -q tests/test_trajectory_storage_schema.py tests/test_migration_and_schema.py
make validate
```

## 部署与回退

生产迁移由既有 MigrationRunner 顺序执行，不改已发布迁移。建索引及 DDL 会持有数据库锁，部署应沿用已有迁移窗口。旧应用可继续使用新增的可空列和空表；代码回退应保留 017，不自动删除证据或迁移记录。启用后续写入前，须完成 insert-only、脱敏和冻结身份校验的独立验收。
