# Flowise Agentflow V2 原生 HTTP 联调（Issue #213）

本示例不增加 Adapter，不修改 Executor/Worker，不为被测 Agent 安装 Argus 或 Langfuse SDK。
Argus 直接调用 Flowise 原生 `POST /api/v1/prediction/{flowId}`，保留原生 `text`、工具记录和错误响应；不会改写为 `answer`。
本项只验收 HTTP 执行，不实现 #214～#219 的托管评分采纳。

## 环境与隔离

需要 Docker Compose、Python 3.12+、真实 OpenAI-compatible 模型凭证、独立 Langfuse 测试项目凭证。
Flowise 固定为 3.1.2 的镜像 digest（见 compose.yml），使用独立 PostgreSQL；SQLite 在本次镜像中无法可靠启动。
为允许原生 requestsGet 请求 Docker 内的受控工具，专用测试 Flowise 设置 `HTTP_SECURITY_CHECK=false`；默认 SSRF 策略会拒绝该私网地址（已实际观察）。这降低了此测试实例的出站防护，禁止在共享／生产环境照搬；没有修改 Flowise 源码。
所有宿主端口仅监听 loopback，数据库不暴露宿主端口。PostgreSQL trust 仅适用于此隔离的本地测试网络，不用于生产。
默认端口：Argus 19090、Flowise 19091、Console 19093、业务工具 19094。

```sh
cp examples/flowise/.env.example .env.flowise
# 编辑 .env.flowise，填写真实凭证和模型 ID；不要提交。
# ARGUS_BUILD_ID 必须为当前 git rev-parse HEAD 的完整 SHA。
mkdir -p examples/flowise/.runtime
(umask 077; touch examples/flowise/.runtime/runner.env)
docker compose --env-file .env.flowise -f examples/flowise/compose.yml up -d postgres flowise evidence-tool
# 等待 Flowise /api/v1/ping 返回 pong。
python examples/flowise/e2e.py setup --env-file .env.flowise --register-owner
docker compose --env-file .env.flowise -f examples/flowise/compose.yml up -d --build eval-runner console
python examples/flowise/e2e.py run --env-file .env.flowise
```

`setup` 仅用于新建专用本地实例：创建 owner、原生模型 Credential、Prediction API Key 和两个原生 Agentflow（正常／模型不可达）。不要针对共享生产 Flowise 使用 `--register-owner`。
若已有 `.runtime/resources.json`，setup 拒绝重复创建；部分失败时先查看 Flowise 资源，不要盲目重跑。
模型 key 仅写入 Flowise Credential，Argus 仅获得 Flowise API token；运行时凭证保存在忽略目录中，文件权限 600。
OpenCode Go 网关需要 `MODEL_EXTRA_HEADERS={"x-opencode-session":"argus-issue213"}`；这只是原生模型连接配置，不是 Argus 协议扩展。

## 配置与执行契约

- `agentflow.json` 是原生 Start → Agent 配置，包含 Calculator 与 `requestsGet` 工具，Memory 关闭。
- `evidence_tool.py` 是独立业务 HTTP 工具，不是 Prediction 代理或模型 mock。它为实际请求生成随机 receipt，并在服务端保存请求记录。
- AgentVersion 使用 HTTP_JSON、POST、`max_retries=0`、`is_idempotent=false`；业务输入通过既有 mapping 传入 question、streaming=false、逐 Item 唯一 sessionId。
- `dataset.json` 有 5 个逻辑用例：普通问答、工具、结构化、鉴权错误、超时；模型不可达复用最后一个逻辑用例，在单独 Launch 执行。
- Ground Truth 只写入 Langfuse Dataset expectedOutput，不传给 Flowise。
- 正常／鉴权／超时／不可达分别使用独立 Dataset、Version、Launch，避免把单个 Version 的凭证和 timeout 设置混为用例输入。
- 原生 `text` 不满足现有 builtin `answer_present` 时，质量可为 fail；HTTP 200 的 execution_status 仍须为 succeeded。不能以质量 fail 冒充执行异常，也不能为使质量通过改写业务响应。
- `max_retries=0` 约束 Argus HTTP Attempt，不证明模型 SDK 内部没有重试；原生 Flowise 记录用于单独核验预测调用次数。

## 检查结果与 Console

每次 run 写入 `.runtime/i213-*/`：创建回执、Launch/Item、Attempt、Snapshot summary、comparison/case 原始输出、Flowise 原生 execution 列表和独立工具日志。
应核对工具 receipt 同时存在于业务工具日志与对应 Item 的原生输出；检查第二个 Item 的原生模型输入没有第一个 Item 的 marker，不能只凭模型声称“无历史”断言隔离。
打开 `http://localhost:19093`，按输出中的 Launch ID 查看执行状态、Attempt、输入与 Case 对比详情。执行故障必须有明确 401/403、TIMEOUT 或模型不可达 HTTP 500 证据。
脚本收集成功不自动等于全部验收通过，仍需核验 Console、Session、调用次数和回归记录。

## 排障与清理

- 401/403：检查原生 Flowise API Key 与 chatflow.apikeyid 绑定；不要为了通过关闭鉴权。
- 500：查看 Flowise 日志确认模型连接配置；不要把模型错误伪装为 200。
- Timeout：Argus 超时不会自动取消 Flowise 已开始的任务；等其稳定后核对原生 execution，不要立即重发。
- 缺工具记录：检查 Flowise 的原生工具配置／参数，不以最终算式答案代替工具证据。
- 缺 Case 输出：核验 Runner build_id、Snapshot 和真实 Observation；不要改读别的旧实例。
- `docker compose --env-file .env.flowise -f examples/flowise/compose.yml down` 只停止本示例，不加 `-v` 以保留证据。删除测试卷、Langfuse Dataset 和本地凭证应由操作者另行明确决定。

自动化配置测试：`python -m pytest -q tests/test_flowise_e2e.py`。这些测试与真实付费 E2E 分开记录。

## 恢复收集与现有服务回归

已提交的 Launch 若因收集器中断未出报告，使用 `run --resume i213-实际命名空间` 恢复；不会对已经记录的 Launch 再次发起 `/run`。若中断恰好发生在写入 created 回执和 `/run` 之间，需人工核验 Launch 状态，脚本不会冒险自动重发。

```sh
docker compose --env-file .env.flowise -f examples/flowise/compose.yml --profile regression up -d --build demo-agent-v1 demo-agent-v2 real-llm-agent
python examples/flowise/regression.py --env-file .env.flowise --real-llm
```

以上回归分别使用现有 Demo 的 `/experiments/run` 路径和 #198 的六条合成 Item Launch；`--real-llm` 显式开启六次可能计费的模型调用。不加该参数只跑 Demo v1/v2。回归不使用旧的宿主 18080 实例。

### 本次版本的真实错误语义

- Flowise 3.1.2 在 Prediction 内部拒绝无效 token 时，其原生服务日志为 `Error: Unauthorized`，但对外包装为 **HTTP 500**，不是 401/403。Argus 如实记录 HTTP_5XX / execution failed；需同时核对原生 execution 中不存在该 auth session。不得改 HTTP 状态或称作已验证 401。
- 一秒 read timeout 对非幂等 Prediction 记录为 **AMBIGUOUS_OUTCOME / MAY_HAVE_BEEN_SENT**（http_status=null），不是确定未执行。Flowise 的任务可继续完成；Argus 不自动重发，需核对原生 session 仅一条执行。
