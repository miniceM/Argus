# [S1][Platform MVP] 建立 Agent Registry 与版本化评测领域模型 实施总结

## 1. 概述与交付内容

根据 [Issue #2](https://github.com/miniceM/Argus/issues/2) 及其方案评审与企业规范要求，已完成平台核心领域模型、持久化数据库迁移、零路径变量 REST API、四维不可变快照、原子执行锁与重试收敛的完整实施：

### 核心交付物清单
1. **数据库 DDL 与版本化迁移**：
   - `migrations/001_initial_schema.sql`：规范定义 `schema_migrations`, `agents`, `agent_versions`, `experiment_launches`, `experiment_item_executions`, `execution_attempts` 的建表、索引、外键与唯一约束。
   - `scripts/init-argus-db.sql`：PostgreSQL 容器初始化脚本，创建专属 `argus` 数据库与用户，实现与 Langfuse 内部库表空间隔离。
   - `services/eval-runner/app/db.py`：数据库引擎管理、`MigrationRunner`（基于文件 Checksum 事务执行迁移）、启动检查与防隐式降级机制。
   - `services/eval-runner/app/db_models.py`：映射 DDL 的 SQLAlchemy 声明式模型。
2. **企业级 REST API（零 URL 路径变量）**：
   - `services/eval-runner/app/api_registry.py`：
     - `POST /api/v1/agents`（创建 Agent）
     - `GET /api/v1/agents`（`?id=...` 查询或列表）
     - `POST /api/v1/agent-versions`（创建不可变版本）
     - `GET /api/v1/agent-versions`（`?agent_id=...&version=...` 查询）
     - `POST /api/v1/agent-versions/archive`（归档/停用版本）
   - `services/eval-runner/app/api_launches.py`：
     - `POST /api/v1/experiment-launches`（幂等创建并固化四维快照）
     - `GET /api/v1/experiment-launches`（`?id=...` 查询或列表）
     - `POST /api/v1/experiment-launches/run`（同步触发执行，基于原子条件更新锁）
     - `GET /api/v1/experiment-launch-items`（`?launch_id=...` 查询条目状态）
     - `GET /api/v1/execution-attempts`（`?item_execution_id=...` 查询重试日志）
3. **四维不可变 Manifest 与原子执行锁**：
   - `services/eval-runner/app/manifest.py`：创建时解析冻结 Dataset、Agent、Evaluator、Runner 四维版本及执行策略，写入不可变快照；提供基于原子条件更新的执行防重锁 `acquire_launch_execution`。
4. **安全凭据边界与配置指纹**：
   - `services/eval-runner/app/security.py`：禁止 URL 内嵌凭据与敏感 Query 参数；白名单约束 `env://` 环境变量；Trace 敏感 Header 自动脱敏。
   - `services/eval-runner/app/registry.py`：规范化执行配置并计算 SHA-256 `spec_digest`；支持 AgentVersion 粒度的 YAML 幂等导入（同版本同 digest 跳过，不同则报冲突）。
5. **重试机制收敛与状态解耦**：
   - `services/eval-runner/app/executor.py`：
     - 细粒度 `ErrorClassification`（`StrEnum`）。
     - 4xx 与 JSON 格式错误立即失败不重试。
     - 请求发送后的 `READ_TIMEOUT` 在非幂等 Agent 场景下禁止重试（副作用防护）。
     - 请求发送前记录 Attempt，请求完成后更新指标。
     - 状态解耦矩阵：`aggregate_launch_status` 区分技术执行成功但业务质量不达标（`SUCCEEDED + fail`）、Evaluator 报错（`FAILED + unknown`）与 Agent 远程调用失败（`FAILED + fail`）。
6. **OpenAPI 3.1 规范**：
   - `scripts/export_openapi.py`：离线导出工具，不依赖数据库与外部网络。
   - `docs/openapi.json`：导出的 OpenAPI 3.1 完整规范文件。

---

## 2. 自动化测试与验证结果

### 2.1 新增测试矩阵 (TDD)
- `tests/test_migration_and_schema.py`：验证 SQL 迁移执行、版本记录、表约束（唯一版本号、唯一条目、唯一 Attempt 序号）及防隐式降级机制。
- `tests/test_agent_registry_contract.py`：验证 URL 凭据拒绝、白名单环境变量、`spec_digest` 防篡改、不可变版本防覆盖、YAML 独立幂等导入。
- `tests/test_launch_manifest_freeze.py`：验证四维快照冻结、缺失版本解析失败拒绝、创建幂等键（同键同体幂等、同键异体 409 冲突）。
- `tests/test_executor_retry_classification.py`：验证错误分类、4xx 与 JSON 错误不重试、非幂等 Agent 的 ReadTimeout 防重放、5xx/429 有限重试、Attempt 预入库。
- `tests/test_status_and_langfuse_decoupling.py`：验证状态解耦矩阵（技术成功质量失败、评测异常、执行异常）。
- `tests/test_idempotency_and_atomic_lock.py`：验证原子条件更新执行锁，杜绝并发调用与重复运行。
- `tests/test_api_endpoints.py`：端到端验证所有零路径变量 REST 接口的请求、响应、执行与关联查询。

### 2.2 质量门禁执行记录

```bash
$ make validate
./scripts/validate.sh
[1/6] Parse YAML/JSON
configuration parse: OK
[2/6] Compile Python sources
[3/6] Verify zero evaluation-SDK dependency in Demo Agent
[4/6] Verify runner contains remote experiment + W3C propagation
[5/6] Run local behavior tests
...........................                                               [100%]
27 passed, 1 warning in 6.92s
[6/6] Docker Compose validation
docker compose config: OK
Validation complete.

$ .venv/bin/python -m ruff check services tests scripts
All checks passed!
```

### 2.3 验收标准清单 (Definition of Done) 核对

- [x] AgentVersion 执行配置不可原地修改，重复版本号被拒绝（409）。
- [x] 注册与执行 API 遵循零 URL 路径变量规范，所有路径均为静态常量。
- [x] 新增版本或配置变化不影响已有 Launch 的快照和执行输入。
- [x] 四维版本解析失败时不启动执行；不可恢复的历史版本不回退到当前版本。
- [x] 同幂等键同请求不重复创建 Launch，同键不同请求返回冲突（409）。
- [x] 并发调用 `/api/v1/experiment-launches/run` 仅有 1 个请求成功取得执行权，其余请求返回 409。
- [x] Retry 只增加 ExecutionAttempt，不重复创建逻辑 ItemExecution。
- [x] 错误分类明确：普通不可重试 4xx、格式错误立即失败；读取超时默认不重放；429/5xx 有限重试。
- [x] 评测质量不通过与执行失败、Evaluator 报错状态完全区分。
- [x] Langfuse 映射缺失或同步失败具有独立、可查询的状态，不误报为业务失败。
- [x] 仅允许受控 `credentialRef`，Secret 不进入持久化快照、Trace 或异常信息。
- [x] API 注册版本后无需重启 Runner 即可执行。
- [x] OpenAPI 文档生成独立可执行，不依赖数据库与 Langfuse 在线。
- [x] 保留 Demo v1 `2/6`、v2 `6/6` 回归基线，并通过 `make validate`。
