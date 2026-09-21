# Langfuse `zh-CN` Patch Layer

本目录将官方 Langfuse 源码作为唯一业务基线，通过可重放补丁提供 `en` / `zh-CN` 产品界面。它不保存 Langfuse 源码，也不修改 Dataset、Trace、Observation、Experiment、Score、认证回调或业务 API 契约。

## 发布基线

发布清单位于 [`upstream/manifest.json`](upstream/manifest.json)：

- 官方 tag：`v4.38.0`
- commit：`4ecaabed8d9c39d0d3ba483f02ba1cca020388a9`
- Node.js：`24`
- pnpm：`12.4.1`
- Next.js：`16.3.3`
- next-intl：`4.14.5`
- 官方 web/worker 多架构镜像使用不可变 digest；Compose 中 worker 与源码 tag 匹配。
- manifest 保存按序 patch、locale、上游 Dockerfile 和打补丁后 lockfile 的 SHA-256。

`images.releaseWebDigest` 只能在镜像推送后填写。CI 的本地构建会记录 image ID；发布报告必须补充 registry 返回的 digest，不能把本地 image ID 当作可部署 digest。

## Locale 契约

语言解析顺序固定为：

1. 有效的显式 Cookie `langfuse-locale`；
2. 环境变量 `LANGFUSE_UI_DEFAULT_LOCALE`；
3. 安全回退 `en`。

白名单只有 `en` 和 `zh-CN`。非法 Cookie 或部署默认值不会进入渲染上下文。服务端为每个请求独立解析 locale，不使用进程级可变状态；`_app` 与 `_document` 使用同一解析函数，避免 SSR hydration 语言不一致。

语言切换器写入 `SameSite=Lax` Cookie 后刷新当前 URL，因此保留路由、query 和项目上下文。语言与时区独立：切换语言不会改变评测资产、存储值、筛选时间范围或 API 参数。未保存表单在刷新时按 Langfuse 现有页面行为处理。

## 当前覆盖

补丁覆盖高频试点界面的公共渲染入口：

- Sidebar 导航、分组、tooltip、语言切换；
- Projects、Tracing、Sessions、Observations、Datasets、Experiments、Scores、Prompts、Settings、Users、Organization 的页面标题、breadcrumb 和页签；
- 核心表格的常用列头；
- 通用按钮、空态、无结果态和崩溃页。

Trace、Observation、Dataset、Experiment、Evaluator、Prompt 等术语按 [`terminology-allowlist.json`](terminology-allowlist.json) 审查。Trace 输入输出、Prompt 正文、Dataset 内容、用户名称/描述、Score 名称和协议枚举保持原值。

英文资源是源文案 SSOT。翻译资源只存放在 `locales/`，构建时复制到上游 `web/messages/`，不在 patch 中维护第二份翻译。

## 三个验证入口

```bash
# 干净克隆并重放 patch，检查资源、ICU、硬编码文案基线、测试、typecheck 和 lint
make validate-i18n

# 从锁定源码构建最终 web 镜像
make build-langfuse-i18n

# 对已部署的隔离环境执行 Runner/API 与双语言浏览器闭环
make validate-langfuse-integration
```

完整 Next.js 生产构建需要约 12 GiB 可用内存（物理内存与 swap 合计）。CI 镜像任务会额外配置 8 GiB swap；本地 Docker Desktop 需要为构建虚拟机配置足够内存。

## GHCR 发布与部署

PR 只执行补丁验证和可加载的 `linux/amd64` smoke 镜像构建，不推送镜像。相关变更合并到
`main` 或手动执行工作流后，GitHub Actions 使用 QEMU + Buildx 构建并推送
`linux/amd64` 与 `linux/arm64` 多架构镜像到：

```text
ghcr.io/minicem/argus-langfuse-i18n
```

每次发布都会生成不可变的 `4.38.0-i18n-<git-sha>` 标签；`main` 同时更新
`4.38.0-i18n` 标签。工作流 artifact 中的 `release-image.txt` 保存 registry
返回的完整 digest 和 build ID。正式部署必须在环境文件中固定这个完整 digest：

```text
LANGFUSE_WEB_IMAGE=ghcr.io/minicem/argus-langfuse-i18n@sha256:<registry-digest>
```

服务器只拉取成品镜像，不在部署机编译 Langfuse：

```bash
docker compose --env-file .env.remote pull langfuse-web
docker compose --env-file .env.remote up -d --no-build langfuse-web langfuse-worker
```

私有 package 需要先用具备 `read:packages` 的凭据登录 `ghcr.io`；公开 package
可以匿名拉取。工作流只在 `deploy/langfuse/**` 或工作流文件自身发生变化时自动运行，
也可通过 `workflow_dispatch` 手动发布和验收。

`make validate` 继续验证 Argus 原有回归，并增加 manifest/locale 的快速静态检查；它不会下载或构建 Langfuse。

远程验收必须提供：

```text
LANGFUSE_BASE_URL
LANGFUSE_PUBLIC_KEY
LANGFUSE_SECRET_KEY
LANGFUSE_ADMIN_EMAIL
LANGFUSE_ADMIN_PASSWORD
LANGFUSE_PROJECT_ID
LANGFUSE_I18N_IMAGE_DIGEST=ghcr.io/minicem/argus-langfuse-i18n@sha256:<registry-digest>
LANGFUSE_I18N_BUILD_ID
```

缺少任何变量时，集成入口以退出码 `2` 报告 `NOT_RUN`。`LANGFUSE_I18N_IMAGE_DIGEST`
必须使用发布 artifact 中的完整 GHCR digest；`LANGFUSE_I18N_BUILD_ID` 必须与其中的
`argus-i18n-<git-sha>` 相同。验收会读取该 digest 的 OCI config 和正在运行的 Langfuse
服务的 build identity，确认两者相同。私有 GHCR package 还需提供 `LANGFUSE_GHCR_USERNAME`
与 `LANGFUSE_GHCR_TOKEN`（`read:packages`）；公开 package 可匿名查询。凭据只通过环境
变量或权限受控的临时 env 文件传入，不写入 manifest、截图或 Git。

## 门禁如何失败

- `verify-upstream.sh`：repository、commit 或工作区状态不匹配时失败。
- `apply-patches.sh`：逐个执行 `git apply --check`，发生冲突立即指出 patch 文件并失败；半应用目录不能再次作为干净输入。
- `check-i18n-coverage.py`：checksum、key 集、空译文、花括号、插值参数、覆盖率或术语豁免不合法时失败。
- `check-icu.mjs`：使用 next-intl 依赖的 FormatJS parser 验证 ICU 语法。
- `check-hardcoded-ui.mjs`：使用 TypeScript AST 扫描已覆盖范围的 JSX、可见属性、表格字段和 Toast；新增硬编码文案超过审查基线时失败。
- Vitest：验证 Cookie 优先级、非法值回退、请求隔离和切换持久化。
- Storybook + Playwright：加载打过补丁的真实 Sidebar、表格、Dialog 和 locale，生成双语言截图。
- 远程验收：验证 v1 `2/6`、v2 `6/6`、Dataset run URL、核心深链接、双语言并发、刷新持久化和 hydration 错误。

静态扫描仍可能漏掉运行时动态拼接和第三方组件文案，发布时需要结合截图与人工复核。组件 smoke 不代表完整应用 E2E；镜像构建成功也不代表远程运行验收通过。

## 升级上游

1. 创建升级 PR，记录目标官方 release、完整 commit 和官方 web/worker digest。
2. 更新 `upstream/manifest.json` 与 `upstream/VERSION`，将 `Dockerfile` 重新基于该 commit 的 `web/Dockerfile` 生成。
3. 在目标 commit 的干净 checkout 依次尝试旧 patch。任何冲突都人工定位，禁止 `--3way`、模糊跳过或自动提交。
4. 修改上游工作副本中的最小 i18n 差异，按基础设施、导航、核心页面模块重新生成 patch。
5. 更新 locale、术语豁免和经过审查的硬编码遗留基线；重新计算 patch、locale、lockfile 与 Dockerfile SHA-256。
6. 运行 `make validate-i18n`、`make validate` 和 `make build-langfuse-i18n`。
7. 查看原始上游 diff、patch diff、双语言截图与测试报告。补丁可应用不等于语义兼容，不自动合并。
8. 部署到隔离环境，运行 `make validate-langfuse-integration`，将报告与 registry 中的最终镜像 digest 绑定。

## 回滚

同一上游版本内回滚 i18n 时，切回已完成远程验收的官方 web 镜像 digest，并保持相同 worker digest；随后重跑健康检查和 Argus 集成回归。

跨上游版本回滚前必须检查 Langfuse migration、数据库向后兼容性和 ClickHouse/PostgreSQL 备份。存在不可逆 migration 时，先按官方恢复流程还原数据，再切换 web/worker；不得只替换容器镜像。

## 来源与许可

补丁只基于官方 `langfuse/langfuse` 的锁定 commit 编写。未复制 OceanBase Langfuse fork 的代码或非 i18n 业务变更。上游许可证和 NOTICE 随官方源码构建流程保留。
