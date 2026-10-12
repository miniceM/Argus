# CLI 与发布流水线

`argus` 是薄 HTTP 客户端，不在业务 Agent 中加入评测 SDK。控制面负责冻结版本、异步执行、持久化结果和门禁判定；CLI 等待结果并用退出码控制发布。

## 安装与运行

Python 3.12，在已检出的 Argus 仓库执行：

```bash
python -m pip install ./services/argus-cli
argus eval run --help
```

在业务 CI 中安装固定的 Argus commit，避免客户端随分支漂移：

```bash
python -m pip install "argus-release-cli @ git+https://github.com/miniceM/Argus.git@${ARGUS_CLI_REF}#subdirectory=services/argus-cli"
```

`ARGUS_CLI_REF` 应为审核过的 40 位 commit SHA。示例流水线会校验其格式。

配置 `ARGUS_API_TOKEN` 时，API 必须使用 HTTPS；仅 localhost 与 IP loopback 的本地开发地址允许 HTTP。参数解析错误同样返回结构化 UNKNOWN 与报告，不回显原始参数值。

先按 [发布门禁](./release-gates.md) 创建固定策略，并确认 Agent、Dataset 与 Evaluator 的具体版本。`--dataset-version` 必须与平台所选 Dataset 的已确认版本一致；不得使用 `latest`。正式部署的 Runner 自身也必须配置不可变 Build ID。

```bash
export ARGUS_API_URL='https://argus.example.com'
# 入口需要 Bearer 认证时，由 CI Secret Store 注入 ARGUS_API_TOKEN。
argus eval run \
  --agent banking-agent --agent-version v2 \
  --dataset banking-agent-regression --dataset-version "$DATASET_VERSION" \
  --evaluator intent_match@1.0.0 --evaluator required_tool_match@1.0.0 \
  --evaluator pii_safe@1.0.0 --evaluator escalation_match@1.0.0 \
  --policy banking-production --policy-version 1.0.0 \
  --idempotency-key "$CI_RELEASE_KEY" \
  --wait --timeout 300 --report artifacts/argus-gate.json
```

`run` 默认等待，`--wait` 可显式表达意图。Evaluator 必须逐个指定 `id@version`，Agent、Dataset 和 ReleasePolicy 也必须固定版本。默认使用策略的 environment，显式 `--environment` 必须一致；`--baseline-snapshot` 可选择明确基线，否则使用控制面在创建时冻结的当前绑定。相对规则要求有可比 Baseline。

标准输出始终是一份 JSON 结果，`--report` 额外保存同样内容。Gate 结果包含固定 Snapshot 报告链接、规则实际值、原因码和策略版本。CLI 错误输出 `UNKNOWN`、错误码、已知 Launch ID 与提交幂等键；不输出原始远端错误内容或 API Token。

| 退出码 | 含义 | 流水线行为 |
|---|---|---|
| 0 | Gate 为 `PASS` 且 `releasable=true` | 可进入发布步骤 |
| 1 | Gate 为 `FAIL` | 阻断发布，检查违反的规则 |
| 2 | Gate 为 `UNKNOWN`，或参数/网络/API/超时/报告写入错误 | 阻断发布，检查证据或恢复任务 |

`--timeout` 是整个操作的等待预算；每次 HTTP 请求最多使用剩余预算中的 10 秒，响应超过预算也不能放行。超时后远端任务可能仍在执行，CLI 不自动取消。写请求不会被自动重试；网络结果不明时使用相同参数、相同 `--idempotency-key` 再执行，或使用已知 Launch ID 等待：

```bash
argus eval wait --launch-id "$LAUNCH_ID" \
  --policy banking-production --policy-version 1.0.0 \
  --timeout 300 --report artifacts/argus-gate.json
argus eval result --gate-id "$GATE_ID" --report artifacts/argus-gate.json
```

`wait` 不创建、不重新启动任务；`result` 只读取已保存的 Gate。服务不可用时返回 2，由流水线或操作者决定何时恢复。修改策略版本会得到新的门禁结论，不改写原结果。

## CI/CD 示例

模板包含安装固定客户端、注入入口凭据、等待门禁、保存 JSON 证据、成功后才进入发布步骤：

- [GitHub Actions](../examples/ci/github-actions.yml)
- [Jenkins](../examples/ci/Jenkinsfile)
- [GitLab CI](../examples/ci/gitlab-ci.yml)

示例部署步骤仅提示接入业务已有发布命令，需替换后使用。配置固定客户端 commit、可访问的 API 地址、精确 Dataset/Agent 版本、预先创建的策略及 CI 凭据；平台须有正常运行的 Worker 和生产入口鉴权。

不要用 `continue-on-error`、`allow_failure` 或忽略 shell 退出码来绕过 Gate。授权 Override 及审计仍依赖 Issue #6/#7，本期客户端不提供覆盖结论的开关。

CLI 在创建、并发恢复和轮询时校验完整 Agent/Dataset/Evaluator 版本与显式 Baseline 身份；ISO UTC Dataset 时间戳允许等价表示。终态必须同时确认 progress.evaluating 为 0，再通过 `POST /api/v1/experiment-launches/{id}/result-snapshots` 冻结当前已完成的结果后请求 Gate；evaluation-only 重试仍在运行时继续等待。冻结相同结果幂等，变化后的结果生成新的修订；显式 Snapshot/Gate ID 的历史读取不改写。若冻结时任务重新进入运行态，返回 UNKNOWN，不能回退到旧 PASS。控制面缺少进度字段或冻结接口也返回 UNKNOWN，需与本 PR 的 API 配套使用。GitHub 模板的 API Token 只注入 Evaluate 步骤，安装和 artifact Action 不接收该凭据。

GitLab 必须将 `ARGUS_API_TOKEN` 设置为 masked/protected CI Variable，并将 Environment scope 精确设为 `argus-gate`，不得使用默认的 `*`。`install_cli` 在不持有该 Token 的 Job 中安装固定客户端并上传包目录；只有声明 `argus-gate` Environment 的门禁 Job 获取 Token，并直接执行安装好的客户端。发布 Job 不声明此 Environment；安装与发布的 Token 缺失检查可阻断错误的全局配置。流水线运行于受保护分支/标签；Runner 必须支持 Environment-scoped Variables。
