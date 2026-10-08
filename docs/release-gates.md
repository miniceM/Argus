# 版本化发布门禁

ReleasePolicy 将 Agent、environment 和发布阈值固定为不可变版本。Gate 只读取指定 RunResultSnapshot；活动 Launch、当前 Baseline 和 Langfuse 同步进度不会改变已保存的结论。

## 创建策略并取得结论

先注册 Agent，完成具有完整证据的正式评测，通过 API 创建策略。完整字段见 [OpenAPI](./openapi.json)。例如保存以下内容为 `release-policy.json`：

```json
{
  "name": "banking-production",
  "version": "1.0.0",
  "agent_id": "banking-agent",
  "environment": "production",
  "block_critical_failures": true,
  "rules": [
    {"id": "quality", "metric": "pass_rate", "operator": ">=", "threshold": 0.95},
    {"id": "regressions", "metric": "regression_count", "operator": "<=", "threshold": 0}
  ]
}
```

```bash
curl --fail-with-body -X POST "$ARGUS_API_URL/api/v1/release-policies" \
  -H 'Content-Type: application/json' --data-binary @release-policy.json
```

同名同版本且内容相同的提交返回原策略；内容不同返回 409，修改规则必须创建新版本。`latest`、`dev`、`main`、`head` 不能作为策略版本。

评测完成后读取 `/api/v1/experiment-launches/{launch_id}/summary`，捕获 `snapshot_id`。向 `POST /api/v1/release-gates/evaluate` 提交 `policy_name`、`policy_version`、`candidate_launch_id`、`candidate_snapshot_id`。通过 `GET /api/v1/release-gates/{gate_id}` 读取原结果；重复提交同一 Snapshot 与策略不会产生新结论。

## 规则和放行条件

| 指标 | 含义 |
|---|---|
| `pass_rate` | 全部必需用例中的质量通过比例 |
| `evaluation_coverage` | 完整、可判定的评测覆盖率 |
| `execution_error_rate` | 执行失败比例 |
| `critical_failure_count` | 至少一条 critical 规则失败的用例数 |
| `p95_latency_ms` | 全部用例均有有效延迟时的 p95 |
| `score_mean:<evaluator_id>` | 全部用例均有该数值指标时的均值 |
| `regression_count` | 相对冻结 Baseline 的退化用例数 |
| `pass_rate_delta` | Candidate 通过比例减去 Baseline 通过比例 |
| `p95_latency_regression_percent` | 相对 Baseline 的 p95 增长百分比，Baseline 为零时不可用 |

运算符为 `>=` 或 `<=`，阈值必须是有限数值。布尔、无序分类和文本不能被隐式平均；分类仅使用既有冻结归一化语义。

仅 `PASS` 且 `releasable=true` 可放行。阈值违反或 critical 失败得到 `FAIL`。证据不完整、历史契约未知、缺失指标、critical 证据缺失或不可比得到 `UNKNOWN`，同样阻断发布，并返回逐规则实际值和原因码。

相对规则只使用 Candidate Manifest 中的 `baseline_snapshot_id`；更新当前 Baseline 不会移动该引用。两侧必须属于同一 environment、覆盖相同用例、拥有完整证据，并满足 Dataset 身份、Measurement、Quality Policy、Aggregation/Comparison 的既有可比契约。缺少 Baseline 时不会伪造零退化。

Gate 返回固定修订的报告链接、策略摘要、候选与基线 Snapshot ID/revision/digest、引擎版本和创建时间。数据库外键保留被引用的 Snapshot；Purge 此类 Agent 返回 409，包括并发插入 Gate 时的冲突。策略有显式 Agent 外键；没有 Gate 的策略随 Agent 清理，避免遗留可被同名 Agent 复用的策略。

## 部署与治理边界

流水线使用 [CLI 与 CI/CD 示例](./release-cli.md) 等待评测、固定 Snapshot 并取得退出码。

数据库升级追加迁移 `013_release_gates.sql`。本期提供规则引擎与 API，不包含 Console 策略管理页。与既有控制面一致，生产部署必须在入口实施身份认证和权限控制。

本期没有 Override 接口，不能把 `FAIL` 或 `UNKNOWN` 改成可发布。授权 Override、SSO/RBAC、操作者审计与保留期治理仍由 Issue #6/#7 承接；不可变 Gate 用于追溯原始判断，不代替完整审计系统。

创建策略时锁定 Agent 行并要求 active，防止 Purge 中途创建策略。引擎仅接受当前实现支持的冻结聚合/比较 digest；双方历史契约即使相同，若实现不支持仍返回 AGGREGATION_CONTRACT_UNSUPPORTED / UNKNOWN。

门禁引擎 v2 要求 Dataset / Agent / Evaluator / Runner 四类冻结身份完整，缺失或使用动态版本引用时返回 UNKNOWN；Runner build identity 复用既有可靠性校验。新引擎重新评估时不会复用旧引擎的 PASS，旧 Gate 仍可按原 ID 查询。

报告链接使用 ARGUS_CONSOLE_BASE_URL（默认 http://localhost:18083），需配置为浏览器可访问的 Console 地址，支持路径前缀；Runner 的 18080 端口提供 API。Compose 已转发此变量。

CLI 幂等键上限为 128 字符，超长输入在任何 HTTP 请求前返回结构化 UNKNOWN（INVALID_IDEMPOTENCY_KEY）。CI 模板使用数值项目/流水线 ID 或稳定 SHA-256 摘要，避免长仓库或 Job 路径超过数据库限制。
