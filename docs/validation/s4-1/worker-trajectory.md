# S4.1 正式 Worker 自动采集

关联 #5、#127、#128；本批将已有 Manifest 1.3、可信 Reader 与协调器接入 `ExecutionWorker`。执行恢复、eval-only 输入 Adapter 与 Snapshot 新资格仍在后续实施；当前 Output Binding 的评分与 Demo 基线不变。

## 执行与恢复边界

1. 正常调用 Agent 一次，HTTP 结束即释放原 Agent 并发许可，保存原成功响应 Checkpoint。
2. 仅显式 Manifest 1.3 调用 Collector；scope 来自 Checkpoint 的 Launch/Item/DatasetItem/Attempt/generation/Trace/root，合同只从冻结 Manifest 解码，并复核数据库的不可变 AgentVersion 分支。
3. 原执行租约继续续期。Coordinator 先提交 lease/generation/CAS 和累计预算预留，Lazy Reader 才读取 Runner 批准配置、探测固定 API 文档并分页；整个 probe/排队/采集共用原 Checkpoint 截止，不重新计时。
4. READY/UNAVAILABLE 持久化原采集账本；未知合同、API 漂移、配置错误拒绝，不自动回退或批准。终态回读不打开 Reader；取消沿用保守预留，中止轮次并关闭 Reader。
5. 租约/代数/Launch 取消 fence 失败或租约事件触发会停止后续评测；外部 Task 取消原样传播。缺可信 Root/Checkpoint、存储失败只输出有限诊断，不伪造 ID、公共 Acquisition 或质量结论，不丢原成功响应。
6. 现有冻结 Output Binding 继续评测原输出。新轨迹 Binding 与 required-evidence UNKNOWN 判定由后续 Adapter/T4 接入，不在采集服务直接判断质量；旧 Manifest 1.2 无新增配置或源访问。

Missing scope 的有限诊断目前记录在 Worker 日志，不能落一个缺少合法 scope 的公共 `TrajectoryAcquisition`。根 ID/Checkpoint 缺失在原执行记录中保留，后续规则输入 Adapter 必须把不可用证据投为 UNKNOWN；不能把该路径当作完整轨迹。

若成功响应保存后发现冻结 Manifest 1.3 合同损坏，Collector 返回有限的 `TRAJECTORY_WORKER_CONTRACT_INVALID`，不读取配置、数据库 scope 或在线源。Worker 仍完成原业务成功状态；共享冻结评测校验拒绝损坏合同，结果为 `eval_status=failed`、`quality_conclusion=unknown`、无 Score，不能回退旧规则把该输入判为通过。

## Runner 可信部署配置

```dotenv
ARGUS_TRAJECTORY_READER_APPROVALS=[]
```

默认空列表拒绝所有来源。批准由部署方对其实际 Agent/SDK/映射/容量校准后提供，数组元素严格只有以下三个键，各值为 `sha256:` 加 64 个小写十六进制字符：

```json
[
  {
    "trajectory_contract_digest": "<完整冻结合同摘要>",
    "reader_artifact_digest": "<reader.py/codec.py 实现摘要>",
    "api_document_digest": "<固定公开 API 文档原字节摘要>"
  }
]
```

上面为格式占位，不能直接作为批准值。限制为 UTF-8 16 KiB、最多 16 条，重复 JSON 键或未知字段拒绝；不接受凭据、文件路径、URL、业务数据或 Trace 自报资格。Root Compose 透传这个变量，`.env.example` 默认 `[]`；Cloud Compose 已有 `env_file`，可在部署方自己的 env 配置中提供同名变量，不修改 Cloud 工作流或凭据。

连接仍使用现有 `LANGFUSE_BASE_URL`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`，沿用配置边界的不可见字符清理。仅固定 Docker 内部 host `langfuse-web` 与 loopback 可用 HTTP，其余连接要求 HTTPS；Trace/公开 DTO 不能增加豁免。有效 Factory 配置在首次 1.3 消费时缓存，更新批准、连接或凭据后应重启 Worker；实现/API/合同变化须先重新校准。

文档 probe 有独立 1 MiB 上限，不计正常 Observation 字节；取消仍按协调器的未知在途保守预留规则处理，不能退还不确定额度。Probe 与采集共享原时间预算；缺省批准不会创建 HTTP 客户端或访问网络。

## 验证

```bash
python -m pytest -q tests/test_trajectory_worker.py tests/test_trajectory_coordinator.py \
  tests/test_issue_84_eval_only_retry.py tests/test_orchestrator_and_worker_s2.py
make validate
python -m pytest -q tests/test_trajectory_backend_integration.py \
  tests/test_trajectory_observation_integration.py -m trajectory_local
```

新增单测覆盖 legacy 零访问、先预留再 probe、配置边界、原截止已过零源访问、任务/租约事件取消、lease/generation/Launch fence、资源关闭、单 Attempt、响应先保存/Agent 许可先释放、采集诊断不改变旧 Output 评分。五项真实消费者覆盖 native OTel、Pydantic AI、HTTP-only、8s late_all 和未批准来源，复用上一批提交的真实校准批准，禁止从本次 Trace 自动批准。

2026-10-14 本批核验结果（基于 PR #175 `15eb344c`，对应自动测试文件及本批差异）：

| 验证 | 实际结果 |
|---|---|
| TDD | Collector 缺失、有限来源诊断和正式 Worker 未调用等边界先确认 RED；独立审阅发现的损坏合同两项回归也实际 RED，修复后新 Worker 测试 26/26 PASS |
| PostgreSQL 定向门禁 | 修复后 849 PASS、零跳过；使用独立 loopback Docker PostgreSQL，退出时核验容器所有权后清理 |
| `make validate` | 修复后全部 8 步 PASS：1609 Python、847 Console；57 个显式真实集成用例由单独命令执行；OpenAPI/生成类型无 drift，Compose 校验通过 |
| 完整真实门禁 | 审阅修复前 57 PASS、零跳过，612.58s；包含原 90 次资格校准及本批 5 个自动 Worker 消费场景 |
| 修复后的真实消费者回归 | 5 PASS、零跳过，47.39s；覆盖 native OTel、Pydantic AI、HTTP-only、原 8s late_all、未批准来源；不重复整套 90 样本校准，修复仅限非法合同异常边界 |
| 部署透传 | Root Compose 在变量未设置时传入 `[]`，显式三摘要 JSON 原样传入；没有启动或重启实际部署 |
| 独立审阅 | 整批一次只读审阅：Critical 0、Required 1；主代理接受有限异常边界，补两项 RED→GREEN 后解决；损坏合同不评分，由原冻结评测边界失败/UNKNOWN |

定向 PostgreSQL 和两次真实执行的 JUnit 均为 errors/failures/skipped=0。运行日志、JUnit 与真实观测元数据按本批 `t1-worker-initial-real`、`t1-worker-final-auto`、`t1-worker-final-pg-targeted`、`t1-worker-final-validate` 前缀保存在交付执行记录；CI 的真实门禁会再次产生对应 artifact。已有 `datetime.utcnow()` 弃用和 Console chunk 大小警告未纳入本批改动。Cloud 门禁未运行，不将占位检查当作 Cloud 验收。

本批尚未覆盖成功响应后再调度、eval-only Adapter、Snapshot 轨迹资格和规则/Gate。S41-10 与全 S4 验收仍须后续完成；不能据此关闭 #5、#127 或 #128。
