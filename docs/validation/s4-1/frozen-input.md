# S4.1 冻结轨迹输入与 Snapshot 引用

关联 #5、#127、#128，基于 PR #177 的成功响应恢复。本批提供 T2 的本地输入装载与引用序列化；T3 的轨迹规则和 T4 的 required 完整度、聚合及资格合同继续实施。

## 输入与兼容性

`trajectory/inputs.py` 的 `EvaluationContext` 保持技术契约的五个字段：`output`、`expected_output`、`input_payload`、`trajectory`、`trajectory_evidence_ref`。业务字段来自原响应 Checkpoint 的独立拷贝；轨迹来自本地不可变制品，读取重新验证摘要、合同、scope、实际 AgentVersion profile 和完整度。PENDING 私有候选不发布给评测；UNAVAILABLE、缺少可信 root 或制品失效时 trajectory/ref 为空，内部装载结果保留有限原因。

正常 Worker、成功响应执行恢复与 eval-only 都向共享 `evaluate_frozen_item` 提供可选上下文。只有明确声明 `input_contract.input_kind=trajectory` 的执行器收到 context 参数；已发布的 Output Binding 继续调用原 `fn(output, expected_output)`，本批不注册轨迹规则版本，也不把缺轨迹改为已有输出规则的失败。

Manifest 1.3 的可恢复业务输入额外核验完整 Manifest digest、实际 Item 的 Launch/Dataset/generation、原 active/final Attempt 的归属，以及唯一 DatasetItem 的原 input/expected_output；篡改时 eval-only 在评分之前阻断，保留 UNKNOWN，不调用 Agent。旧 Manifest 不增加轨迹读取或原输入校验；旧执行器与旧 Snapshot 不回填新字段。

eval-only 在已有独立 evaluation lease 下只装载当前原代 Checkpoint 的终态证据，不继续 PENDING 采集，不打开 Reader、不查询最新 Trace、不创建业务 Attempt。业务输出恢复仍服从原 Checkpoint TTL；轨迹制品的历史读取独立于该 TTL。

## Snapshot 合同

仅 Manifest 1.3 的结果条目新增私有 `trajectory_input`，精确包含：

```json
{
  "version": "1.0",
  "trajectory_contract_digest": "sha256:<digest>",
  "acquisition": "原 TrajectoryAcquisition JSON 或 null",
  "reason_code": "有限不可用原因或 null"
}
```

这是字段说明，`acquisition` 在实际载荷中是对象或 null。引用随 `source_result_digest` 冻结，不复制 Observations 或完整 StandardTrajectory。可信 scope 缺失时只保存 null Acquisition 和有限原因，不构造虚假身份。版本、字段集合、Acquisition、原因、条目身份和两个 Snapshot 摘要均经过读取校验。

历史装载使用 Snapshot 自身的 Manifest、原 generation/Attempt/root 与 EvidenceRef；不依赖当前 Launch 的 Manifest、Checkpoint 是否保留或当前最新 Trace。授权清理 Checkpoint 后仍可读取保留的 artifact。制品删除、损坏或错配只返回有限不可用状态，历史 items 与摘要不改写；Snapshot 创建时已不可用的输入不会因后续存储修复而升级。

本批不写 `evidence_contract_version=trajectory-1.0` 或 `required_evidence_status`，不启用新的 Baseline/Gate 资格。T4 必须将两者与新聚合合同同时交付，避免半契约快照。当前轨迹输入不可用只表示证据缺失；新轨迹规则产生 UNKNOWN 的验收由 T3/T4 完成。

## 验收与复现

```bash
python -m pytest -q tests/test_trajectory_inputs.py \
  tests/test_trajectory_execution_recovery.py tests/test_issue_84_eval_only_retry.py \
  tests/test_issue_85_snapshot_revision.py tests/test_issue_81_frozen_execution_identity.py
make validate
python -m pytest -q tests/test_trajectory_backend_integration.py \
  tests/test_trajectory_observation_integration.py -m trajectory_local
```

PostgreSQL 用例需独立测试数据库的 `TEST_POSTGRES_URL`，逐测试创建隔离 schema；未配置时明确跳过，不能算 PostgreSQL 通过。真实集成前置及隔离资源约束沿用 [S4.0 验收记录](../s4-0/README.md)。

测试覆盖 READY/PENDING/UNAVAILABLE、独立拷贝、原输入篡改、过期或清理 Checkpoint、制品损坏/删除、严格版本/字段/摘要/跨 scope、原 generation 的历史读取及旧 Output 兼容。五种来源场景各走正常、采集前中断和 READY 后中断，真实消费者检查原 ref 的传递、eval-only 零来源/业务调用，以及清理 Checkpoint 后原 Snapshot 的本地读取。来源校准仍使用原 12s deadline 和 8s late_all，没有放宽预算或凭 Trace 自报准入。

2026-10-14 核验（基线 PR #177 `7c2524b`，对应本批差异）：

| 门禁 | 实际结果 |
|---|---|
| TDD | 缺输入模块、正常/eval-only 未传 context、显式轨迹声明未透传、畸形/未知 Snapshot 版本均实际 RED→GREEN；审阅后错配 Dataset/Launch/Attempt 的三项回归实际 RED→GREEN，没有放宽断言 |
| 本批输入回归 | 52 项：24 个持久化场景分别跑 SQLite/PostgreSQL，另有旧输入零新 IO、正常 Worker 与两类 Executor 分发；均在最终定向门禁内通过 |
| 最终 PostgreSQL 定向 | 937 PASS，零跳过，42.50s；独立 loopback Docker PostgreSQL、逐测试 schema，退出前核验 owner 后清理 |
| 最终 `make validate` | 全部 8 步 PASS：1697 Python、847 Console；Python 66.53s，Token lint、契约生成无 drift、Compose 通过 |
| 完整真实门禁 | 67 PASS，零跳过，666.91s；审阅修复前启动，含 90 次资格样本、15 项正常/恢复消费者与旧链路兼容 |
| 修复后真实消费者 | 15 PASS，零跳过，101.24s；固定三类执行路径，验证原 ref 的 eval-only 传递及清理 Checkpoint 后原 Snapshot 的读取，原评分保持，禁止新的业务调用与 Reader Factory |
| 独立审阅 | 整批一次只读审阅 Critical 0、Required 1、Optional 0；主代理补齐实际 Item 与原 Attempt 的归属校验，一轮 TDD 修复，无二审 |

最终定向与两次真实门禁的 JUnit 均 errors/failures/skipped=0。最终日志/JUnit 按 `t2-input-final-reviewed-pg-targeted`、`t2-input-final-reviewed-validate`、`t2-input-initial-real`、`t2-input-final-reviewed-auto` 前缀保存在交付执行记录；CI 会另生成真实门禁 artifact。首轮输入及审阅回归中的测试夹具错误已纠正，中间失败保留，以上只计有效行为 RED 与最终通过结果。既有 datetime/Console chunk 警告不在本批修改范围。

本批不宣告 S41-10 或全 S4 完成：轨迹规则的 UNKNOWN、required 完整度、新资格、诊断和 Gate 仍须继续。Cloud 由 #162 所在任务负责，本批不运行 Cloud，不将占位检查计入真实验收。
