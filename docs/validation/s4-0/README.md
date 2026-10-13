# S4.0 隔离观测运行环境

此增量提供真实本地 Langfuse SDK → OTLP → v2 API 的测试基础设施，关联 [#5](https://github.com/miniceM/Argus/issues/5)、[#127](https://github.com/miniceM/Argus/issues/127)。两种业务 Agent 的执行与 Attempt 归属在后续独立 PR 验收，本基础设施通过不代表 S4.0 完成。

## 运行与隔离

准备 Runner Python 3.12 环境后，从仓库根目录执行：

```bash
make trajectory-prepare
make trajectory-test
make validate
```

准备阶段拉取六个固定 digest 的镜像；测试阶段 `--pull never`。服务使用 Langfuse web/worker 4.38.0、PostgreSQL 17、ClickHouse 25.12、Redis 7 和 MinIO。测试前核验 Docker endpoint 为本机 Unix socket，拒绝 SSH/TCP 和远端 context；拒绝时尚未调用 Compose。

每次测试创建随机 Compose project、临时卷与临时凭据，仅 Web 发布随机 `127.0.0.1` 端口，关闭桥接网络出站 masquerade。不会加载开发者 `.env` 或 Cloud 配置。SDK、HTTP 与 OTLP 传输禁用环境代理；隔离本测试进程的 `LANGFUSE_*`、`OTEL_*` 与代理环境，退出后恢复。清理逐项执行，保留原始失败并继续尝试移除本项目的服务与卷。

## 断言与证据

真实 SDK 创建根和两个子 Observation，向真实 OTLP API 写入；从实际部署实例读取 OpenAPI，再使用 `/api/public/v2/observations` 的 `traceId`、`fields`、`expandMetadata`、`limit` 和 `meta.cursor` 查询。断言九字段存在、时间已结束、父链完整且属于同一个 Trace。

分页请求 limit=2，每页实际返回条数不得超过 2，必须出现续页；分页唯一 ID 集合必须与 limit=1000 完整查询相同。CI 留存 JUnit 和仅含字段类型/计数/关系 ID 的矩阵，不上传凭据、Header 或 input/output 原文。矩阵区分缺 key、null、空字符串、空对象和空数组。

这层验收不能推导业务工具能力、人工审批可信度或冻结 Snapshot 的发布资格。完整 S4.0 验收仍遵循 [实施方案](../../plans/2026-10-13-s4-trajectory/02-s4-0-observation-validation.md)。
