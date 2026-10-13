# S4.1 第二批：有界只读 ObservationReader

本批实现 [采集方案](../../plans/2026-10-13-s4-trajectory/03-s4-1-acquisition-and-schema.md) 的 Langfuse v2 单页传输入口，供后续 Acquisition 注入。未启用 Runner 新运行路径，也不判轨迹完整度、工具规则或质量。

`app.trajectory.reader.ObservationReader.read_page(scope, *, cursor, page_size, timeout_seconds, max_bytes)` 只接受受控 Scope；调用方必须从冻结合同提供预算。结果 `ObservationPage` 仅包含进程内原始 rows、后继 cursor 和源字节数；原始内容不持久化、不返回浏览器、不写日志，必须交由后续 Normalizer/Redaction 校验。

## 已验证边界

- 唯一业务请求为 `GET /api/public/v2/observations`；使用实测 `traceId`、`limit`、`fields=core,basic,time,io,metadata`、`expandMetadata` 与 `meta.cursor`，不调用 Agent、不写 Langfuse、不内置重试或跟随重定向。
- HTTPX 各次 I/O timeout 外再加整次请求/流的绝对 timeout；取消向上传播并关闭 response，调用方用 `async with` 或 `aclose()` 关闭客户端。
- 单页最多 100 rows、5 秒、8 MiB，调用方可传更低剩余预算。检查 Content-Length，流式累加前检查实际 bytes；请求 identity 编码并拒绝压缩响应，避免解压后再限界。局部精确 JSON decoder 拒绝重复键、非法数字、超深和超长字段。
- 401/403 为鉴权错误，400/404 等为契约错误；408/传输 timeout、429、5xx 可重试。异常只暴露稳定 code/retryable/byte count，不返回 Header、凭据、URL 或上游 body。
- URL/凭据来自已授权 Runner 配置；禁止 URL 内 userinfo、query、fragment。默认非 loopback 地址必须 HTTPS；loopback/localhost 允许本地 HTTP，内部服务的 HTTP 需显式 `trusted_http_hosts=frozenset({...})` 精确准入，不根据 DNS 私网结果自动放行。TLS 校验启用，客户端忽略代理环境。

本批不证明 root/Attempt 闭包或来源能力；这些由后续 Normalizer、Assembler 与 profile 校准负责。分页页数、累计预算、游标回环、quiet window、deadline、并发 semaphore 和恢复仍属于后续 Acquisition。不能把单页读取成功解释为观测完整。

## 验证与复现

```bash
.venv/bin/python -m pytest -q tests/test_trajectory_reader.py tests/test_trajectory_schema.py
make trajectory-prepare
.venv/bin/python -m pytest -q tests/test_trajectory_backend_integration.py
make validate
```

46 项 Reader 单测与 85 项 schema 测试通过。独立审查指出 408 分类缺陷；新增 408/远端 HTTP 准入回归实证 3 failed/12 passed 后修复。真实隔离 Langfuse 的两次 SDK/API 门禁交叉验证生产 Reader：分页耗尽、无重复 ID、Trace/父链及九项字段逐值一致；使用不可达 HTTP/HTTPS/ALL_PROXY 且清空 NO_PROXY，验证实际传输仍成功。最终完整门禁与 CI 结果记录在本批 PR。

最终本地真实门禁 2 passed/0 skipped/22.51s；`make validate` 通过，926 Python、847 Console、Token lint/typecheck/build、契约快照同步与静态 Compose 校验均通过。4 项现有 PostgreSQL 专项因未配置 `TEST_POSTGRES_URL` 跳过，31 项真实采集由独立 CI 门禁执行；本批已单独实际运行受改动影响的 2 项真实 SDK/API/Reader 验收。
