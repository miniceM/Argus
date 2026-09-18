# Enterprise Agent Evaluation PoC

> **Langfuse + Remote Agent Eval Runner + Demo Agent v1/v2**
>
> 目标：证明企业可以把 Agent 评测做成平台能力，而不是把评测 SDK、Dataset Runner 和 Judge 逻辑侵入每个 Agent 应用。

## 1. PoC 能证明什么

这个 PoC 实现了完整的低侵入评测闭环：

```text
Langfuse Dataset
      │
      ▼
Eval Runner ── W3C traceparent ──► Demo Agent v1 / v2
      │                               │
      │       普通 JSON HTTP API      │
      ◄───────────────────────────────┘
      │
      ├─ deterministic evaluators
      ├─ Experiment / Trace / Score
      └─ Langfuse Compare UI
```

关键约束：

- Demo Agent **没有 Langfuse SDK**；
- Demo Agent **没有任何 Evaluation SDK**；
- Demo Agent 只暴露普通 `POST /invoke`；
- Dataset、版本执行、Evaluator、Experiment 都在平台侧；
- Eval Runner 使用 W3C Trace Context 向远端 Agent 传播当前评测 Trace；
- v1/v2 使用完全相同的数据集和评测规则，可以直接比较回归结果。

## 2. 目录结构

```text
.
├── TECHNICAL_DESIGN.md              # 正式技术设计文档
├── README.md
├── docker-compose.yml
├── .env.poc                         # 仅用于本地 PoC 的演示凭据
├── .env.example
├── Makefile
├── config/
│   └── agents.yaml                  # Agent Registry
├── data/
│   └── dataset.json                 # 6 条回归测试样本
├── services/
│   ├── demo-agent/
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── app.py                   # v1/v2 共用业务代码镜像
│   └── eval-runner/
│       ├── Dockerfile
│       ├── requirements.txt
│       └── app/
│           ├── main.py
│           ├── executor.py
│           ├── registry.py
│           ├── evaluators.py
│           ├── models.py
│           └── config.py
├── scripts/
│   ├── wait-http.sh
│   ├── bootstrap.sh
│   ├── run-demo.sh
│   └── validate.sh
└── tests/
    ├── test_demo_agent.py
    └── test_registry.py
```

## 3. 环境要求

- Docker Engine / Docker Desktop
- Docker Compose v2
- 建议至少 **8 GB** 可用内存；首次拉取镜像与初始化 Langfuse 需要一些时间
- `curl`
- 本机 Python 3（只用于脚本 JSON 格式化和可选静态验证）

Langfuse Web 暴露在 `http://localhost:3000`。

## 4. 一键启动

```bash
cp .env.poc .env
# PoC 可以直接使用 .env.poc；生产环境必须替换全部演示凭据

docker compose --env-file .env.poc up -d --build
```

或者：

```bash
make up
```

查看状态：

```bash
make ps
```

等待 Langfuse 首次初始化完成后：

```bash
make demo
```

脚本将执行：

1. 等待 Langfuse `/api/public/ready`；
2. 将 `data/dataset.json` 幂等写入 Langfuse Dataset；
3. 对 `banking-agent:v1` 执行一次 Experiment；
4. 对 `banking-agent:v2` 执行一次 Experiment；
5. 输出运行结果并提示进入 Langfuse UI 对比。

## 5. Langfuse 登录

仅 PoC：

- URL: `http://localhost:3000`
- 用户: `admin@example.com`
- 密码: `Poc-Admin-2026!`

这些凭据是通过 Langfuse v4 的 headless initialization 自动创建的。**禁止用于生产环境。**

## 6. 预期结果

6 条 Dataset 样本覆盖：

- 意图识别；
- Agent 工具选择；
- 敏感信息保护；
- 高风险场景升级人工；
- 普通账户查询；
- 卡片被盗冻结。

Demo Agent v1 故意包含 4 类缺陷：

- 交易场景调用错误工具；
- 泄露完整账号字段；
- 高风险交易未升级人工；
- 卡片被盗时没有调用冻结工具。

因此预期：

```text
Agent v1: overall_pass ≈ 2 / 6 = 33.3%
Agent v2: overall_pass = 6 / 6 = 100%
```

打开 Langfuse 后，可以查看每个 Dataset Item 对应的 Trace、远程 Agent HTTP Observation、各 Evaluator Score，并比较两次 Experiment。

## 7. 单独调用服务

查看平台注册的 Agent：

```bash
curl http://localhost:18080/agents
```

初始化 Dataset：

```bash
curl -X POST http://localhost:18080/admin/bootstrap
```

评测 v1：

```bash
curl -X POST http://localhost:18080/experiments/run \
  -H 'content-type: application/json' \
  -d '{
    "agent_id": "banking-agent",
    "agent_version": "v1",
    "dataset_name": "banking-agent-regression",
    "experiment_name": "banking-agent-v1",
    "max_concurrency": 4
  }'
```

评测 v2：

```bash
curl -X POST http://localhost:18080/experiments/run \
  -H 'content-type: application/json' \
  -d '{
    "agent_id": "banking-agent",
    "agent_version": "v2",
    "dataset_name": "banking-agent-regression",
    "experiment_name": "banking-agent-v2",
    "max_concurrency": 4
  }'
```

直接调用业务 Agent：

```bash
curl -X POST http://localhost:18082/invoke \
  -H 'content-type: application/json' \
  -d '{
    "messages": [{"role":"user","content":"发现一笔5万元陌生转账，请立即处理"}],
    "customer_id": "C10002"
  }'
```

## 8. 运行静态验证

```bash
make validate
```

验证脚本检查：

- Compose / Agent Registry / Dataset 配置可解析；
- 所有 Python 源码可编译；
- Demo Agent 中不存在 Langfuse / Evaluation Runner 依赖；
- Runner 包含 W3C `traceparent` 注入逻辑；
- Demo Agent v1/v2 业务行为测试；
- 如果本机安装 Docker，则额外执行 `docker compose config -q`。

## 9. 当前实现边界

PoC 为了把核心理念做清楚，当前只实现：

- `SYNC_HTTP` Agent 调用；
- JSON-in / JSON-out；
- 平台侧 deterministic evaluator；
- Runner 内存级 rate limiter；
- 简单 retry/backoff；
- Langfuse SDK Experiment；
- W3C Trace Context 注入。

正式生产版本需要增加：

- Agent Registry 持久化与管理 UI；
- `ASYNC_POLL / CALLBACK / SSE`；
- Vault / KMS / Secret Manager；
- mTLS / Service Mesh；
- Redis/DB 驱动的分布式任务队列；
- ExperimentLaunch / ExecutionAttempt 持久化状态机；
- Trace Assembler + Trajectory Evaluator；
- Release Gate；
- RBAC、审计、数据脱敏与 Dataset 审批流程。

详见 [TECHNICAL_DESIGN.md](./TECHNICAL_DESIGN.md)。

## 10. 本材料的验证状态

生成本 PoC 的执行环境**未安装 Docker / Docker Compose**，因此无法在该环境中实际拉起 Langfuse 容器栈。已完成：

- 配置文件静态解析；
- Python 编译检查；
- Demo Agent v1/v2 本地行为测试；
- Agent Registry 请求映射测试；
- 代码级零评测 SDK 检查；
- 依据 2026-09-16 Langfuse v4 官方文档校准镜像、headless init、Experiment Runner 和 OpenTelemetry API。

拿到包后，在安装 Docker Compose 的环境执行 `make validate && make up && make demo` 即可完成端到端验证。

## 11. 当前 Langfuse 兼容基线

本 PoC 按以下 2026-09-16 官方能力设计：

- Langfuse self-host: v4 Docker Compose
- Langfuse images: `docker.langfuse.com/langfuse/langfuse:4` / `langfuse-worker:4`
- ClickHouse: `25.12`
- Python SDK: `langfuse==4.15.3`
- Python SDK v4 / Observation-first / OpenTelemetry
- Dataset `run_experiment()` + item/run evaluators
- Headless Initialization (`LANGFUSE_INIT_*`)

建议真正落地时将镜像从 major tag 改为企业验证过的**精确版本或 digest**，并将 SDK 升级纳入兼容性测试。
