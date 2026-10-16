# Real LLM Agent（Issue #198 可选服务）

> 这是一个可选的隔离服务：把 OpenAI 兼容模型网关包成普通业务 `/invoke`，
> 用于验证 **真实模型响应 + `answer_present@1.0.0` 冻结评分** 的完整执行链路。
> 它不是银行业务 Agent：评分只表示"是否返回了非空回答"，不是业务准确率。
> 不启动本服务时，`make up` 默认栈完全不受影响。

## 1. 配置三项

复制样例到已忽略的 `.env.local` 后填写，只需要以下三项：

```bash
# 仅当 .env.local 不存在时复制；已存在时不要覆盖，只在编辑器里追加这三行。
cp .env.real-llm.example .env.local
```

| 变量 | 填写规则 |
|---|---|
| `MODEL_BASE_URL` | OpenAI 兼容网关的 API 前缀，例如 `https://model-gateway.example.invalid/v1`；不能是 `/chat/completions` 完整路径 |
| `MODEL_API_KEY` | 网关 API Key。只注入本服务容器环境；禁止写入仓库任何文件、日志、验收记录或对话 |
| `MODEL_NAME` | 网关实际支持的固定 model ID；建议选择一个可用的模型快照，不要写 `latest` |

模型名确定后不要在 v1 生命周期内悄悄替换：模型 / base URL / 镜像 / 提示策略发生
变化时必须创建新的 AgentVersion（见第 4 节）。兼容网关若不承诺模型快照不可变，
请在验收记录里如实说明"使用网关当前 serving 配置"，并记录 Agent 实际返回的
`model` 字段（上游真实值，不是把配置名冒充为实际值）。

## 2. 启动默认栈（不包含新服务）

```bash
make up
./scripts/bootstrap.sh
```

上述两条命令只启动默认栈；没有任何 MODEL 变量时原环境保持完全一致，
`make demo` 基线依旧为 v1=2/6、v2=6/6。运行 bootstrap 前先确认你连接的
Langfuse（`http://localhost:3000`）是本地测试项目，不是公司正式共享项目。

## 3. 启动新服务并验证 health

保持与 `make up` 相同的 Compose project：

```bash
docker compose --env-file .env.poc --env-file .env.local \
  -f docker-compose.yml -f docker-compose.real-llm.yml \
  up -d --build --no-deps real-llm-agent

curl -fsS http://127.0.0.1:18084/health
# 期望：{"status":"ok"}
```

`/health` 只证明本服务进程存活，不探测上游模型服务，也不产生任何模型费用。

**会话头说明**：本服务每次 `/invoke` 都会随 Chat Completions 请求发送
`x-opencode-session` 供网关做路由/缓存提示（OpenCode Go 等网关缺它会直接返回
4xx `MissingSessionID`）。取值只来自传入 `traceparent` 的 W3C trace-id，缺省时
生成本次会话专用随机 hex；不读 `customer_id` 或消息内容，不写日志，也不外传
业务字段。网关若忽略该 header 无副作用。

## 4. 注册 AgentDefinition 与不可变 AgentVersion

```bash
curl -fsS http://localhost:18080/api/v1/agents \
  -H 'Content-Type: application/json' -d '{
    "id":"real-llm-agent",
    "name":"Real LLM Agent",
    "description":"仅验证真实模型响应及 answer_present，不代表银行业务质量"
  }'

curl -fsS http://localhost:18080/api/v1/agent-versions \
  -H 'Content-Type: application/json' -d '{
    "agent_id":"real-llm-agent",
    "version":"v1",
    "endpoint":"http://real-llm-agent:8080/invoke",
    "protocol":"HTTP_JSON",
    "method":"POST",
    "request_mapping":{
      "messages":"input.messages",
      "customer_id":"input.customer_id"
    },
    "timeout_seconds":75,
    "max_retries":0,
    "max_concurrency":1,
    "rate_limit_per_minute":60,
    "is_idempotent":false,
    "environment":"poc",
    "usage_cost_mapping":{
      "input_tokens_path":"usage.input_tokens",
      "output_tokens_path":"usage.output_tokens",
      "total_tokens_path":"usage.total_tokens",
      "source":"provider_reported",
      "measurement_scope":"agent_invocation_total"
    }
  }'
```

注册是幂等快照：同一 `id + version` 重复提交会得到 **409 冲突**。先读取并核对
现有配置再决定是否继续运行；不要忽略冲突，也不要自动覆盖。`usage_cost_mapping`
只提取 Provider 上报的 token usage；本服务不记录金额证据，因此不配置
`amount_path` / `currency_path`，费用保持 unknown。

## 5. 创建并运行 Launch

```bash
REQUEST_KEY="issue198-$(python3 -c 'import uuid; print(uuid.uuid4())')"
LAUNCH_JSON=$(curl -fsS http://localhost:18080/api/v1/experiment-launches \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $REQUEST_KEY" -d '{
    "name":"issue198-real-llm-smoke",
    "agent_id":"real-llm-agent",
    "agent_version":"v1",
    "dataset_name":"banking-agent-regression",
    "environment":"poc",
    "max_concurrency":1,
    "evaluator_selections":[{"id":"answer_present","version":"1.0.0"}],
    "quality_policy":{
      "rules":[
        {
          "evaluator_id":"answer_present",
          "operator":"==",
          "expected_value":true,
          "result_type":"boolean",
          "required":true
        }
      ]
    }
  }')
LAUNCH_ID=$(printf '%s' "$LAUNCH_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

# boolean 指标必须显式声明 `operator=="=="; expected_value=true`；
# 缺省策略对 boolean 会生成 operator=None，被服务器以 400 拒绝而不是静默放行。
# 先核对 Manifest：确认 Dataset/version、Agent/Version、Evaluator/version 与 Runner 四类身份已冻结。
curl -fsS "http://localhost:18080/api/v1/experiment-launches/$LAUNCH_ID"
```

⚠️ **运行前提醒**：下一条命令会向上游模型发送 `data/dataset.json` 的六条合成
Dataset Item 并可能产生真实计费。只使用本仓库合成数据与目标测试项目；不要把真实
客户数据或生产数据外发给该模型。确认费用与项目写入权限后，再执行启动：

```bash
curl -fsS -X POST "http://localhost:18080/api/v1/experiment-launches/$LAUNCH_ID/run"
```

当前 v1 的执行策略是 `max_retries=0` 且 `max_concurrency=1`：每条 Item 只调用
一次模型，没有自动重试。失败样例如需重新评估请新建 Launch；同一次逻辑重试需沿用
同一个请求 key，新的一次付费运行使用新的 key，不要自动触发。

## 6. 查看结果与观测

```bash
curl -fsS "http://localhost:18080/api/v1/experiment-launches/$LAUNCH_ID"
curl -fsS "http://localhost:18080/api/v1/experiment-launches/$LAUNCH_ID/items?limit=100"
# 从实际返回中取 item ID（不猜测），然后：
curl -fsS "http://localhost:18080/api/v1/experiment-item-executions/{item_execution_id}/attempts"
curl -fsS "http://localhost:18080/api/v1/experiment-launches/$LAUNCH_ID/summary"
curl -fsS "http://localhost:18080/api/v1/experiment-launches/$LAUNCH_ID/result-snapshots"
```

- **逐项结果**：每条 Item 有 Attempt 与终态；Item 失败对应该 Attempt 的
  `http_status` 和安全 `error_message`，不会被当作"业务评分失败"。
- **Agent 响应**：`answer` / `model` / `usage`（`input_tokens` / `output_tokens` /
  `total_tokens`，某项不可用时为 `null`）。与 Attempt 的 `usage_cost` 一起核对，
  必要时叠加脱敏网关记录或 Provider request ID；两者都无法核查时，真实性验收未完成。
- **评分**：本次只冻结 `answer_present@1.0.0`（boolean），成功 Item 为 true；
  失败 Item 无假 PASS。不得把该结果称为"业务准确率 100%"。
- **Console**：`http://localhost:18083` 同一 Launch 内查看逐项输出与评分。
- **Langfuse**：在 `http://localhost:3000` 打开同一 Launch 对应的 Experiment，
  检查 Trace / Score 是否真实落地并同 Launch/Item 对应；只看 Launch 结束不算，
  后台同步可能尚未完成，请用有界等待核实。
- 完整验收核对项参见计划 §8.2 的 AC1–AC10。

## 7. 故障处理

Agent 错误 body 统一为受控内容 `{"error":{"code":…,"message":…}}`，不透传
Provider 原文或完整异常。日志只记录 code、HTTP status、耗时与已清洗 request ID；
不记录 Key、请求体或上游响应原文。

| 上游故障 | Agent HTTP / code | Argus 侧表现 | 处理提示 |
|---|---|---|---|
| 401 | 401 `MODEL_AUTH_FAILED` | `HTTP_4XX`、Item failed | 这是**模型网关鉴权**失败，不是 Argus AgentVersion Credential 问题；核对 `.env.local` Key 并重启本服务 |
| 403 | 403 `MODEL_PERMISSION_DENIED` | `HTTP_4XX` | Key 对所选模型 / 项目 / 组织无访问权限 |
| 429 | 429 `MODEL_RATE_LIMITED` | HTTP 429 | 网关限流；降低并发或换用其他项目 |
| 连接失败 / DNS / 连接拒绝 | 502 `MODEL_UNREACHABLE` | `HTTP_5XX` | 核对 `MODEL_BASE_URL` 是否 API 前缀而非完整路径；检查网关可达性 |
| 其他上游错误 | 502 `MODEL_UPSTREAM_ERROR` | `HTTP_5XX` | 查看网关侧原因后再调整，不改变 v1 配置 |
| SDK 超时 / 总 deadline | 504 `MODEL_TIMEOUT` | `HTTP_5XX` | Agent 的 HTTP 504 超时，不是 Runner 的 `READ_TIMEOUT`；考虑缩短输入或更换模型 |

模型 / base URL / 镜像 / 提示策略发生变化时，永远创建新的 AgentVersion，不要在
旧的 v1 上复用。Console 中该 Agent 出现的错误 code 只能用于排查提示，不要求
Console 展示结构化 Agent error 字段。

## 8. 本地自动化验收样例（无真实模型）

```bash
.venv/bin/python -m pytest -q \
  tests/test_real_llm_agent.py tests/test_real_llm_agent_integration.py \
  tests/test_real_llm_deployment.py \
  tests/test_expected_pass_rate.py tests/test_executor_single_invoke.py \
  tests/test_cost_evidence.py tests/test_registry.py tests/test_dataset_resolver.py \
  tests/test_issue_80_evaluator_versioning.py tests/test_issue_82_typed_results.py

.venv/bin/python -m ruff check services tests
make validate

env -u MODEL_BASE_URL -u MODEL_API_KEY -u MODEL_NAME \
  docker compose --env-file .env.poc -f docker-compose.yml config -q

MODEL_BASE_URL=https://model.invalid/v1 MODEL_API_KEY=not-a-real-key MODEL_NAME=test-model \
  docker compose --env-file .env.poc \
  -f docker-compose.yml -f docker-compose.real-llm.yml config -q

docker build -t argus-real-llm-agent:issue198 services/real-llm-agent
```

这些命令通过只证明自动化验证已交付，**不**证明真实模型 E2E 已完成；请勿在
README 中提前打勾"真实 E2E PASS"。

## 9. Cloud 栈（简述，可选）

远端 / Langfuse Cloud 模式同样可以接入 real-llm-agent：保留当前 main 的 Langfuse
数据源修复、非 dev 的 build identity 和既有 `env_file` 配置，再叠加
`docker-compose.real-llm.yml` 并注入三项配置。本地全栈路径仍是首选验证方式；
Cloud 栈不额外修改 Runner 或编排源码。
