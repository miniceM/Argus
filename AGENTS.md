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

四条硬边界（详见 §2）：

1. 业务 Agent 零 Evaluation SDK；
2. Langfuse 与 Argus 职责分离；
3. `traceparent` 必须传播，正式 Experiment 四个版本必须可复现；
4. `docs/openapi.json` 与 `services/console/src/api/schema.d.ts` 必须同步。

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

**Argus** 是企业级 AI Agent 低侵入评测与质量门禁平台：

```text
Langfuse = Dataset / Trace / Observation / Experiment / Score 的 System of Record
Argus    = Agent Registry / Versioned Launch / Remote Execution / Evaluation Orchestration / Release Gate
Agent    = 普通业务应用，不感知 Evaluation Framework
```

以下边界 MUST 保持：

1. **业务 Agent 零 Evaluation SDK 依赖**
   - `services/demo-agent/` 以及未来被测 Agent MUST NOT 引入 `langfuse`、Argus Eval SDK 或其他评测 SDK。
   - Agent 仅暴露正常业务 API，例如 `POST /invoke`。
   - 评测元数据通过 HTTP Header / W3C Trace Context 传递，不污染业务 DTO。
2. **Langfuse 与 Argus 职责分离**
   - MUST NOT 在 Argus 中重复实现 Langfuse 已成熟的数据模型和分析 UI。
3. **W3C Trace Context 是跨系统链路标准**
   - Eval Runner 调用 Agent 时 MUST 传播 `traceparent`；Agent 内部已有 OpenTelemetry 时继续向下游传播。
   - OpenTelemetry 是可观测性增强，不构成运行基础评测的前置条件。
4. **评测结果必须可复现**
   - 正式 Experiment 必须能确定 Dataset / Agent / Evaluator / Runner 四个版本；禁止只记录 `latest`。
5. **契约快照必须同步**
   - `docs/openapi.json` 与 `services/console/src/api/schema.d.ts` 是同一份 API 合约的两份快照，改动任一侧都必须同步另一侧（见 §4）。

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
- 状态机、并发限流、Attempt 记录改动 MUST 有对应回归测试。

### 5.4 Evaluator / Baseline / Comparison

- 确定性规则能解决时不引入 LLM Judge；引入时必须固定 Prompt 与 Model 版本。
- Evaluator 输出必须可解释：输入、输出、阈值显式定义。
- 质量结论采用 fail-closed / unknown-safe：无 Evaluator 时不得判定通过。
- Baseline 绑定、Run Summary 与 Candidate 对比必须基于冻结版本，不使用 `latest`。

### 5.5 Demo Agent

MUST NOT：引入 Langfuse SDK / Argus Eval SDK、读取 Dataset、自行计算 Score、因为"正在评测"改变业务行为。
MAY：接收标准 `traceparent`、使用常规 OpenTelemetry instrumentation。

### 5.6 Console

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

- [ ] 已阅读相关 Issue、设计评论和现有实现。
- [ ] 验收标准已转换为测试或明确验证项。
- [ ] 遵循 RED → GREEN → REFACTOR；例外已说明。
- [ ] 新行为有自动化测试；Bug 有回归测试。
- [ ] 目标测试通过。
- [ ] `make validate` 通过（含 OpenAPI 与 Console 契约同步）；跳过项已说明原因。
- [ ] 未破坏零 Evaluation SDK、W3C Trace、版本可复现与契约快照同步等边界。
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
| `TECHNICAL_DESIGN.md` | 设计与开发 | 领域模型、状态机、API 设计的权威说明 | 快速上手 |
| `deploy/langfuse/README.md` | 运维与开发 | i18n 镜像构建、发布与验收 | 全平台架构 |

共同约定：

- 详细 API 以 `docs/openapi.json` 为唯一事实来源，其他文档只引用不复制。
- 文档中的命令、路径、端口与版本必须与仓库实测一致；不确定先运行再写。
- 采用渐进式披露：入口文档给出结论与去处，细节留在对应专题文档，不在入口堆叠。
- 改动任一份文档后，检查是否需要同步其他文档的链接与描述。
