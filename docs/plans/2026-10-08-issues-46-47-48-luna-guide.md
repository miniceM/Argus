# Issue #46 → #47 → #48：Console E2E 补齐与运行说明（Luna 实施方案）

日期：2026-10-08。目标分支：`codex/issues-46-48-console-e2e`。
本方案根据当日 GitHub 正文及当前 checkout 制定，三个 Issue 均为 OPEN、无评论。
用户同时授权依次实施；每个 Issue 经目标验证后创建独立本地 commit，不自动 push、提 PR 或关闭 Issue。

## 1. 问题结论

- **#46**：Launch 筛选已有实现与单测，但缺少「条件 → 请求 → 可见结果集」的 Playwright 闭环。不是已确认业务缺陷。
- **#47**：已存在 `responsive.spec.ts` 的 390/768px 基础检查；但它不检查页面完成加载，Version 详情请求被 mock 成数组，且没有表单提交闭环。补强已有测试，不建立截图基线或重新设计 UI。
- **#48**：配置已有端口覆盖、隔离 SQLite 与禁止服务复用，但贡献文档缺少说明。优先补 `CONTRIBUTING.md` 的门禁章节，不把开发细节塞进用户 README。

## 2. 当前实现与根因

### #46

`services/console/src/features/launches/LaunchesList.tsx` 的 `LaunchesList`：

- 三个 state 分别控制 `agent_id`、`status`、`quality_conclusion`，参与 query key 与 GET 参数。
- Agent 使用文本框 `按 Agent ID 过滤`，不是 Agent 下拉框。
- 空结果渲染「暂无评测记录」及过滤空态说明；重置同时清空三个 state。
- `e2e/launches-list.spec.ts` 专注 #26 表格布局，返回固定响应，不测试服务端筛选。

### #47

- `AgentsList`、`AgentDetail`、`AgentVersionDetail` 和 `CreateLaunch` 已有响应式结构。
- `AgentVersionDetail` 对 `GET /api/v1/agent-versions?agent_id=...&version=...` 期待单对象，现有响应式测试无条件返回数组。
- `CreateLaunch` 从 `/api/v1/evaluators` 加载定义；现有响应式测试未 mock 此端点，间接依赖真实 API。
- 原测试使用 `networkidle` 和全局 overflow 检查，不能证实核心标题、真实数据和操作均正确渲染。

### #48

- `playwright.config.ts`：默认 `18080/18083`；`getPort` 校验 Number 转换后的整数范围 1–65535。
- 两个 `webServer` 都为 `reuseExistingServer: false`；API 用 `/tmp/argus_playwright_e2e_<API端口>.db`。
- `vite.config.ts` 按 `ARGUS_E2E_API_PORT` 配置 API proxy。
- `test:e2e` 仅执行 Playwright；`make console-e2e` 先 build，防止测试陈旧 `dist/`。
- 项目配置的 Chromium 使用 `channel: "chrome"`，环境必须可启动 Google Chrome。

## 3. 目标行为

- #46：每个 Agent/质量条件及交集有确定结果；无匹配无旧行；重置控件与全部结果同步恢复。
- #47：支持 1440×720 桌面、1024×720 窄桌面基线，并保留已有 390/768px 检查；核心标题、主要操作、真实 Version 数据、表单控件可见可操作。长标识/JSON 只在局部滚动或截断，不产生整页横向溢出。正常纵向滚动可完成创建。
- #48：文档可复制命令启动当前 bundle 的隔离 E2E；失败不误判为业务问题，正常结束释放端口。
- 不改变 API、评分口径、Agent SDK 边界、冻结版本或生产数据。

## 4. 推荐解决方案

1. 新增 `e2e/launches-filter.spec.ts`，固定时戳、Agent ID、Launch ID，使用生成的 `components["schemas"]` 类型，不手写重复 DTO。
2. route mock 用 AND 过滤。请求参数与 UI 精确结果分别断言，预期列表手工写明，避免测试 oracle 自己复用过滤算法。
3. 在现有 `responsive.spec.ts` 修正 mock 语义并补 typed fixtures、页面 ready 标志、布局/命中检查及创建提交；不重复 #26 的 Launch 表格密度断言。
4. 在 `CONTRIBUTING.md` 记录端口、build 前置、环境准备、冲突诊断与测试服务生命周期。
5. 此次纯覆盖与文档任务不为制造 RED 改坏正常业务实现；使用临时错误响应/样式扰动验证新增断言确实失败。若发现实际 UI Bug，则先确认失败，再最小修复并复跑。

## 5. 详细实施步骤

### A. #46（第一提交点）

- 新建 typed fixtures：两个 Agent，多条 pass/fail/unknown、不同执行状态的 Launch。
- 注册 mock，按 query 参数匹配结果；等待精确请求与响应，不使用固定 sleep。
- 独立覆盖 Agent、三种质量结论、Agent+status+quality 交集、空态与重置。
- 用 Launch 详情链接的 aria-label 断言完整 ID 集合，额外确认行内 Agent/质量信息。空态必须没有 table 行。
- 临时让 mock 忽略 Agent，验证 Agent case 失败，恢复后目标 E2E/typecheck 通过。
- 检查 diff，仅暂存本方案与该 spec，提交 `test(console): cover launch filter result sets`。

### B. #47（第二提交点）

- 修正版本 API mock：列表请求返回列表，带 version 参数返回单对象；Agent detail 同理。
- 补全 evaluator mock，使用长 Agent ID、endpoint、digest 和 JSON 字符串，均为人工合成数据。
- 原窄屏检查先等正确标题/主内容，不只等 networkidle；保留已有检查。
- 参数化 1440×720、1024×720，检查 Agents 列表 → 管理 → Agent → 查看配置 → Version 返回链接/凭据显示。
- 核心元素先 scrollIntoView，再检查视口范围与中心点命中；校验主要内容之间不重叠，表格/JSON 局部滚动可到达内容。
- 创建表单填名称、环境、Agent、Active Version、Dataset 固定快照、Evaluator 和并发；mock POST，核对完整请求及成功跳转。
- 临时错误版本响应应使 ready 断言失败；临时遮挡主操作应被布局/命中断言发现。恢复后复跑 responsive spec、typecheck、Vitest。
- 本单元必要验证后提交 `test(console): strengthen core responsive layout coverage`。

### C. #48（第三提交点）

- 扩展贡献文档门禁章节，明确默认端口、覆盖范围、禁止复用及 API 隔离数据库生命周期。
- 文档给出仓库根目录 build + 覆盖端口运行命令，和 `make console-e2e` 等价入口。
- 提供 lsof 查询，不建议自动 kill 开发服务；并行测试要使用不同 API/Console 端口对。
- 按文档执行全量 E2E，前后查询两个端口；用无效值验证配置范围错误提示。
- 运行 `make validate`，核对两份契约无 drift；跳过项如 PostgreSQL 必须如实说明。
- 更新本方案执行记录，检查暂存 diff，提交 `docs(console): document isolated Playwright ports`。

## 6. 关键实现说明

- 请求验证需要考虑 React Query 缓存：从空态重置可直接恢复缓存，全量结果恢复是必须条件，不强求重复 GET。
- Agent 文本框 fill 可能产生中间输入请求；等待最终精确参数请求，不假定请求次数。
- mock 只处理预期路径与方法，不让宽泛 glob 吞掉详情/POST。
- Version ready 断言必须包括真实版本、ACTIVE、endpoint 和归档按钮，防止数组响应蒙混过关。
- 布局断言不能依赖每个像素完全一致；允许 1px 舍入误差，允许纵向页面滚动与局部横向滚动。
- POST 使用生成的 `ExperimentLaunchCreateRequest` 类型。成功响应只表示 Console/API 创建契约闭环，不等于真实 Worker/Agent/Langfuse 执行完成。

## 7. 测试方案

| 文件/入口 | 检查 |
|---|---|
| `e2e/launches-filter.spec.ts`（新增） | Agent、pass/fail/unknown、三条件交集、空态、重置结果集合 |
| `e2e/responsive.spec.ts` | 原 390/768px 可达性；1440/1024px 页面基线、长字段、局部滚动、填写提交 |
| `pnpm --dir services/console typecheck` | typed fixtures、请求字段、辅助函数 |
| `make console-test` | 既有组件与领域口径回归 |
| `ARGUS_E2E_API_PORT=28080 ARGUS_E2E_CONSOLE_PORT=28083 make console-e2e` | rebuild 后全量浏览器回归、含真实 API 创建测试 |
| `make validate` | Token、Python、API/schema 同步、Console、Compose |

## 8. 验收标准

全部命令从仓库根目录执行。以下初始为待执行，结果填在文末，不提前称为通过。

- [ ] #46 所有筛选 case 同时验证请求参数和精确可见集合；空态无残留，重置恢复全部记录。
- [ ] #47 四类核心页在桌面/窄桌面均完成渲染；长内容不产生整页横向溢出。
- [ ] #47 form 正常纵向滚动可填写和提交，POST 及跳转正确。
- [ ] 原 390/768px 检查保持，错误 Version 响应不能再假绿。
- [ ] #48 文档端口与变量、整数范围、命令、隔离说明准确。
- [ ] 覆盖端口的完整命令成功，结束后两个端口不再 LISTEN。
- [ ] typecheck、Vitest、完整 Playwright 与 make validate 通过，跳过项明确记录。
- [ ] 三个 Issue 顺序独立本地提交，工作区干净；不自动变更远端 Issue 状态。

## 9. 风险与注意事项

- 当前工作区起点干净、HEAD detached；从现有 HEAD 建上述 codex 分支，不切换到未知远端状态。
- 固定 SQLite 文件会在每次 API 服务启动前删除；不得用测试端口连接开发环境。
- 端口范围不保证可绑定；选择空闲且不受权限限制的端口。正常退出清理进程但不承诺删除 SQLite 文件。
- Mock E2E 不证明后端筛选实现或完整 Worker 链路；已有真实 API 场景和 Python 测试继续回归。
- 不提交 node_modules、dist、测试产物或虚拟环境；不覆盖并行改动，不全量 git add。
- 如需要产品修复，只修改已证实受影响页面且遵循 Token 规范；本计划无数据库迁移。
- 回退按 Issue commit revert，勿 reset/clean 用户数据。

## 10. Luna 执行清单

1. 再核对分支、工作区及 Issue 时效；发现偏差先核实。
2. 执行 A，目标测试与灵敏度检查完成后提交 #46。
3. 执行 B，确保页面真实渲染及提交结果后提交 #47。
4. 执行 C，照文档运行、记录端口释放及完整门禁后提交 #48。
5. 交付文件路径、每个 commit hash、实际通过/跳过项和远端未发布边界。

## 执行记录

### #46

- typecheck、build、Token lint 通过；Vitest 27 文件 / 397 项通过。
- `launches-filter.spec.ts`：5 项通过。
- 灵敏度验证：临时忽略 Agent 参数，Agent case 因期望 3 行而实际 6 行失败；恢复后 5 项再次通过。未修改产品逻辑。
- 环境：Python 3.12.4 虚拟环境；本机 Node 22 缺少 `libsimdutf.31.dylib` 无法启动，使用可用 Node 24.13.0。其余 Issue 与完整门禁待执行。


### #47

- `responsive.spec.ts`：22 项通过（原 18 项保留并补页面 ready 检查，新增 4 项桌面/窄桌面闭环）。
- typecheck、Token lint 通过；Vitest 27 文件 / 397 项通过。
- 灵敏度验证：临时恢复错误的 Version 数组响应，390px Version case 因标题缺少 `1.0.0` 失败；临时遮挡页面，1440px journey 因 `unobstructed: false` 失败。两项扰动均已恢复。
- 首次新增场景暴露的是测试 locator 的 Environment 名称及复制按钮变更名称问题；已按真实 accessible name 修正，不是产品 Bug。无需改动业务 UI。
- Homebrew Node 22 无法启动，但已取得隔离的 Node 22.22.0，最终完整门禁将使用此运行时，未修改系统安装。
