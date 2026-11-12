# Issue #255 多项目连接验证

验证日期：2026-11-12。环境：macOS ARM64、Python 3.12.4、Node 24、pnpm 10.5.2；
本地 Langfuse 固定为夹具中的 4.38.0 镜像 digest。没有使用 Cloud 或业务项目凭据。

## 范围与结果

连接在 Agent 层配置一次，全部已有版本与后续版本共用同一项目和一套凭据。
版本创建不接受项目覆盖；Launch 冻结当次连接。修改 Agent 配置只影响新建
Launch，不重算版本 digest，不改写历史 Manifest / Snapshot / Gate。
复用已有 Credential 的管理授权、加密、轮换、停用和外键引用；Dataset、OTLP、
Score 使用同一冻结绑定。配置与未测边界见[项目连接说明](../../langfuse-project-connections.md)。

| 验证 | 结果 | 说明 |
|---|---|---|
| RED → GREEN | 已观察 | 纠偏前新增 6 项 Agent 级需求测试全部失败，最小实现后全部通过；后续补充旧绑定、并发变更、注册/清理和 Managed 幂等回放反例 |
| `make validate` | PASS | Python 1735 passed、39 skipped、32 deselected；Console 911 passed；契约同步、Token lint、typecheck、build、Compose 配置均通过 |
| 全量 Console E2E | 141 passed | 真实 API 配置 Agent 项目后，创建两个版本无需重复授权或配置项目；键盘 Space、390px 无横向溢出；桌面/窄屏截图已人工复核 |
| 真实双项目 | 1 passed | 66.54 秒；两个 Agent 各有 v1/v2，4 次 Launch 成功；同一 Agent 的两个版本使用同一项目/凭据，跨项目查询为空 |
| `make trajectory-test` | 31 passed | 无跳过；真实 SDK、生产 Reader、W3C 父子关系和业务观测回归 |
| 未绑定 Demo 规则基线 | PASS | 全量 Python 回归保持 v1=2/6、v2=6/6；不等同于重新部署 Demo 全栈验收 |

Console 首轮全量测试中既有 AppShell Dashboard 链接用例失败一次；单独复跑
19 项全部通过，之后完整 `make validate` 的 Console 911 项全部通过，未修改或
放宽该测试。39 项跳过均为未配置 `TEST_POSTGRES_URL` 或要求 PostgreSQL
MVCC / 行锁的专项；SQLite 的并发配置反例不代表 PostgreSQL 行锁验收。
Node 版本与 CI 固定 Node 22 不同；build 保留已有大 chunk 提示。

纠偏回归覆盖：管理授权与拒绝未授权清除、Basic Auth/origin/环境约束、已有与
后续版本共享、拒绝版本覆盖、重新绑定仅影响新 Launch、Builtin / Managed
幂等回放不受重新绑定影响、历史凭据引用保留、YAML 配置层级、旧版本绑定
未迁移时失败关闭，以及读取 Dataset 期间连接变更时拒绝提交。

## 真实双项目证据

[无密钥回执](evidence/two-project-receipt.json)保存每次 Launch 的 Agent/版本、项目、
Credential 引用与实际 Attempt 修订、Dataset Item、Trace / Observation / Score ID
及交叉查询计数，不保存 pk/sk、认证头、原始业务内容或持久访问链接。

两把项目密钥由本地 Langfuse 原生函数生成，加密存入测试 Argus 数据库；每个
Agent 只绑定一次。两个 Agent 的 v1/v2 分别读取各自项目中同名 Dataset 的不同
用例，4 次执行均成功且 Builtin 质量通过。同步强制走冻结连接的直发 OTLP，
即使全局策略设为 `legacy` 也不使用全局客户端。各项目读到自己的
`remote-agent-http` 和 API Score，读不到对方数据。一次性 Docker 服务和数据卷
已由夹具清理。

复现命令：

```bash
.venv/bin/python -m pytest -q tests/test_agent_langfuse_binding.py tests/test_langfuse_connections.py
.venv/bin/python tests/fixtures/trajectory_agents/prepare.py
.venv/bin/python -m pytest -q tests/test_langfuse_connection_integration.py -m trajectory_local
make trajectory-test
make validate
ARGUS_E2E_API_PORT=18580 ARGUS_E2E_CONSOLE_PORT=18583 pnpm --dir services/console test:e2e
```

真实双项目测试使用 Builtin `source=API` Score，未声称完成多项目原生 Rule /
`source=EVAL` 验收。Managed 采纳路由、身份/来源拒绝、无部分采纳以及取消/迟到
结果隔离由契约与既有回归覆盖。Cloud、跨区域、项目数量上限、持续并发内存和
导出延迟仍未实测。当前结果是本地验证，不代表远端 CI、合并或发布。
