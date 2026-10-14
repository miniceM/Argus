# S4.1 来源校准与可信 Reader 准入

关联 #5、#127、#128；前置为轨迹 Manifest、Acquisition 与协调器。本批提供内部准入工厂及受控来源重复校准；正式 Worker 自动启用、执行/评测恢复、配置装载与生产负载容量校准仍在后续实施。

## 准入与传输

- Runner 可信配置注入不可变 `ReaderApproval` tuple，最多 16 条；精确匹配完整合同、`reader.py`/`codec.py` 的文件实现摘要和 API 文档摘要。无批准默认拒绝，Trace 和业务响应不能自报资格。
- `open(contract)` 先复核冻结合同/AgentVersion 来源，再匹配合同和实现；不匹配返回 `TRAJECTORY_SOURCE_UNQUALIFIED`，零网络请求。打开后只 GET 固定 `/api/openapi.yaml`，不接收额外 URL，不跟随重定向，不使用进程代理；凭据由可信配置注入。
- API 文档按 chunk 增量摘要，不解码/保存原文；超过 1 MiB、非 identity 编码、非 200 状态或摘要漂移均关闭并拒绝。错误仅暴露有限 reason，原始响应不进入异常。
- 探测与排队共用冻结单请求 timeout（上限 5s）及 Factory 共享并发槽，探测完成即释放槽，由 Acquisition 自身限流。无自动旧版本 fallback；消费者的异常不被当作探测异常。所有退出都尝试关闭客户端；关闭失败不掩盖已有异常或取消，正常退出的关闭失败转换为有限 `TRAJECTORY_SOURCE_TRANSPORT_ERROR`。
- 默认每个 Reader 拥有独立 HTTPX transport；注入接口为 `transport_factory`，调用方必须每次生成新实例，不能缓存共享已关闭的 transport。Reader 配置拒绝时也关闭已生成的 transport。
- 文档探测有独立 1 MiB 上限，不计入 Observation source wire；后续正式 Worker 必须仍从原响应 Checkpoint 计全程期限。准入只证明这个实现可消费获批合同/API，不保证一个 Trace 本身完整或可信；Normalizer/Acquisition 继续逐条校验和降级。

## 校准合同与范围

| 维度 | 固定值与验证 |
|---|---|
| Agent 来源 | native OTel、真实 Pydantic AI、HTTP-only；隔离 Langfuse 4.38.0，固定镜像/依赖沿用 S4.0 |
| 分组 | native/Pyd 各 serial、blocked、parallel、late_all ×10，HTTP-only serial ×10；共 9 组、90 次 |
| 截止与静默 | 原响应 Checkpoint.created_at +12s；quiet 0.3s；late_all 保留真实 8s 延迟，响应到 READY 至少 7.5s |
| 容量 | max_steps=64、page_size=2、max_pages=20、max_source_bytes=1 MiB、max_artifact_bytes=64 KiB；所有样本使用同一完整合同 |
| 容量边界 | native/Pyd serial 每个样本额外以 3 steps 和半制品容量消费生产 Reader 页面，断言 PARTIAL、对应截断 reason、Root 保留、steps/bytes 不越限；不批准这些测试用限制合同 |
| 身份与状态 | 原单 Attempt、Agent 输出 intent_match PASS、W3C/Root 父链仍逐样本断言；HTTP-only 到原截止保持 ROOT_ONLY；native/Pyd 真实分页/terminal/quiet 后 OBSERVED_COMPLETE |
| 数据语义 | 9 个公开字段 presence/null/type；真实父闭包、显式依赖、请求/执行配对、阻断和参数来源；并行无兄弟依赖；结构 Span 无工具能力 |
| 归档 | 原始 API 仅在进程内消费；完整逐样本字段元数据留测试 artifact，仓库只保存 [紧凑校准摘要](reader-calibration.json)，不保存业务 input/output |
| 工厂实读 | 每组 10 次全部通过后，由测试控制面批准其合同/Reader/API tuple，真实探测、读取一个 Observation 页并验证关闭 |

总响应到 READY 时间包含 Worker 完成后等待可见、完整分页和采集静默；测试允许截止收尾的 250ms 调度/DB 发布误差，它不延长采集 deadline。字段 null 不等于缺失：Root parent null 和结构 Span 的 input/output null 均原样统计，不用非空比例伪造语义完整度。

这些样本只批准受控银行夹具合同，不能用于任意 Agent、SDK、业务工具映射或生产容量的准入。90 次重复与容量余量不是生产压力测试或 SLA；部署方需测其真实负载/迟到分布并给出独立批准。任何合同、Reader/codec 实现或 API 文档改变，都使旧批准失效，不应自动扩大期限或限额。

## 可复现命令

```bash
python -m pytest -q tests/test_trajectory_reader_factory.py tests/test_trajectory_reader.py
make validate
python -m pytest -q tests/test_trajectory_backend_integration.py tests/test_trajectory_observation_integration.py -m trajectory_local
python tests/fixtures/trajectory_agents/summarize_calibration.py \
  <pytest-artifact>/s401/field-matrix.json docs/validation/s4-1/reader-calibration.json
```

真实门禁需先按 S4.0 准备固定镜像与隔离 Agent 环境；连接失败不得跳过或替换为 Mock。校准测试位于现有真实门禁模块，现有 CI 入口会自动执行，不修改工作流或 Cloud 配置。

## 验证记录

核验日期：2026-10-14；增量基线为 PR #174 的 `9b485fabe6acdd8930d563cbe3bd603f04520674`。

- RED→GREEN：缺少工厂模块；7 项消费异常/非法 timeout、5 项非法批准配置、1 项非法合同序列化警告泄露；审阅后再以 3 项清理故障、1 项共享 transport 复用、1 项配置拒绝的孤儿 transport 回归复现并修复。共享合同校验禁用 serializer warning，但仍严格拒绝非法值。
- 最终 Reader/工厂：77 PASS（31 项工厂、46 项既有 Reader）。隔离 PostgreSQL 定向兼容回归：799 PASS、0 skip。`make validate`：8 步 PASS，1583 Python、847 Console，OpenAPI/生成类型与 Compose 校验正常。
- 最终完整真实门禁：52 PASS、0 skip，约 592.65s；包含 90 次校准调用、9 组工厂实读、20 个真实串行样本的 Step/Artifact 容量边界及原有兼容用例。完整 XML、逐样本元数据和服务/依赖信息留测试 artifact；紧凑摘要按上述命令生成。
- 9 字段 absence 均为零；最大 13 steps、深度 4、8737 bytes artifact。native/Pyd 累计采集预算扣减最大分别 53044/92019 bytes。公开 API 文档实测 509216 bytes，摘要在三次隔离门禁一致；实际批准三项身份详见摘要。
- 迟到组总响应到 READY 最大约 11.095s；HTTP-only 原 12s 截止收尾最大约 12.081s。HTTP-only 重复查询可耗满 1 MiB 累计预算，未完成请求按已有保守预留规则扣减；仍输出 ROOT_ONLY，不能据此声明未观察到内部工具或容量具有余量。每组仅 10 个样本，其经验 P95 与最大值相同，不能外推生产 SLA。
- 一次独立只读整体审阅提出 1 Required、1 Optional；本批以一轮 RED→GREEN 自修关闭清理异常遮蔽及 transport 生命周期问题，无未处理 Required；未发起第二次审阅。

新增容量验证的中间运行误用了测试 HTTP 客户端 float DTO，绕过生产 Decimal wire codec，导致两个真实串行组拒绝输入；该失败不计通过。已改为生产 Reader 页面并在上述最终完整门禁中验证。未放宽参数类型、容量、quiet 或原截止。
