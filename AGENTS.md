# Argus — Agent 工作指导规范

> 本文只记录 Argus 仓库特有的边界、事实与门禁。
> 通用工作约定（授权、删除/发布、Git 安全、证据与完成标准、工具路由）继承上层指令，本文不重复。
> 本文与上层指令冲突时，以更高优先级指令为准。

---

## 0. 速查

最常用命令：

```bash
make validate                        # 唯一本地质量门禁（8 步，见 §4）
make up / make down                  # 本地全栈启停
make demo                            # Demo 回归（v1 2/6、v2 6/6）
make console-test / console-e2e      # Console 门禁
```

产品范围先读 [产品边界与需求准入](docs/product-boundaries.md)；以下门禁是开发约束，不代表待建能力已交付。

硬边界（详见 §2）：

1. 业务 Agent 零 Evaluation SDK；
2. Langfuse 与 Argus 职责分离；
3. `traceparent` 必须传播；冻结身份/证据，不承诺随机系统完全确定性；
4. 质量门禁不等于最终发布授权；禁止把版本 A 的结论给版本 B 使用；
5. `docs/openapi.json` 与 `services/console/src/api/schema.d.ts` 必须同步。

按任务类型读取（渐进式披露：只读你要改的那一节）：

| 你要改什么 | 必读 |
|---|---|
| FastAPI 路由 / 请求响应模型 | §4.2 第 5 步、§4.3、§5.1；改完执行 `python scripts/export_openapi.py` |
| Agent Registry / 版本管理 | §5.2 |
| 队列 / Worker / 状态机 / 取消恢复 | §5.3 |
| Evaluator / Baseline / 对比 | §5.4 |
| Console 前端（`services/console`） | §5.6 + `.agents/skills/argus-design-system/SKILL.md` |
| Langfuse 界面语言镜像 | §5.7 + `deploy/langfuse/README.md` |
| Database Schema | §5.1；迁移按序号追加到 `migrations/`，不修改已发布迁移 |
| 提 PR 或参与贡献 | `CONTRIBUTING.md` |
| 新需求 / 扩范围 / Roadmap / Issue 设计 | §2.1、§2.2 + `docs/product-boundaries.md` |
| 编写文档 | §11 |

---

## 1. 规范级别

- **MUST / 必须**：硬性约束，除非任务本身明确修改该约束，否则不得违反。
- **SHOULD / 应该**：默认遵守；不遵守时在交付说明中给出原因。
- **MAY / 可以**：按任务需要选择。
- 本规范适用于需求分析、代码修改、测试、重构、文档、配置、Issue/PR 处理和交付说明。
- 用户可见说明、任务清单、代码注释和仓库文档默认中文；标识符、协议字段和业界术语保持英文。
- 只修改任务必需的内容；不覆盖用户已有未提交改动，不夹带无关重构。
- 发现同一处存在并行改动（其他会话、子代理、分支或手工编辑）时，禁止回退或覆盖他人版本；无法确认归属时保持现状并向用户说明。
- 不要求输出内部推理过程；对外只提供结论、证据、方案、验证结果和必要权衡。

---

## 2. 系统定位与架构边界

**Argus 是已有 Agent 服务的受治理评测桥接服务、持久化执行控制面与发布质量证据/门禁平台。**

```text
Langfuse = 日常 Dataset/Prompt、Trace/Observation、实验分析与在线评估
Argus    = 适配接入 / 持久化 Launch 与 Attempt / 冻结证据 / 批准基准 / 质量 Gate
Agent    = 业务 Runtime、会话与业务副作用
CI/CD    = 构建/部署/灰度/回滚与最终发布授权
```

### 2.1 范围准入门禁

每个新增需求、Issue 设计和 PR MUST 写清：消费者与实际场景；降低接入成本/提高执行可靠性/增强证据可信度/约束发布风险中的哪项；为何不能复用现有能力；权威状态归属、非目标、失败/权限/恢复验收及维护成本。详见 [产品边界 §5](docs/product-boundaries.md#5-开发需求准入人工评审门禁)。

- MUST NOT 默认扩建第二套 Dataset/Prompt 编辑、通用 Trace/实验分析、在线评估告警、通用 DAG/审批流、生产部署/灰度/回滚或自建 IdP/Vault。
- SHOULD 优先复用 Langfuse、企业身份/Secret/审批/CI 设施以及现有 Argus 模块；不能因 SDK 有并发就省略持久化执行，也不能再造第二套调度/结果体系。
- Trajectory/Judge、新协议、多 CI Provider 等 MUST 按具体消费者重新准入；深度轨迹和自建 Judge 不再是普通发布闭环/v1.0 的默认前置。
- 既有接口、冻结历史、安全与来源校验、真实夹具、质量回归和 i18n 补丁必须兼容维护；收敛范围不等于删除实现/验收证据或放宽门禁。
- Langfuse Python/JS/TS SDK + 外置脚本同样可跨语言调用服务；MUST NOT 将零业务 SDK、并发/比较或基础 CI Gate 宣称为 Argus 独占能力。

### 2.2 架构与可信证据门禁

1. **业务 Agent 不必引入 Evaluation SDK**
   - Argus Harness 放在外部 Runner；`services/demo-agent/` MUST NOT 引入评测 SDK、读取 Dataset 或自行评分。
   - 对已支持调用契约承诺低侵入，不承诺任意接口/多轮/异步协议“只填 URL”。业务已有的可观测性 SDK 不要求删除。
   - 元数据用 Header/W3C Trace Context，不强加业务 DTO 分支。
2. **权威状态与外部采纳**
   - Dataset/Prompt 日常编辑与 Trace 分析优先 Langfuse；Argus 拥有本次 Launch、冻结 Snapshot、批准基准和质量决策。
   - 发布必需的冻结用例/最小证据允许持久化；MUST NOT 建双向可变主数据或用在线 latest 回填历史。
   - 外部 Score 必须按冻结采纳契约验证精确来源和尝试，不按名称猜归属；复用 `TypedEvaluationResult`，不建平行结果模型。
3. **W3C 与黑盒评测边界**
   - Runner MUST 传播 `traceparent`；内部 OTel 是可选增强，不是最终输出评测前置。
   - 无内部观测不伪造轨迹。冻结策略要求的证据缺失是 UNKNOWN；optional 诊断缺失不能污染无此要求的资格，不得临时改 optional 绕过失败。
4. **冻结身份不等于核验远端制品，也不保证完全确定性**
   - 正式结果必须固定 Dataset/Agent/Evaluator/Runner、环境、质量/比较策略与基准；禁止 latest 静默回退。
   - AgentVersion 声明和远端实际制品核验 MUST 区分；URL/手填版本号不是可信执行回执。未实现的核验、失效/有效期能力必须标待实现，不能宣称已受保护。
   - 模型、知识库、工具等不可控依赖必须披露；新证据/策略产生新 revision/decision，不改旧快照。
5. **执行可靠性与副作用**
   - 任务与 Attempt 持久化，评分重试不默认重调 Agent；lease/generation/取消/迟到结果隔离必须有反例测试。
   - MUST NOT 承诺远端 exactly-once；超时不代表未执行。有副作用调用在没有业务幂等/状态查询/隔离契约时不得盲目自动重放。
6. **质量门禁不是发布授权**
   - 分开 Agent Execution / Evaluation / Quality Conclusion / Langfuse Sync；无效/缺失证据不得被均分洗成 PASS。
   - Gate 只用于明确候选/环境/Snapshot/策略/基准；CI 的身份与适用范围核验属于正式集成验收，不以“最新绿灯”替代。
   - Argus 不持有生产部署权；例外处置须授权和审计且与原始 Gate 分离，不能把 FAIL/UNKNOWN 改成 PASS。未交付例外能力必须明确不支持。
7. **桥接访问与敏感数据**
   - 新接入必须明确授权目标、SSRF/DNS/重定向/TLS、凭证用途与资源权限；不得携凭证访问任意 URL。
   - 凭证只存引用；对日志/Trace/错误/快照/UI 的最小保留与脱敏均需验收，不能仅做前端遮罩。
8. **API 契约快照**
   - `docs/openapi.json` 与 `services/console/src/api/schema.d.ts` 必须同步；规划不等于新增了运行时接口。


---

## 3. 仓库地图

| 子系统 | 路径 | 语言 / 栈 | 事实来源 |
|---|---|---|---|
| Eval Runner（控制面） | `services/eval-runner/` | Python 3.12 / FastAPI / SQLAlchemy / psycopg / Redis | `app/`、`docs/openapi.json` |
| Console（管理前端） | `services/console/` | TypeScript / React / Vite / Vitest / Playwright / pnpm | `src/features/`、`e2e/` |
| Demo Agent（被测样例） | `services/demo-agent/` | Python / FastAPI | `services/demo-agent/app.py` |
| Langfuse i18n 镜像 | `deploy/langfuse/` | Dockerfile / Patch / Node 脚本 | `deploy/langfuse/README.md` |
| 数据库 Schema | `migrations/` | PostgreSQL DDL | Runner 启动时由 `MigrationRunner` 顺序执行 |
| Bootstrap 数据 | `config/agents.yaml`、`data/dataset.json` | YAML / JSON | Demo 回归与本地 bootstrap 使用 |

其他常用入口：

- `Makefile`：本地任务入口（`validate` / `up` / `demo` / `console-*` 等）。
- `scripts/validate.sh`：`make validate` 的真实实现，共 8 步，是 §4 的唯一事实来源。
- `docs/openapi.json`：API 合约快照，由 `scripts/export_openapi.py` 生成。
- `TECHNICAL_DESIGN.md`：总体技术设计基线；`walkthrough.md`、`VALIDATION_REPORT.md`：演示流程与验证记录。
- `CONTRIBUTING.md`、`SECURITY.md`：贡献流程与安全报告渠道。

---

## 4. 质量门禁

### 4.1 唯一本地入口

```bash
make validate
```

前置条件（缺失即失败或跳过）：

- Python 3.12，优先使用仓库内 `.venv/bin/python`；
- `pnpm` 必须可用（缺失时 `make validate` 直接失败，不降级）；
- Node.js 22（CI 固定版本；`pnpm` 在 CI 固定为 10.5.2）；
- Docker / Compose 可用；缺失时只跳过第 8 步静态 Compose 校验并显式打印 `SKIP`。

### 4.2 `scripts/validate.sh` 的 8 个步骤

```text
1. 解析 docker-compose.yml / config/agents.yaml / data/dataset.json
2. compileall + ruff check（services、tests）
3. Demo Agent 零评测 SDK 依赖 + traceparent 标记校验
4. Runner 远程实验与 W3C 传播标记校验
5. OpenAPI 快照同步（scripts/export_openapi.py 后 git diff 必须为空）
6. Console：schema 同步 → typecheck → vitest → build
7. pytest -q tests -m "not trajectory_local"（S4 真实采集另执行 make trajectory-test）
8. docker compose --env-file .env.poc config -q（无 Docker 时 SKIP）
```

### 4.3 最容易踩的坑

- 改动 FastAPI 路由、请求 / 响应模型后 MUST 执行 `python scripts/export_openapi.py` 重新生成 `docs/openapi.json`。
- 改动 `docs/openapi.json` 后 MUST 执行 `pnpm --dir services/console api:generate` 同步 `services/console/src/api/schema.d.ts`。
- 任一快照 drift 都会让第 5 / 6 步失败；只改后端不重新导出会出现"测试全绿但 validate 失败"。
- Console 侧改动还要跑 `make console-test` / `make console-e2e`（Playwright 需先安装 chromium）。
- `make up` 会把当前 Git commit SHA 作为 `ARGUS_BUILD_ID` 注入 Runner 镜像；正式 Launch 不接受空值或 `dev` / `latest` 身份。

### 4.4 CI 与分支保护

CI 与 Cloud E2E 由四条工作流协作：

- `.github/workflows/ci.yml`：PR 常规门禁包括 `Code Quality`、`Full Python Tests`、`Docker / Compose Validation`、`Console Quality & E2E`、`S4 Local Langfuse Integration`。PR 的 `Langfuse Cloud E2E` 为 `skipped_by_policy` 且不读取 Langfuse secrets；main push/dispatch 必须实际运行 Cloud，并由 `Main Cloud Acceptance` 核验实际 Cloud 步骤与 receipt。
- `.github/workflows/cloud-e2e-trusted.yml`：由可信 main 的 CI 控制器自动派发精确 head/base SHA；候选使用无需人工审批、仅限测试 project 的 Environment。可信 receipt v2 校验 GitHub Actions bot 发起身份及完整 Cloud 实测证据。
- `.github/workflows/cloud-integration-policy.yml`：可信默认分支控制器自动发布 `Cloud Integration Policy` 并在成功的当前 PR CI 后派发高风险 Cloud candidate；低风险仅在五项当前身份检查全绿时通过，高风险还要求匹配的可信 Cloud receipt，不要求人工操作。
- `.github/workflows/langfuse-i18n.yml`：正式 GHCR 发布必须通过同一当前 main SHA 的 `Main Cloud Acceptance` 与 receipt gate；PR 构建不发布。

main Ruleset 的目标 required checks 是五项常规 CI 加 `Cloud Integration Policy`；只有新 context 在真实 PR 上可见并完成存量 PR 初始化后，管理员才可移除旧 PR Cloud context，期间保留 strict 与无 bypass。Ruleset / Environment 属于远端配置，需按 [`docs/ci/cloud-e2e-policy.md`](docs/ci/cloud-e2e-policy.md) 独立核验，不以仓库 YAML 或本地测试推断已部署。涉及 S4 观测链路时，另执行 `make trajectory-prepare` 与 `make trajectory-test`；Managed Cloud 不替代自托管 Langfuse 镜像、migration、SDK、消息 / 持久化等专项验收。

### 4.5 回归基线

```text
Agent v1: overall_pass = 2 / 6
Agent v2: overall_pass = 6 / 6
```

由 `tests/test_expected_pass_rate.py` 保护。业务预期确需变更时 MUST 说明原因；禁止仅为通过测试修改基线。

---

## 5. 子系统实现约束

### 5.1 Eval Runner

- Request Mapping 与 Agent 业务协议解耦。
- Retry / Timeout / Rate Limit 行为必须可测试。
- `traceparent` 正确传播；Execution Failure 与 Evaluation Failure 分离。
- 同一 DatasetItem 的 Retry 不创建多个逻辑 Experiment Item。
- 幂等、取消、恢复、失败重跑通过明确状态机表达，不依赖临时脚本约定。

### 5.2 Agent Registry

- SSOT 是 PostgreSQL 中的 `AgentDefinition` / `AgentVersion`，不是 YAML。
- `config/agents.yaml` 仅用于本地 bootstrap / demo（`ARGUS_AUTO_IMPORT_YAML` 开启时导入）。
- Endpoint、Request Mapping、执行策略属于 AgentVersion，不属于 Dataset。
- 只保存 `credentialRef`，不保存明文 Secret。
- AgentVersion 视为不可变快照；配置变化创建新 version。

### 5.3 Launch / Queue / Worker

- 生产模式下 `ARGUS_REDIS_URL` 缺失 MUST fail closed，不允许无协调地多实例执行。
- Worker 在 Runner 进程内以 asyncio 任务运行（`ARGUS_WORKER_ENABLED` 控制），测试模式回退到内存队列。
- 状态机、并发限流、Attempt 记录改动 MUST 有对应回归测试；覆盖崩溃恢复、lease/generation、取消/迟到结果及副作用重试边界（§2.2）。

### 5.4 Evaluator / Baseline / Comparison

- 确定性规则或 Langfuse 托管评分能解决时不自建 Judge；新增自建 Judge 必须先过 §2.1，再固定实现/Prompt/Model 身份与采纳契约。
- Evaluator 输出必须可解释：输入、输出、阈值显式定义。
- 质量结论采用 fail-closed / unknown-safe：无 Evaluator 时不得判定通过。
- Baseline 绑定、Run Summary 与 Candidate 对比必须基于冻结版本，不使用 `latest`；批准基准不得因首次或最新运行而自动获得资格。

### 5.5 Demo Agent

MUST NOT：引入 Langfuse SDK / Argus Eval SDK、读取 Dataset、自行计算 Score、因为"正在评测"改变业务行为。
MAY：接收标准 `traceparent`、使用常规 OpenTelemetry instrumentation。

### 5.6 Console

- 聚焦接入/版本、执行进展/恢复、冻结发布证据、基准/审计；详细 Trace 和自由实验探索优先链接 Langfuse，不扩建通用浏览器。
- 历史报告读取选定 Snapshot；四类状态独立展示，未验证外部值只能作诊断。

- 组件与数据获取遵循现有 `src/features/`、`src/components/` 结构，不新建平行体系。
- API 类型来自 `services/console/src/api/schema.d.ts`（由 OpenAPI 生成），不得手写重复类型。
- 交互改动 MUST 覆盖 typecheck、vitest 与 Playwright E2E；删除、发布等破坏性操作必须二次确认。
- **设计体系与 Token 约束**：UI 组件必须严格使用 `src/design-system/tokens/` 与 `tokens.css` 中的语义 Token。严禁在 `.tsx` / `.css` 中编写裸十六进制色值（如 `#ffffff`、`#171923`）或 Tailwind 任意颜色类（如 `bg-[#...]`、`text-[#...]`）。必须通过 `pnpm --dir services/console lint:tokens` 检查。

### 5.7 Langfuse i18n 镜像

- 补丁只允许改变界面渲染、导航与语言切换，不得改动 Dataset / Trace / Experiment / Score 或业务 API 契约。
- 改动后运行 `make validate-i18n`；涉及集成行为时运行 `make validate-langfuse-integration`。

---

## 6. TDD 与测试选择

遵循 RED → GREEN → REFACTOR：功能代码修改前先写能表达需求的测试，实际运行确认 RED，再做最小实现。
Bug 必须有回归测试；禁止删除断言、放宽验收条件或跳过测试来制造绿色。

```text
纯业务规则                       → Unit Test
Registry / 状态机 / API 契约      → Domain / Contract Test
DB / Queue / HTTP / Langfuse 边界 → Integration Test
Dataset → Runner → Agent → Langfuse → Console → E2E
```

不要用 E2E 替代本应由单元或契约测试覆盖的边界条件。纯文档与格式化修改可豁免 RED，但 MUST 做相应验证。

---

## 7. Git / Issue / PR

- 开始编码前 MUST 阅读相关 Issue 与设计评论，并把验收标准转换为测试或明确验证项。
- Commit 保持单一目的，使用 Conventional Commits（`feat` / `fix` / `test` / `refactor` / `docs` / `chore`）。
- 不修改与任务无关的文件，不覆盖用户已有未提交修改。
- PR 描述 SHOULD 包含：问题、方案、测试证据、兼容性影响、风险。

---

## 8. 安全与敏感数据

- 禁止提交真实 Token、密码、Cookie、API Key 或数据库凭证。
- `.env.poc` 仅允许演示凭据；`.env.example` / `.env.cloud.example` 只放占位值。
- 日志、Trace、测试 Fixture 中不得无意保存真实 PII；Authorization、Cookie 等敏感 Header 不得写入 Langfuse Trace。
- 新增外部调用时考虑 timeout、重试边界、SSRF、证书校验与 Secret 获取方式。

---

## 9. Definition of Done

- [ ] 已阅读相关 Issue、设计评论和现有实现；旧设计与当前范围冲突时已按 `docs/product-boundaries.md` 收敛并标注。
- [ ] 已通过范围准入：消费者、四类价值、复用判断、权威状态、非目标、失败/权限/恢复验收与维护成本。
- [ ] 验收标准已转换为测试或明确验证项。
- [ ] 遵循 RED → GREEN → REFACTOR；例外已说明。
- [ ] 新行为有自动化测试；Bug 有回归测试。
- [ ] 目标测试通过。
- [ ] `make validate` 通过（含 OpenAPI 与 Console 契约同步）；跳过项已说明原因。
- [ ] 未破坏外置 Harness、W3C、冻结身份/来源、不可变证据、状态分离、发布授权与契约快照边界。
- [ ] 涉及远端身份、副作用、迟到/取消、外部 Score、版本/环境错配时已有反例测试；未交付保证明确标注。
- [ ] 未引入明文 Secret 或不必要的敏感数据。
- [ ] Diff 无任务外修改。
- [ ] 文档 / API / 配置在需要时同步更新。

---

## 10. 最终交付格式

保持简洁，只报告对用户有用的信息：

```text
完成内容
- ...

测试 / 验证
- RED：...
- GREEN：...
- make validate：PASS / 未执行（原因）

影响与风险
- ...
```

---

## 11. 文档分工

| 文档 | 读者 | 写什么 | 不写什么 |
|---|---|---|---|
| `README.md` | 使用 Argus 的用户 | 定位、上手路径、能力概览、部署要点、文档导航 | 仓库结构、CI 细节、代码规范、内部回归细节 |
| `AGENTS.md`（本文） | 本仓库的开发者与 Agent | 仓库地图、质量门禁、架构边界、子系统约束、交付格式 | 用户导览、与开发无关的产品介绍 |
| `CONTRIBUTING.md` | 外部贡献者 | 贡献路径、开发环境、流程、门禁与 PR 要求 | 架构约束的完整解释（指向本文） |
| `SECURITY.md` | 报告者与部署方 | 报告渠道、支持范围、披露流程、部署方责任 | 通用安全科普 |
| `docs/product-boundaries.md` | 产品、设计与开发 | 定位、权威状态、范围准入、保留/下沉/延期与近期验收 | 运行时能力或测试已通过的声明 |
| `TECHNICAL_DESIGN.md` | 设计与开发 | 领域模型、状态机、API 设计的权威说明 | 快速上手 |
| `deploy/langfuse/README.md` | 运维与开发 | i18n 镜像构建、发布与验收 | 全平台架构 |

共同约定：

- 详细 API 以 `docs/openapi.json` 为唯一事实来源，其他文档只引用不复制。
- 文档中的命令、路径、端口与版本必须与仓库实测一致；不确定先运行再写。
- 采用渐进式披露：入口文档给出结论与去处，细节留在对应专题文档，不在入口堆叠。
- 改动任一份文档后，检查是否需要同步其他文档的链接与描述。
