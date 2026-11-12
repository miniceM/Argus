# Issue #255 多项目连接验证

验证日期：2026-11-12。环境：macOS ARM64、Python 3.12.4、Node 24、pnpm 10.5.2；
本地 Langfuse 固定为夹具中的 4.38.0 镜像 digest。没有使用 Cloud 或业务项目凭据。

## 范围与结果

本次交付按 AgentVersion 冻结项目绑定，复用已有 Credential 的管理授权、加密、
轮换、停用和外键引用；Dataset、OTLP、Score 使用同一绑定。来源为 `BOUND`
时失败关闭，`GLOBAL_ENV` 沿用旧路径。配置与未测边界见
[项目连接说明](../../langfuse-project-connections.md)。

| 验证 | 结果 | 说明 |
|---|---|---|
| RED | 已观察 | 缺失绑定实现、未授权绑定、全局 Tracer 泄漏、OTLP 重定向、同步 PREPARED、修订审计、Run Score 日志脱敏和 YAML 保留绑定均先失败后修复 |
| `make validate` | PASS | Python 1726 passed、39 skipped；Console 908 passed；OpenAPI / TS 契约、Token lint、typecheck、build、Compose 配置均通过 |
| 全量 Console E2E | 141 passed | 包含真实 API 的版本绑定、来源展示、凭据引用保护 |
| 新增页面复核 | 1 passed | 补充键盘 Space 勾选、390px 无横向溢出；实际截图复核桌面与窄屏 |
| 连接定向回归 | 18 passed | 管理权限、目标与环境、冻结、轮换、停用、认证/项目错配、OTLP 失败、同步入口与评分恢复、YAML 导入、日志脱敏 |
| 两个真实项目 | 1 passed | 最终复跑 37.79 秒；真实 Dataset / HTTP Agent / OTLP / Score API，交叉查询为空 |
| `make trajectory-test` | 31 passed | 真实 SDK、生产 Reader、W3C 父子关系和业务观测回归，无跳过 |
| 未绑定 Demo 基线 | PASS | `test_expected_regression_delta` 保持 v1=2/6、v2=6/6；不等同于重新部署 Demo 全栈验收 |

39 项跳过均为未配置 `TEST_POSTGRES_URL` 或要求 PostgreSQL MVCC / 行锁的专项；
不将 SQLite 回归解释为这些 PostgreSQL 并发/锁测试已通过。Node 版本
与 CI 固定的 Node 22 不同。build 保留已有的大 chunk 提示。

## 真实双项目证据

[无密钥回执](evidence/two-project-receipt.json)仅保存测试项目、Launch、Credential
引用及实际 Attempt 修订、Dataset Item、Trace / Observation / Score ID 和交叉
查询计数，不保存 pk/sk、认证头、原始业务内容或持久访问链接。

两把项目密钥由本地 Langfuse 原生函数生成并加密存入测试 Argus 数据库。两个
Agent 分别绑定独立项目，读取同名 Dataset 的不同用例；执行成功且 Builtin
质量通过。同步强制走绑定的直发 OTLP，即使全局策略设为 `legacy` 也不使用
全局客户端。各项目读到自己的 `remote-agent-http` 和 API Score，读不到对方数据。
一次性 Docker 服务和数据卷已由夹具清理。

复现命令：

```bash
.venv/bin/python tests/fixtures/trajectory_agents/prepare.py
.venv/bin/python -m pytest -q tests/test_langfuse_connection_integration.py -m trajectory_local
make trajectory-test
```

真实双项目测试使用 Builtin `source=API` Score，未声称完成多项目原生 Rule /
`source=EVAL` 验收。Managed 采纳路由、身份/来源拒绝、无部分采纳以及取消/迟到
结果隔离由契约与既有回归覆盖。Cloud、跨区域、项目数量上限、持续并发内存和
绑定增长后的导出延迟仍未实测。当前结果是本地验证，不代表远端 CI、合并或发布。
