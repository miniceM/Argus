# 贡献指南

感谢参与 Argus。本文件只写"怎么把改动交上来"；架构边界、仓库地图与质量门禁的完整规则见 [AGENTS.md](./AGENTS.md)。

## 贡献路径

| 你要做的事 | 从哪里开始 |
|---|---|
| 提新功能或修 Bug | 先开 Issue 描述问题与验收标准，确认方向后再写代码 |
| 小改动（文档、注释、明确修复） | 可直接提 PR，描述里说明动机即可 |
| 改 Langfuse 界面语言 | 见 [deploy/langfuse/README.md](./deploy/langfuse/README.md) |
| 报告安全漏洞 | **不要**提公开 Issue，见 [SECURITY.md](./SECURITY.md) |

仓库目前没有 Issue / PR 模板。提 PR 时请在描述里写清「问题、方案、测试证据、兼容性影响、风险」；没有 Issue 的改动请在描述中补上动机与验收标准。

## 开发环境

```bash
git clone https://github.com/miniceM/Argus.git
cd Argus

python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
pnpm --dir services/console install

make validate   # 唯一本地质量门禁
```

前置条件与完整命令见 [AGENTS.md](./AGENTS.md) §4。简要版：Python 3.12、Node 22、pnpm，可用时另需 Docker。

## 不可破坏的边界

1. 业务 Agent 零 Evaluation SDK 侵入；
2. Langfuse 与 Argus 职责分离，Argus 不重复实现 Langfuse 的数据模型与分析 UI；
3. W3C `traceparent` 必须传播，正式 Experiment 必须冻结 Dataset / Agent / Evaluator / Runner 四个版本；
4. `docs/openapi.json` 与 `services/console/src/api/schema.d.ts` 必须同步。

违反上述边界的 PR 不会被合并，即使测试通过。

## 开发流程

```text
Issue / Design → RED → GREEN → REFACTOR → 目标测试 → make validate → CI / E2E
```

- **先写测试**：功能与修复先写能表达需求的测试，确认它真的失败，再做最小实现；Bug 必须有回归测试。
- **同步契约**：改动 FastAPI 路由或请求 / 响应模型后执行 `python scripts/export_openapi.py`；改动 OpenAPI 后执行 `pnpm --dir services/console api:generate`，两个快照都要一起提交。
- **不要改回归基线**：Demo 回归基线（Agent v1 `2/6`、Agent v2 `6/6`）由 `tests/test_expected_pass_rate.py` 与 E2E 保护。业务预期确需变更时，在 PR 中说明原因。
- **不要夹带无关重构**：一次提交只做一件事。

## 门禁

提交前必须让 `make validate` 通过（8 步，含 OpenAPI 快照、Console 契约同步、typecheck、Vitest、pytest 与 Compose 校验）。Console 改动额外要求：

```bash
pnpm --dir services/console lint:tokens   # 设计 Token 约束
make console-test
make console-e2e
```

Console UI 改动请先阅读 `.agents/skills/argus-design-system/SKILL.md`。

### Playwright E2E：隔离服务与端口

从仓库根目录运行。先完成上述 Python 虚拟环境和 Console 依赖安装；浏览器项目使用 `channel: "chrome"`，本机需有可启动的 Google Chrome。配置来源为 `services/console/playwright.config.ts`、`services/console/vite.config.ts` 和 `services/console/package.json`。

| 测试服务 | 默认端口 | 覆盖变量 |
|---|---|---|
| Eval Runner API | `18080` | `ARGUS_E2E_API_PORT` |
| Console Preview | `18083` | `ARGUS_E2E_CONSOLE_PORT` |

端口值必须为 **1–65535 的整数**，否则配置加载时会报错。有效范围不代表端口可用：两个端口应不同、空闲且有绑定权限；并行运行不同 checkout 时，各自使用不同的端口对。

推荐入口会先构建当前源码，再启动测试，避免误测旧 `dist/`：

```bash
ARGUS_E2E_API_PORT=28080 ARGUS_E2E_CONSOLE_PORT=28083 make console-e2e
```

等价的分步命令（`test:e2e` 自身不会 build）：

```bash
pnpm --dir services/console build
ARGUS_E2E_API_PORT=28080 ARGUS_E2E_CONSOLE_PORT=28083 pnpm --dir services/console test:e2e
```

Playwright 会自行启动绑定于 `127.0.0.1` 的 API 和 Console Preview，**不会复用开发者或 Compose 已运行的服务**（两者均设置 `reuseExistingServer: false`）。API 以 test 模式使用独立 SQLite 文件 `/tmp/argus_playwright_e2e_<API端口>.db`，启动前会删除同名测试数据库。部分场景会创建真实 Launch；不要让测试连接开发环境，也不要把服务复用改成 `true` 来绕过端口冲突。Console 的 API proxy 同步读取 `ARGUS_E2E_API_PORT`。

端口冲突时先检查占用者，改用空闲端口，不必停止已有开发服务。在 macOS / 安装了 `lsof` 的 Linux 上可以运行：

```bash
# 检查默认端口是否被 Compose 或其它服务占用
lsof -nP -iTCP:18080 -iTCP:18083 -sTCP:LISTEN

# 按覆盖命令测试后，检查测试服务是否已退出
lsof -nP -iTCP:28080 -iTCP:28083 -sTCP:LISTEN
```

正常测试结束（包含断言失败）后，Playwright 会停止自己启动的服务；第二条检查应无 LISTEN 输出（`lsof` 无匹配时返回 1）。SQLite 文件可能保留，不代表服务仍在运行。若进程被强制杀死或机器异常，先通过 `lsof` 核对残留进程归属，再处理；不要误杀开发服务。

隔离真实 API 场景验证 Console/API 创建与 Frozen Manifest，不等于完整真实 Agent → Worker → Langfuse 执行验收。

## Commit 与 PR

- 分支建议：`codex/<简短描述>`（其他前缀也可以，但请保持短且语义清晰）。
- Commit 使用 Conventional Commits：`feat` / `fix` / `test` / `refactor` / `docs` / `chore`。
- PR 描述包含：问题、方案、测试证据（命令与结果）、兼容性影响、风险。
- PR 必须通过 CI 的 5 项检查：Code Quality、Full Python Tests、Docker / Compose Validation、Console Quality & E2E、Langfuse Cloud E2E。

## 安全与数据

- 不要提交真实 Token、密码、Cookie、API Key 或数据库凭证；`.env.poc` 仅允许演示凭据。
- 不要在日志、Trace 或测试 Fixture 中写入真实 PII。
- 演示与 E2E 使用的 Langfuse Key 应通过 GitHub Environment / Repository Variables 配置，外部 fork PR 默认不获得密钥。

## 许可

本仓库的许可协议尚未确定。在添加 `LICENSE` 之前，如果你对代码有版权顾虑，请先开 Issue 讨论，不要直接提交 PR。
