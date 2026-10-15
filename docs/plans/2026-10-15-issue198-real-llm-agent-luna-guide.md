# Issue #198：真实 LLM 本地轻量 Agent — Luna 实施方案

> 日期：2026-10-15（Asia/Shanghai）  
> Issue：https://github.com/miniceM/Argus/issues/198  
> 交付范围：本轮仅分析与方案落盘；未实现业务代码、创建本地提交、推送、修改 Issue 或启动付费调用。Luna 获得后续实施授权后按本方案执行。  
> 分析根目录（下称 ROOT）：`/Users/hosea/.codex/worktrees/1a39/Argus`。

## 1. 问题结论

### 1.1 这是缺少真实调用样例，不是 Remote Runner 缺少协议

现有 Demo 的 v1/v2 规则输出可以保护调度、HTTP、评分与同步，却不能证明 Agent 实际访问了模型。推荐独立增加 `services/real-llm-agent/`，只实现普通的 `POST /invoke → OpenAI 兼容 Chat Completions → answer`，通过现有 Registry、Launch、Item、Attempt 与 Langfuse 同步链路完成验证。

**不改 Demo、不新增 Evaluator、不改 Runner 协议、不建 Agent Framework，不把内部 Generation/Tool Span 纳入本次验收。**

验收分成两个不可替代的层次：

1. 模拟 HTTP 上游自动化：证明真实 SDK 调用路径、输入输出、错误、费用未知语义及 Argus 集成正确；不花费真实模型额度。
2. 一次显式授权的真实模型 E2E：证明实际 Dataset Items 经模型服务处理，Argus/Console/Langfuse 均有对应结果。只有第一层不能关闭 Issue。

### 1.2 本轮证据身份

| 证据 | 已核验结果 |
|---|---|
| Issue | OPEN；2026-10-15 创建/更新；读取时 0 条评论 |
| 本地 checkout | detached HEAD，`73e77172f12d60ea8d5c16704d568dc115f2346f`；开始分析时工作区干净 |
| 远端 main | GitHub API 读取为 `22c8c1364cd626bb100e5477f36de1a6944c6dcc`；直接父提交即上述本地 HEAD |
| main 新增差异 | PR #199：Cloud 数据集默认源改为 `langfuse`，以及 Console 审计文案；没有新增 Real LLM Agent |
| 当前新增功能 | 尚未实现；以下新增文件、响应格式、错误码和提交点均为拟实施内容 |
| 测试环境 | 使用已有 `/Users/hosea/work/ai-eval/Argus/.venv/bin/python`，Python 3.12.4，测试加载的是本 checkout 源码；本 checkout 无 `.venv`，该共享环境未安装 `openai` |

远端状态可能继续变化。Luna 开始前必须重新读取 Issue、main、目标分支和工作区，不能将本方案中的 SHA 当作永久最新值。本轮未 fetch、checkout 或 rebase。

## 2. 当前实现与根因

### 2.1 现有可复用实现

以下为当前 checkout 已定位并读取的代码，行号只用于本次定位；实施时以符号与当前源码为准。

| 位置 | 当前事实与本次关系 |
|---|---|
| `/Users/hosea/.codex/worktrees/1a39/Argus/services/demo-agent/app.py`：`AgentRequest`、`AgentResponse`、`invoke()` | 请求为非空 `messages` 与可选 `customer_id`；`invoke()` 返回规则结果。仅复用 HTTP 形态，不复用要求 `intent/tool_calls` 的 Demo 响应模型 |
| `/Users/hosea/.codex/worktrees/1a39/Argus/services/eval-runner/app/models.py`：`AgentVersionSpecValidator`、`AgentVersionCreateRequest` | 已有 `HTTP_JSON`、POST、映射、1–600 秒 timeout、重试、并发、环境、artifact、usage 映射；无需加 API 字段 |
| `/Users/hosea/.codex/worktrees/1a39/Argus/services/eval-runner/app/api_registry.py`：`create_agent()`、`create_agent_version()` | 实际创建接口是 `POST /api/v1/agents` 和 `POST /api/v1/agent-versions`，不是嵌套的 `/agents/{id}/versions` |
| `/Users/hosea/.codex/worktrees/1a39/Argus/services/eval-runner/app/api_launches.py`：`create_experiment_launch()`、`run_launch_async()` | 创建与运行是两个操作；创建不等于执行。支持固定 `evaluator_selections` 与 `Idempotency-Key` |
| `/Users/hosea/.codex/worktrees/1a39/Argus/services/eval-runner/app/evaluators.py`：`answer_present()`（111–120）、`_register_builtin_evaluators()` | `answer_present@1.0.0` 是已有 item-scope boolean；只检查 `answer` 是非空白字符串，`default_selected=False`，所以必须显式选择 |
| `/Users/hosea/.codex/worktrees/1a39/Argus/services/eval-runner/app/executor.py`：`RemoteAgentExecutor.invoke_once()`（158–314） | 429→`HTTP_429`；其他 4xx→`HTTP_4XX`；5xx→`HTTP_5XX`；记录 HTTP status，但并不把 Agent 的结构化错误 JSON 自动翻译为新的领域错误类型 |
| `/Users/hosea/.codex/worktrees/1a39/Argus/services/eval-runner/app/worker.py`：`ExecutionWorker.execute_item_message()` | 仅 `READ_TIMEOUT` 分类进入 `TIMED_OUT`；Agent 返回 504 属于 `HTTP_5XX`，Item 为 `failed`，evaluation 跳过、quality unknown。不要据此扩改状态机 |
| `/Users/hosea/.codex/worktrees/1a39/Argus/services/eval-runner/app/costs.py`：`extract_usage_cost()` | 读取显式 dot path，Usage 缺失不当成零；不从 token 推算金额。若 input/output 都存在而 total 缺失，现有提取层会相加得到 total；Agent 本身仍应保留 Provider 原始缺失值 |
| `/Users/hosea/.codex/worktrees/1a39/Argus/services/eval-runner/app/dataset.py`：`DatasetResolver.__init__()` | 非 test 模式未显式配置时走 Langfuse；test 模式走 seed。不能用测试模式的 seed 成功替代真实 Langfuse 验收 |
| `/Users/hosea/.codex/worktrees/1a39/Argus/services/eval-runner/app/main.py`：`bootstrap()`（319–352） | `POST /admin/bootstrap` 使用现有六条 seed 向 Langfuse upsert Dataset Items，可复用；这是数据写操作，执行前确认目标为测试项目 |
| `/Users/hosea/.codex/worktrees/1a39/Argus/config/agents.yaml`、`data/dataset.json` | 保持默认 banking-agent v1/v2 与六条 seed 不变；新 Agent 用 Registry API 创建，不修改默认 YAML |
| `/Users/hosea/.codex/worktrees/1a39/Argus/scripts/validate.sh`、`.github/workflows/ci.yml` | 零评测 SDK 检查当前只覆盖 Demo；增加独立样例后要补覆盖。CI 与本地校验仍然不需要模型 Key |

### 2.2 真正需要补的缺口

- 缺独立的 LLM HTTP 适配样例和可选 Compose 服务。
- 缺一个明确使用 `answer_present@1.0.0` 的注册/执行步骤；沿用默认银行业务 Evaluator 会产生不正确的验收期待。
- 缺真实响应、Usage unknown、失败错误以及真实请求证据的约定。
- 缺普通 CI 无 Key、无真实网络也可跑的服务测试。

### 2.3 实施时必须预防的设计陷阱

1. **双层重试**：官方 SDK 默认有重试，单独设 AgentVersion `max_retries=0` 不够。两层均显式置 0，首版不做手工 retry。
2. **超时层级**：SDK 的 connect/read 等 timeout 不是整次业务调用的绝对截止时间；额外用异步总 deadline，且 Runner timeout 要大于 Agent deadline。
3. **失败不可区分**：如果所有上游问题都包装成 200 或同一个 500，Argus 无法通过现有 HTTP status 区分验收要求的三类失败。
4. **冻结配置与运行进程漂移**：同一个不可变 AgentVersion 背后的模型服务配置仍可被重启改变；必须记录模型配置指纹/制品身份并按配置变化创建新 version，不承诺云端可变模型别名完全可重现。
5. **凭据误注入**：模型 Key 归 Agent→模型，不是 Runner→Agent 的 Endpoint Credential；不得放入 AgentVersion、Manifest 或 Console 表单。

## 3. 目标行为

1. 默认 `make up`、`make demo` 不需要任何模型变量；旧 v1/v2 继续 2/6 与 6/6。
2. 三个必要变量 `MODEL_BASE_URL`、`MODEL_API_KEY`、`MODEL_NAME` 足以启动新服务；缺配置快速、安全地失败，但不影响默认栈。
3. `GET /health` 仅报告本地配置加载与进程可用，绝不调用模型，不宣称远端鉴权或模型连通性。
4. 每次有效 `/invoke` 调用最多向兼容上游发送一次非流式 Chat Completions 请求；错误、超时或空白答案不回退假回答。
5. 正常返回真实 `answer`；实际响应的 model、usage 可缺失，缺失是 null，不伪造为配置值或 token 0。
6. Argus 以服务 DNS 调用；固定只选 `answer_present@1.0.0`。成功的 boolean 结果只表达“返回了回答”，不表达业务准确率、工具正确率或银行操作完成。
7. 模型鉴权、网关不可达、模型超时均是执行失败，通过 401、502、504 和安全诊断可区分；不要求给 Runner 新增领域错误码。
8. 模拟上游测试为普通 CI 必选；真实调用为显式付费、可选运行路径，但真实 E2E 记录是 Issue 完成的必要证据。

## 4. 推荐解决方案

### 4.1 变更清单

以下文件均位于 ROOT；“新增”指当前尚不存在的计划交付物。

| 文件 | 动作与边界 |
|---|---|
| `/Users/hosea/.codex/worktrees/1a39/Argus/services/real-llm-agent/app.py` | 新增：请求/响应模型、`create_app()`、lifespan 内 SDK client、`/health`、`/invoke`、安全异常处理 |
| `/Users/hosea/.codex/worktrees/1a39/Argus/services/real-llm-agent/requirements.txt` | 新增：与当前 FastAPI/Pydantic/httpx 范围对齐，加入固定 OpenAI SDK 版本 |
| `/Users/hosea/.codex/worktrees/1a39/Argus/services/real-llm-agent/Dockerfile` | 新增：独立构建，仅 COPY 本服务文件，不把根目录或环境文件复制进镜像 |
| `/Users/hosea/.codex/worktrees/1a39/Argus/services/real-llm-agent/README.md` | 新增：短路径说明、可复制 API 示例、验收证据和常见错误 |
| `/Users/hosea/.codex/worktrees/1a39/Argus/docker-compose.real-llm.yml` | 新增：只增加 real-llm-agent，不覆盖 eval-runner 环境、默认 Demo 或 Compose project name |
| `/Users/hosea/.codex/worktrees/1a39/Argus/.env.real-llm.example` | 新增：仅三个必要模型参数的占位值，不放真实 Key；实际使用已经被忽略的 `.env.local` |
| `/Users/hosea/.codex/worktrees/1a39/Argus/tests/test_real_llm_agent.py` | 新增：Agent + 真实 SDK + 模拟 HTTP transport 回归 |
| `/Users/hosea/.codex/worktrees/1a39/Argus/tests/test_real_llm_agent_integration.py` | 新增：映射→Agent→模拟上游→Executor/Worker→现有评分/持久化边界 |
| `/Users/hosea/.codex/worktrees/1a39/Argus/tests/test_real_llm_deployment.py` | 新增：Compose、环境隔离、架构依赖及示例请求的静态契约测试 |
| `/Users/hosea/.codex/worktrees/1a39/Argus/requirements-dev.txt` | 修改：`-r services/real-llm-agent/requirements.txt`，确保普通测试环境有 SDK |
| `/Users/hosea/.codex/worktrees/1a39/Argus/scripts/validate.sh` | 最小修改：新 override 的配置/依赖边界纳入现有校验，不新增付费门禁 |
| `/Users/hosea/.codex/worktrees/1a39/Argus/.github/workflows/ci.yml` | 最小修改：aggregate pip cache 输入、零 SDK 检查、新服务构建/Compose 静态验证；绝不添加模型 secret |
| `/Users/hosea/.codex/worktrees/1a39/Argus/.github/dependabot.yml` | 最小修改：新增服务 Docker 入口；pip 由根 aggregate 的 `-r` 覆盖，不再另加重复服务 pip 入口；相关“两服务”注释更新为三服务 |
| `/Users/hosea/.codex/worktrees/1a39/Argus/README.md` | 只新增一条 Real LLM 样例文档入口，完整操作不复制到入口文档 |
| `/Users/hosea/.codex/worktrees/1a39/Argus/docs/validation/2026-10-15-issue198-real-llm-e2e.md` | 后续真实验收时新增；实际执行不是 10 月 15 日则使用执行日，重名追加后缀，不用模板冒充实测 |

**默认保持不变**：Eval Runner 生产源码、数据库迁移、Console 组件、OpenAPI/TypeScript 合约、Demo、seed、默认 agent YAML、轨迹/门禁与远端 Ruleset/Environment。

### 4.2 SDK 版本与文档依据

建议首版使用已核对官方 tag 的 `openai==2.11.0` 作为明确、可复现的实现基线，**不是宣称它是当前最新版**。该版本 README 支持 AsyncOpenAI、httpx、`base_url`、显式 timeout 与 `max_retries=0`。Luna 在干净 Python 3.12 环境安装并执行 `pip check`；若现有依赖或安全策略要求更换，先核对新版本官方文档并同步测试，不盲目采用浮动 `latest`。

本轮按仓库要求执行 Context7 library 解析及 docs 查询；即使指定 v2.11.0，部分返回片段仍引用 main 的 httpx2 代码，故**不能照抄这些 main 片段来实现固定版本**。已直接读取官方 `v2.11.0/README.md` 复核使用 httpx 的配置与重试/timeout。没有因此引入 httpx2，也不为消除现有测试弃用警告做无关依赖升级。

参考：
- `https://github.com/openai/openai-python/blob/v2.11.0/README.md`（Async usage / Retries / Timeouts / Configuring the HTTP client）
- `https://github.com/openai/openai-python/blob/v2.11.0/api.md`（Chat Completions）
- `https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create`（请求与响应字段；不将平台特有新参数强加企业兼容网关）

本服务使用 Chat Completions 是兼容网关和 Issue 的明确要求，不切换到 Responses API，不固定特定厂商 model 名称。

## 5. 详细实施步骤

### S0：执行前身份与依赖核对（不单独提交）

- 阅读 Issue 最新正文/评论、本地 AGENTS 和贡献约定。
- 核对目标分支、HEAD、工作区；当前为 detached，不得直接在不明分支上交付。获得实施授权后按仓库流程选择/创建 `codex/issue198-real-llm-agent`，不要擅自覆盖已有同名分支。
- 核对 PR #199 或更新主线；保留 Cloud 默认源修复，不从当前旧 checkout 复制旧 cloud 文件覆盖主线。
- 在本 checkout 建立独立 `.venv`（不修改本轮使用的共享环境），安装 aggregate 开发依赖；确认 Python 3.12、Node 22、pnpm、Docker/Compose 可用。
- 固化本 Issue 验收项与现有失败基线。配置、契约或测试与本方案不一致时先刷新对应部分，不扩大为全仓重构。

### S1：TDD 实现独立 Agent（原子提交 1）

**先 RED**：新增 `test_real_llm_agent.py` 的至少成功、usage 缺失、无效 Key、不可达、timeout、空答案、SDK 不重试和日志脱敏测试。用独立模块名通过 `importlib.util.spec_from_file_location()` 加载服务文件，避免把 Runner 的 `app` 包与样例的 `app.py` 混淆。服务未实现时实际运行并记录失败；不能只写“预期 RED”。

**最小 GREEN**：

- `create_app(settings=..., client_factory=...)` 是拟新增测试接缝；配置从环境装载，生产 client 在 lifespan 构造/关闭，避免 import 时依赖真实 Key，也不在 import/启动/health 发远程请求。
- 启动时检查三个必要变量非空；URL 只允许 http/https，必须有 hostname，禁止 username/password、query 和 fragment，错误不回显原值。支持可信企业内部 HTTP，但对公网推荐 HTTPS，不允许 `verify=False`。
- 输入：`messages` 非空，message content 非空白；首版 role 限 `system/user/assistant`，不暴露 tool/image/function 参数；`customer_id` 可接收但不传给模型，避免无必要地传用户标识。请求中不接受模型 Key 或 endpoint override。
- 使用 `AsyncOpenAI(api_key=..., base_url=..., max_retries=0, timeout=...)`，只调用 `await client.chat.completions.create(model=MODEL_NAME, messages=..., stream=False)`。不自动注入 temperature、response_format、tools、固定 token 上限等可能不被网关支持的参数。
- 成功只白名单输出 answer/model/usage（可选 Provider request ID，见 §6）；不输出 SDK 完整对象、system 配置、密钥、任意 Provider extra 字段或原始 error body。
- 固定连接 timeout 5 秒、read timeout 50 秒、write 10 秒、pool 5 秒，并以 60 秒总 deadline 包住实际调用。测试用依赖注入缩短 deadline，不用 sleep 60 秒。
- 异常处理顺序先 APITimeoutError（属于连接错误子类），再 APIConnectionError，再状态错误；适配后的错误见 §6.2。总 deadline 过期同样返回 504。正常关闭 client，不吞掉取消信号。
- 如使用 extra-forbid 或自定义校验错误，验证返回体不回显敏感输入；不得把 Pydantic 的完整 input、SDK 异常字符串、Authorization 或 Provider body 写入日志。
- 最小依赖：保持 FastAPI/uvicorn/Pydantic 与现有范围一致、显式 httpx、固定 openai；不新增 langfuse、Argus 包、Agent Framework、OTel instrumentation。

目标测试和 `ruff`、`pip check` 通过后提交：`feat(demo): add isolated real llm http agent`。实现与回归测试同一提交；依赖 aggregate 引用随本提交，避免提交后测试缺 SDK。

### S2：可选 Compose 与 secretless 门禁（原子提交 2）

- 新 Dockerfile 使用项目允许且已验证的 Python 运行时；优先 Python 3.12 与 CI 对齐。现有 Demo Dockerfile 是 Python 3.14，本次不要改它或把它视为必须复制的模板。
- 新 override 只定义 `real-llm-agent`，服务内 8080，宿主机可选端口绑定 `127.0.0.1:18084:8080`，build context 限新服务目录，healthcheck 仅探测 `/health`。
- `MODEL_*` 使用显式 environment 三项映射；可用 `${VAR:?required}` 阻止显式启用时缺配置。不能把整个 `.env.local` 作为 Runner 的 `env_file`，也不能向 Runner 增加 `MODEL_API_KEY`。
- 无顶层 `name`/自定义 networks，继承基础 Compose 默认网络；启动新服务用同一 project 和相同 `-f` 组合，禁止创建独立孤立网络导致 Runner 无法解析服务名。
- 提供 `.env.real-llm.example`；复制到已忽略的 `.env.local`，若用户已有该文件则只提示追加三项，不能覆盖。
- 扩展零评测 SDK guard 覆盖 Demo 与新 Agent（源码和 requirements）；新测试同时验证无模型网络的 health。
- 新增 YAML 静态校验；Docker 可用时以明确假凭据执行 combined `config -q`，缺 Docker 的本地路径按现有规则显式 SKIP，不宣称 Compose 验证通过。
- `.github/workflows/ci.yml` 中安装根 requirements 的 pip cache 输入都纳入新 requirements；保持已有 `pip check`、CLI 安装等逻辑。新服务 Docker build 放在普通 Docker 验证 job，假参数仅用于配置解析，不运行真实调用。
- Dependabot 增加新 Docker 目录；root pip aggregate 已包含新 requirements，不另建重复 pip 更新组。不要改 Actions pin、远端保护、Cloud secrets 或 trusted workflow 权限。

先写并运行 `test_real_llm_deployment.py` 的 RED，再改配置/guard，目标测试和无 Key 配置验证 GREEN 后提交：`build(demo): wire optional real llm compose and offline checks`。

### S3：Argus 跨边界回归（原子提交 3）

只增加测试，不为测试方便修改 Runner 生产逻辑。

- 使用现有 `map_request()` 和 Registry 数据模型加载与 §6.3 一致的注册配置。
- 测试 Agent 使用真实 SDK，`httpx.MockTransport` 模拟上游；Executor→Agent 可使用 `httpx.ASGITransport`。验证 URL/path、Bearer 测试值、model、messages，而不是直接 mock SDK 的 `create()` 返回对象。
- 用 `RemoteAgentExecutor.invoke_once(client=...)` 的现有接缝走通 HTTP 边界；Worker 持久化测试参考现有 `setup_runtime` / typed-results 夹具，以测试专属 transport 注入或补丁接通实际 invoke_once，不替换评分结果。
- 至少一次完整断言：六条 seed 输入中代表性用例真实经过适配服务；冻结 evaluator 仅 `answer_present@1.0.0`；结果 boolean true；Usage 从 response dot path 到现有 cost extraction/Attempt；失败场景对应 HTTP status、execution failed、eval skipped/quality unknown、无假通过。
- Worker 集成可参数化代表性成功和三类失败；不要要求真实 Langfuse、数据库服务器或模型 Key，让普通 CI 使用现有 SQLite/fake-Langfuse 测试设施。
- 明确验证 502/504 虽都属 `HTTP_5XX`，但 HTTP status 与安全 error_message 区分；不能在断言中要求结构化 Agent error code 已出现在现有 Console。

GREEN 后提交：`test(demo): cover real llm argus execution and scoring contract`。

### S4：README 与无密钥操作示例（原子提交 4）

文档顺序严格为：配置三项→默认栈→新服务→health→注册→创建并运行 Launch→查看结果→故障处理。

- 用 §6.3 的实际接口和 payload，不沿用旧 `/experiments/run`，不默认注册 baseline，不默认跑银行全指标。
- 选择固定 model ID/可用的 Provider 模型快照并记录实际返回 model；兼容网关没有不可变模型快照时如实说明。
- 注册相同 ID/version 遇 409 时读取并核对现有配置，不吞错或覆盖；模型/base URL/镜像/提示策略改变创建新 version，不能悄悄复用旧 v1。
- 真实调用前提示六条 Dataset Items 会发送到外部模型并可能计费；只使用合成数据与测试项目；不要让用户把真实 Key 贴进对话或验收记录。
- README 提供 §8 的验收命令和期望，不提前给出“真实 E2E PASS”。
- Cloud 栈可附简短替代说明，但主路径是本地全栈；Cloud 命令必须保留当前 main 的 Langfuse 数据源修复、非 dev 的 build identity 及既有 env_file 配置，不再改 Cloud 栈源码。
- 根 README 只加一条入口。

校验文档 JSON 与当前 Pydantic 模型、检查占位值、链接、端口后提交：`docs(demo): document real llm local evaluation walkthrough`。

### S5：显式真实验证与脱敏报告（原子提交 5，具备运行授权和条件时）

- 获得真实付费调用及目标测试项目写入授权后执行。Key 由用户在本地注入；只核对是否配置，不读取/复制/打印其内容。
- 真实运行六条 Dataset Items，max_concurrency=1，两层重试 0；记录每条 Dataset Item、Attempt、Trace、Score 对应关系。不要为了获得好看的结果盲目重跑。
- Console 检查实际 Agent output 和评分；Langfuse 查看 Experiment/Trace/Score 的真实落地与一致身份。后台同步最终一致时有界等待，不能只看 Launch 执行结束就说同步成功。
- 针对无效 Key、不可达和超时验证失败。优先用可控模拟上游完成确定性错误测试；真实故障专项用独立测试 Agent/version 和稳定可控目标，不能改正在验收成功用例的 v1 配置。没有真实故障复现证据时标明“自动化验证，非真实 Provider 故障实测”。
- Provider Usage 只作为辅助真实请求证据；推荐同时记录 Provider request ID/脱敏网关日志。Provider 不给 Usage 时仍可凭网关证据通过，但需标记 usage unavailable。若两者都无法核查，真实性验收未完成。
- 报告使用实际日期记录 Git SHA、Runner build、Agent image/artifact、AgentVersion ID、模型名、脱敏 base origin、Dataset/version/count、evaluator/version、Launch/Item/Attempt/Trace/Experiment IDs、结果数、实际命令及 PASS/FAIL/BLOCKED/NOT RUN。

报告脱敏检查后提交：`docs(validation): record issue198 real model end-to-end evidence`。没有环境/授权时交付前四单元及未完成清单，不创建假的 PASS 报告，不关闭 Issue。

**提交授权规则**：以上是 Luna 后续获得实施授权后的本地原子提交点；每次先验收并检查暂存 diff，只提交本任务内容，无需每次再询问。push、PR、merge、发布、远端配置及真实付费调用分别处理授权；本轮仅生成方案，不执行这些操作。

## 6. 关键实现说明

### 6.1 建议响应契约（新 Agent 自有业务契约，不是 Runner 新 API）

```json
{
  "answer": "上游模型实际返回的非空文本",
  "model": "上游响应中的实际 model，缺失时为 null",
  "usage": {
    "input_tokens": 12,
    "output_tokens": 34,
    "total_tokens": 46
  }
}
```

映射只取 Chat Completions 的 `prompt_tokens → input_tokens`、`completion_tokens → output_tokens`、`total_tokens → total_tokens`。Usage 整体没有时 `usage: null`；部分字段缺失、无效或无法作为非负整数验证时对应字段 null，bool 不当成整数。不要通过字符长度或固定样例估算 token；不要把 total 缺失改为 0。现有 Argus 提取层可能对已知 input/output 相加，报告区分其归一化值与 Provider 原始值。

`answer = choices[0].message.content`，必须是非空白字符串；空 choices、content null/空白、仅 tool_calls、畸形 2xx JSON 均为上游响应无效，不能当成功。可以修剪边缘空白，但不能合成 answer。只调用一个 choice、一次请求，不把多轮调用费用声称为单次已完整统计。

`model` 没有返回时为 null，不把 `MODEL_NAME` 冒充实际 model。实际配置名可保存在不含 Key 的版本 metadata/验收记录中。可选 `provider_request_id` 只白名单读取并校验长度/字符集，缺失即 null，不自动回显任意头。

`/health` 可固定返回 `{"status":"ok","upstream_checked":false}`。不需要返回 base URL、配置 dump 或真实 Key。首版可以完全不增加 trace response marker；Runner 仍传播 `traceparent`，但 `trace_context_received=false` 只能说明未提供其历史 marker，不能解释为完整分布式链路失败。若主动提供 marker，须明确它仅证明 Header 收到，不证明模型内部 Span；不为 marker 修改 Runner。

### 6.2 安全错误映射（拟新增稳定 Agent error code）

错误 body 统一为受控内容，例如 `{"error":{"code":"MODEL_TIMEOUT","message":"模型请求超时"}}`。安全日志只记 code、HTTP status、耗时、可选已清洗 request ID，不记 Key、请求体、Provider 原文或整条异常。

| 上游故障 | Agent HTTP / code | 当前 Argus 可见结果 |
|---|---|---|
| 401 / AuthenticationError | 401 / `MODEL_AUTH_FAILED` | `HTTP_4XX`、http_status=401、Item failed；明确这是模型鉴权，不是 Argus Endpoint Credential |
| 403 | 403 / `MODEL_PERMISSION_DENIED` | `HTTP_4XX`、http_status=403 |
| 连接失败、DNS、连接拒绝 | 502 / `MODEL_UNREACHABLE` | `HTTP_5XX`、http_status=502 |
| SDK timeout / 总 deadline | 504 / `MODEL_TIMEOUT` | `HTTP_5XX`、http_status=504；不是 Runner 的 READ_TIMEOUT |
| 上游 429 | 429 / `MODEL_RATE_LIMITED` | `HTTP_429`；max_retries=0，不发第二次请求 |
| 上游其他非成功状态 | 502 / `MODEL_UPSTREAM_ERROR` | `HTTP_5XX`；安全日志可记原始数字 status，不透传原文 |
| 2xx 但协议无效/无非空回答 | 502 / `MODEL_INVALID_RESPONSE` | 执行失败，不产生 answer_present true |
| 本地业务请求无效 | 422 / 受控校验错误 | 不调用上游 |

不要求所有 502 子类型在现有 Console 显示不同 error code。Issue 必需三类通过 401/502/504 已可区分，Agent 本地安全日志可进一步细分。不能为透传任意 Provider 错误而扩大 Runner response/数据库字段。

网络 deadline 不保证 Provider 取消计费；即使 Runner cancel，也不能保证上游已停止处理。禁止宣称全局 exactly-once 或取消后绝不计费。

### 6.3 注册与 Launch 的准确操作样例

以下命令是后续 README 的待执行示例，本轮仅做了 payload 模型校验，**未对运行服务发送请求**。工作目录 ROOT；除实际模型 env 本地填写外，没有真实密钥。

```bash
cd /Users/hosea/.codex/worktrees/1a39/Argus

# 已有 .env.local 时不要覆盖；只在编辑器中追加必要三项。
# 文件不存在时再执行：cp .env.real-llm.example .env.local
# MODEL_BASE_URL 应为 API 前缀（例如 https://gateway.example.invalid/v1），
# 而不是完整 /chat/completions 路径；MODEL_NAME 必须是该网关实际支持的模型。
make up

# 先确认 Langfuse 为本地测试项目，再运行：
./scripts/bootstrap.sh

# 保持与 make up 同一个 Compose project；仅启动新增服务。
docker compose --env-file .env.poc --env-file .env.local \
  -f docker-compose.yml -f docker-compose.real-llm.yml \
  up -d --build --no-deps real-llm-agent

curl -fsS http://127.0.0.1:18084/health

# 注册 AgentDefinition；重复执行的 409 应先核对，不自动忽略。
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

# 创建记录尚不运行。费用数据缺失，故不配置 amount/currency path。
# 增加真实 Agent image digest/artifact_ref 与无密钥模型 metadata 后再正式登记版本。
# 同一次逻辑提交重试用同一个 key；新的一次付费运行需新 key，不自动触发运行。
REQUEST_KEY="issue198-$(python3 -c 'import uuid; print(uuid.uuid4())')"
LAUNCH_JSON=$(curl -fsS http://localhost:18080/api/v1/experiment-launches \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $REQUEST_KEY" -d '{
    "name":"issue198-real-llm-smoke",
    "agent_id":"real-llm-agent",
    "agent_version":"v1",
    "dataset_name":"banking-agent-regression",
    "environment":"poc",
    "max_concurrency":1,
    "evaluator_selections":[{"id":"answer_present","version":"1.0.0"}]
  }')
LAUNCH_ID=$(printf '%s' "$LAUNCH_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

# 先确认 manifest 中 Dataset version/count、Agent、Evaluator 与 Runner 四类身份。
curl -fsS "http://localhost:18080/api/v1/experiment-launches/$LAUNCH_ID"
# 确认付费调用授权后，才执行这个真正开始请求模型的操作：
curl -fsS -X POST "http://localhost:18080/api/v1/experiment-launches/$LAUNCH_ID/run"

# 有界轮询详情到终态（建议上限 10 分钟），再检查六条 Item。
curl -fsS "http://localhost:18080/api/v1/experiment-launches/$LAUNCH_ID"
curl -fsS "http://localhost:18080/api/v1/experiment-launches/$LAUNCH_ID/items?limit=100"
# 从实际返回中取 item ID，不猜测。
# GET /api/v1/experiment-item-executions/{item_execution_id}/attempts
# GET /api/v1/experiment-launches/{launch_id}/summary
# GET /api/v1/experiment-launches/{launch_id}/result-snapshots
```

补充规则：

- Runner 不接收 MODEL Key，也不创建 credential_id/credential_ref。这里的 Agent Endpoint 没有额外鉴权，只用于受控本地 Compose 网络，宿主机端口仅 loopback；不面向公网。
- 示例省略 dataset_version 以由服务器解析并冻结，不意味着允许最终 Manifest 保留 `latest`。验收必须读取、记录真实冻结版本和六条 Item；若现有 Dataset 已被其他人修改，不覆盖它，先核对目标测试项目。
- `environment=poc` 在 AgentVersion 与 Launch 一致；它与 Provider 的模型部署名不是同一个概念。
- 使用 actual `artifact_ref` 与 metadata 记录模型配置/制品身份；不可把模型 Key、URL 查询凭据、整个环境 dump 或可逆的 secret 指纹放入 metadata。不能靠日志中出现“model”证明真正请求发生。
- 不用 `curl /invoke` 额外预跑付费请求，health 不收费即可；若需要直连排错，将额外那次请求单独记录，不把 Provider 总请求数简单断言为六次。
- 在 Console 选择已有 real-llm-agent/v1 创建 Launch 时也必须明确只选 answer_present；API 创建的 Launch 同样可在现有 Launch 列表中查看，不需要新页面。

### 6.4 网络与部署说明

- Eval Runner 容器到 Agent：`http://real-llm-agent:8080/invoke`；容器内 `localhost` 是自身，不是宿主机或另一个服务。
- Agent 到云端：`MODEL_BASE_URL=https://实际网关/实际API前缀`，SDK 再追加 Chat Completions 路径；不要强制所有网关都有 `/v1`。
- macOS Docker Desktop 到宿主机网关：使用 `host.docker.internal` 和正确端口/API 前缀；Linux Engine 按实际 Docker 支持增加 `host-gateway` 映射（只加在新服务），并核对宿主机监听地址与防火墙。
- 首版不要默认向公网暴露 Agent，也不要用关闭 TLS 校验解决企业证书；受信 CA 与代理需求按部署环境显式配置。
- `docker compose config` 会展开环境值；真实 Key 配置只用 `config -q`，不要把完整配置、`docker inspect` 环境或 shell `set -x` 输出存档。
- 停止新增服务用同一 Compose 组合 `stop real-llm-agent`；不要执行 `down -v`、`make clean` 或删共享 Volume。默认栈的 down 可能提示 orphan，需按新增服务组合显式停用，不为清理扩大删除授权。

## 7. 测试方案

### 7.1 新 Agent 自动化矩阵

`/Users/hosea/.codex/worktrees/1a39/Argus/tests/test_real_llm_agent.py`：

| 场景 | 核心断言 |
|---|---|
| 正常多轮输入 | 真实 SDK 发送一次 POST 到所配 prefix 的 chat/completions；model/messages 与输入一致；只含允许的字段 |
| Key 隔离 | 上游收到测试 Bearer；Key 不进入返回体/日志/customer_id/Manifest；Provider error 回显假 Key 时也不泄露 |
| health/启动 | health 不发上游请求；缺任一必要配置拒绝启动且错误不回显；health 不保证远端可用 |
| 模型输出 | 非空文本来自模拟响应；实际 model 与配置名不同也按响应保留；缺 model 为 null |
| Usage | 完整、整体缺失、部分缺失、显式 0、负数、bool/非法类型分别测试；不臆造金额、不把缺失归零 |
| 无效响应 | 空 choices/null content/空白/仅 tools/非对象 JSON/畸形 JSON 都不返回成功 answer |
| 错误 | 401、403、429、上游 5xx、连接异常、SDK timeout、绝对 deadline；对应 status/code 确定，零 SDK retry |
| 无效业务输入 | 空 messages、空白 content、不支持 role、意外配置字段不触发上游；422 不泄露敏感 input |
| 生命周期 | client 在关闭时清理；导入模块不调用真实网络；取消不变成模拟成功 |

注：模拟传输抛出的 timeout 要用所选 SDK 支持的 httpx 异常；一次单测检查“调用计数=1”，可防止 SDK 默认重试漏关。MockTransport 也要拒绝意外 host/path，避免测试悄悄走真实网络。

### 7.2 Argus 集成矩阵

`/Users/hosea/.codex/worktrees/1a39/Argus/tests/test_real_llm_agent_integration.py`：

- 真实 `map_request()` 从 seed input 生成 messages/customer_id，后者不外传给模型。
- 注册 payload 与 `AgentVersionCreateRequest` 一致，固定 evaluator 版本成功冻结。
- 通过 Agent ASGI + SDK 模拟 HTTP 上游产生真实响应，现有评分返回 boolean true；不含 intent/tool 等伪造银行字段。
- 三类失败到 Item/Attempt 的 HTTP 状态、错误、quality/eval 状态与无重试；不存在“执行失败但评估 PASS”。
- usage mapping 完整/缺失到现有提取函数与 Attempt 不伪造金额；total 推导按现有代码断言，不修改核心逻辑。
- fake-Langfuse 仅证明同步契约/队列路径；报告必须标 fake，真实 Experiment/Trace/Score 验收另外做。

### 7.3 部署与架构检查

`/Users/hosea/.codex/worktrees/1a39/Argus/tests/test_real_llm_deployment.py`：

- base Compose 未增加必需 MODEL 变量；override 服务通过同一默认网络访问；health 只在本地。
- `MODEL_API_KEY` 只在新服务 environment，样例不含真实 Key，不挂载 `.env.local` 或 Docker socket。
- 新 Dockerfile context/copy 不包含仓库根目录；无评测 SDK、dataset 读取或评分逻辑。
- aggregate requirements、pip cache inputs 与新服务路径一致；不要求 CI 模型 secrets。
- README JSON 示例用真实 Pydantic models 校验；配置静态 parse 与 `config -q` 分层区分。

### 7.4 本轮已运行的证据（不是新增功能的 GREEN）

工作目录均为 ROOT，Python 使用上述已有共享 3.12 环境：

```bash
/Users/hosea/work/ai-eval/Argus/.venv/bin/python -m pytest -q \
  tests/test_expected_pass_rate.py tests/test_executor_single_invoke.py tests/test_cost_evidence.py
# PASS：15 passed in 0.60s

/Users/hosea/work/ai-eval/Argus/.venv/bin/python -m pytest -q \
  tests/test_registry.py tests/test_dataset_resolver.py \
  tests/test_issue_80_evaluator_versioning.py tests/test_issue_82_typed_results.py
# PASS：72 passed in 1.45s，115 条现有弃用警告
```

附加检查：

- 三份拟用 payload 经 `AgentCreateRequest`、`AgentVersionCreateRequest`、`ExperimentLaunchCreateRequest` 校验：PASS。仅证明数据模型接受，未证明远程注册/执行。
- 直接调用当前 `answer_present()`：真实样例文本 true，空白和缺 answer 为 false：PASS。
- Docker Compose CLI 可用，版本 2.40.3；未据此宣称 Docker daemon、镜像构建、真实栈或网关可用。
- 现有测试警告包含 SQLAlchemy datetime.utcnow 与 FastAPI TestClient/httpx 弃用，不在本次范围内。
- RED、新 Agent 单测/集成、`make validate`、Compose 新配置构建、真实模型、Console 实页及 Langfuse 真同步：**NOT RUN**（本轮为方案阶段）。

## 8. 验收标准

### 8.1 实施后的本地自动化（待执行）

从 ROOT 执行，使用本 checkout `.venv`，不依赖共享路径：

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip check

.venv/bin/python -m pytest -q \
  tests/test_real_llm_agent.py tests/test_real_llm_agent_integration.py \
  tests/test_real_llm_deployment.py \
  tests/test_expected_pass_rate.py tests/test_executor_single_invoke.py \
  tests/test_cost_evidence.py tests/test_registry.py tests/test_dataset_resolver.py \
  tests/test_issue_80_evaluator_versioning.py tests/test_issue_82_typed_results.py

.venv/bin/python -m ruff check services tests
make validate

# 原默认栈不提供任何 MODEL 变量仍可静态校验。
env -u MODEL_BASE_URL -u MODEL_API_KEY -u MODEL_NAME \
  docker compose --env-file .env.poc -f docker-compose.yml config -q

# 新服务配置验证用明确的假参数，不调用上游、不输出完整展开结果。
MODEL_BASE_URL=https://model.invalid/v1 MODEL_API_KEY=not-a-real-key MODEL_NAME=test-model \
  docker compose --env-file .env.poc \
  -f docker-compose.yml -f docker-compose.real-llm.yml config -q

# 构建只安装依赖，不会请求模型。
docker build -t argus-real-llm-agent:issue198 services/real-llm-agent

git diff --check
git diff --exit-code -- docs/openapi.json services/console/src/api/schema.d.ts
```

以上为未来验证命令，不能把本方案文件存在当作测试通过。新 Agent 的 FastAPI schema 不并入 Runner OpenAPI。Runner API 未改时，两个已有快照应保持无 diff；若发现确实需改，先解释必要性，并按 AGENTS 同步生成两侧契约，不能顺手手写 schema。

### 8.2 可执行验收 Checklist

- [ ] **AC1 默认隔离**：不设置 MODEL 三变量，原环境启动/`make demo` 不受影响；基线测试严格断言 v1=2/6、v2=6/6。
- [ ] **AC2 真实模型**：仅三项必要配置，普通 HTTP `/invoke` 路径可向所配网关获得真实回答；没有 mock/fallback 开关，服务自身无评测 SDK。
- [ ] **AC3 完整执行**：注册 AgentDefinition 与不可变 Version，Runner 经 Compose DNS 访问；实际冻结六条 Dataset Items；每条有对应 Attempt 与可查询终态；本次无自动重试。
- [ ] **AC4 基本评分**：Manifest 只含 `answer_present@1.0.0`，成功 Item 为 boolean true；失败没有假 PASS。文案与验收报告不把它称为业务准确率 100%。
- [ ] **AC5 可观测闭环**：Console 可看到真实输出、Item/Attempt、逐项评分；Langfuse 实际可打开相关 Experiment、Trace、Score，并与同一 Launch/Item 对应；只有链接不算落地证据。
- [ ] **AC6 真实调用证据**：每条成功结果能通过响应 Usage/request ID/脱敏网关记录核查真实上游请求；Usage 未给就标 unavailable/null，金额未知保持 unknown；证据不足不能写 PASS。
- [ ] **AC7 失败区分**：401/502/504 分别被自动化测试及 Argus 集成验证为执行失败；清晰区分模型 timeout 和 Runner READ_TIMEOUT；真实专项未运行则独立标明。
- [ ] **AC8 安全**：真实 Key 仅注入新服务；仓库、镜像、日志、API/Manifest/报告均无 Key；配置与异常不回显 secret；真实数据只在用户批准后外发。
- [ ] **AC9 自动化门禁**：RED 留有实际证据；目标测试、pip check、ruff、`make validate`、新服务 Docker build 和 combined Compose config 均 PASS；缺依赖/环境明确 BLOCKED/SKIP，不写成 PASS。
- [ ] **AC10 交付完整**：短 README、env 样例、可选 Compose、注册/Launch 示例、脱敏真实 E2E 记录齐全；本地 commits 每个自洽，diff 无任务外内容。

**DoD 判定**：自动化全绿但真实模型/Console/Langfuse 未跑，结论只能“实现已交付，真实 E2E 待验收”，不能“#198 完成”。Langfuse 失败与执行/评分失败分别记录，不借机修改质量语义或关闭平台保护。

## 9. 风险与注意事项

1. **外部费用与数据边界**：六条输入发给真实模型；失败和被取消的请求也可能收费。总调用次数不是分布式 exactly-once 保证；调度重投/人工重跑另有边界，不在本 Issue 重构。
2. **模型能力边界**：该 Agent 没有真实银行工具，不得生成 `intent/tool_calls/escalated` 等伪造业务成果来满足旧 Evaluator；文字回答可能包含幻觉，answer_present 不验证事实。
3. **配置漂移与复现**：AgentVersion 不可变不等于云端别名不可变；真实 model、镜像/artifact、非敏感配置身份及冻结版本必须记录，模型别名无法冻结的限制必须披露。
4. **SDK 与依赖**：固定 tag 文档与 Context7 main 片段可能不同；按所选固定版本测试。增加 SDK 进入开发 aggregate 后的依赖兼容以干净安装/pip check 为准，不能用全局已安装包掩盖缺项。
5. **CI 影响**：改 workflow/依赖可能被现有 Cloud policy 判为高风险，不能因此绕过 exact-head receipt 或更改 Ruleset；既有 Cloud acceptance 不能替代本 Issue 的真实模型实测，反过来也不行。
6. **环境独立**：当前 main 的 PR #199 已修 Cloud source；本方案主路径是本地全栈，不恢复 seed 默认值来让假数据路径更容易通过。
7. **错误口径**：三个必需故障通过 HTTP status 可区分已经够用，不扩大为核心错误模型重构。Provider 使用非标准 status 时按安全上游错误归类，在文档记录兼容限制。
8. **清理与回退**：无数据库迁移；停新服务和停止新增 Launch 即可回退。已登记版本/Launch 是证据，不默认删除、不重写历史；如需代码回退按原子提交 revert，但需正常实施授权，不改共享容器/Volume。
9. **范围排除**：不加 Coding Adapter、sandbox、tool calling、LLM judge、轨迹采集、自动 pricing、Baseline/Release Gate，不改 Demo 预期，不对现有 UI 做无关重构。

## 10. Luna 执行清单

- [ ] **S0**：刷新 Issue/main/工作树证据，明确实施分支；保留既有改动，准备独立 Python 3.12 环境，核对真实付费调用授权另行处理。
- [ ] **S1 RED**：新增 Agent 回归测试并实际运行失败，记录失败原因；不能仅因缺任意环境依赖而误判需求已覆盖。
- [ ] **S1 GREEN**：实现 SDK/lifespan/输入输出/错误/timeout/双层重试约束；新 tests、ruff、pip check 通过，检查暂存 diff，创建提交 1。
- [ ] **S2**：写部署/架构测试，加入可选 Compose、样例、独立镜像、无 Key 门禁和最小 CI/Dependabot 接线；RED→GREEN 后创建提交 2。
- [ ] **S3**：用真实 Agent+SDK 与模拟 HTTP 走映射、执行、冻结评分、Usage、失败持久化，现有 Demo 与核心回归保持绿色；创建提交 3。
- [ ] **S4**：补 README、准确 JSON/curl、网络/凭据/成本边界，校验命令和契约；创建提交 4。
- [ ] **全门禁**：运行 §8.1 全部适用检查，记录 PASS/FAIL/BLOCKED/NOT RUN，明确既有失败与本 PR 归因；不为凑绿放宽断言或改基线。
- [ ] **S5**：在测试项目和用户本地注入 Key 下执行真实 Launch；核对六条 Item、Provider 请求、Console、Langfuse 的同一身份与落地；保存脱敏记录，验证完创建提交 5。
- [ ] **最终交付**：列出真实 commit hashes、分支/HEAD、变更文件、RED/GREEN、目标回归、make validate、真实 E2E/同步/失败分类和未执行项。没有真实证据就保留 Issue 未完成；push/PR/merge/发布依各自授权，不随本地 commit 自动执行。

---

### 本轮方案交付状态

- **PASS**：Issue 与评论读取；当前 checkout 与远端 main 差异核对；目标代码/模型/接口定位；现有 87 项回归；三个建议 payload 模型校验；answer_present 边界核验。
- **NOT RUN**：新增功能实现与 RED/GREEN、make validate、新镜像/override 验证、真实模型、Console 真实页面、Langfuse 实测。
- **无设计性阻塞**：最小实现不需要新增协议或修改核心模型；真实模型、网关条件、测试项目与付费授权是后续运行条件，不通过索要 Key 明文解决。
