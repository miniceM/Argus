# Luna 任务说明：Issue #46 → #47 → #48 的价值、目标与验收

日期：2026-10-08。

## 1. 可直接交给 Luna 的任务描述

按照 `docs/plans/2026-10-08-issues-46-47-48-luna-guide.md`，依次复核并验收 Issue #46、#47、#48。

这项工作的价值不是增加测试数量，而是建立三类可信的交付保证：**筛选条件与实际结果一致、核心页面在支持的窄屏下能够完成业务操作、E2E 能安全且可重复地运行而不影响开发环境。**

当前工作已经完成本地实施，PR #116 的分支 `codex/issues-46-48-console-e2e` 交付内容为：

- #46：Launch 筛选结果集 E2E，主要文件 `services/console/e2e/launches-filter.spec.ts`。
- #47：核心页面布局与操作闭环，主要文件 `services/console/e2e/responsive.spec.ts`。
- #48：隔离 Playwright 端口文档，主要文件 `CONTRIBUTING.md` 的门禁章节。

请以文件位置为准定位实现，不要依赖提交 hash：合并采用 squash，历史记录中的临时 hash 无法解析。

**先复核已有交付，不要重新实现已有能力。** 按下文标准检查代码、运行测试并提交验收证据；仅当标准未满足时，先复现问题，再做最小充分修补。以实际可观察结果为准，不能仅凭控件选中、HTTP 200、页面打开或历史测试绿色宣布完成。

保持产品架构与现有契约不变，不扩大为全局 UI 改版、后端筛选重构、评分算法调整或完整 Worker 链路建设。最后给出逐项验收结论、运行证据、已知限制和实际 commit 信息。

> 本说明是执行与验收依据，不是新的测试通过声明，也不自动授权推送、提 PR、合并、部署或关闭远端 Issue。

## 2. 工作价值与成功目标

| 工作 | 价值 | 可观察的成功目标 | 不代表什么 |
|---|---|---|---|
| #46 | 防止检索错位让用户误读 Agent 或质量结果；避免“请求正确但列表错误”的假绿色 | 输入条件、请求参数、精确可见结果集一致；交集、空态与重置正确 | Mock E2E 不独立证明后端筛选实现 |
| #47 | 让核心管理与创建流程在支持的窄屏下仍可完成；防止错误 mock、加载页或遮挡掩盖布局回归 | 页面真实数据已渲染；标题、内容和主要操作可达；创建表单可滚动填写、提交并跳转 | 不承诺所有设备或像素完全一致，也不要求全局视觉改版 |
| #48 | 降低本地排障成本，避免测试误连接开发服务或残留进程 | 从文档复制命令可测试当前源码；端口与隔离说明准确；运行结束释放测试端口 | SQLite 文件保留不等于服务残留；创建 Launch 不等于完整执行链路验收 |

整体成功定义：三个 Issue 的全部必需标准都有当前 checkout 的证据，完整本地门禁通过，未执行项如实披露，工作区与临时扰动状态可解释；没有为了得到绿色而削弱断言、跳过新增用例或扩大业务改动范围。

## 3. 逐项验收标准

### 3.1 Issue #46：验证筛选结果，而不是只验证输入

主要文件：`services/console/e2e/launches-filter.spec.ts`。

| 编号 | 必需标准 | 验收方法及通过证据 |
|---|---|---|
| AC46-01 | 数据确定、契约正确 | Fixture 使用固定标识和时间，至少包含两个 Agent、三种质量结论及不同执行状态；类型来自生成的 API schema，不依赖开发数据库偶然数据 |
| AC46-02 | 单 Agent 筛选正确 | 输入 Agent ID 后捕获最终 GET 的 `agent_id`；断言全部可见 Launch ID 与预期集合一致，且没有其它 Agent 的记录 |
| AC46-03 | 三种质量结论均有覆盖 | 分别选择 `pass`、`fail`、`unknown`；核对 `quality_conclusion` 请求参数与精确结果集合，而非只检查下拉值 |
| AC46-04 | 多条件是交集而非 OR | 同时设置 Agent、执行状态、质量结论；Fixture 必须含只满足部分条件的反例；精确结果排除这些反例 |
| AC46-05 | 无匹配不显示陈旧结果 | 先展示非空结果，再切换至无匹配条件；出现过滤空态及正确说明，原记录与详情链接全部消失 |
| AC46-06 | 重置是状态与结果的共同恢复 | 从有筛选的空态点击重置；三个控件恢复默认，空态/重置按钮消失，全部未筛选记录恢复。允许 React Query 使用缓存，不强求多发一次 GET |
| AC46-07 | 断言可发现真实结果集偏差 | 在可恢复的临时 mock 中忽略 Agent 条件，指定用例必须因结果集合不符而失败；恢复后重新通过。不得把该扰动留在提交中 |

判失败示例：仅断言请求包含参数；使用始终返回固定列表的 mock 验证筛选；组合筛选只确认选中；空态仍残留旧行；预期结果直接复用被测过滤算法而缺少独立 oracle。

### 3.2 Issue #47：页面真实渲染、布局可达、操作能完成

主要文件：`services/console/e2e/responsive.spec.ts`。
核心范围：Agents 列表、Agent 详情、Version 详情、New Evaluation / Create Launch 表单。

| 编号 | 必需标准 | 验收方法及通过证据 |
|---|---|---|
| AC47-01 | 桌面与窄桌面均覆盖 | 明确执行 `1440×720` 与 `1024×720` 的核心页面场景；保留已有 `390/768px` 基础覆盖，不用新增场景替换原场景 |
| AC47-02 | 页面 ready 是业务内容 ready | 布局断言前确认正确标题和核心数据，不能只等待 `networkidle` 或检查无 overflow。Version 详情包含真实版本、endpoint、ACTIVE 对应的归档操作及 Mapping |
| AC47-03 | Mock 不掩盖契约错误 | Agent/Version 列表请求返回列表，详情查询返回正确单对象；Evaluator 目录使用确定性且契约完整的响应，不隐式依赖实际后端返回 |
| AC47-04 | 核心标题与操作不裁切、不覆盖 | 检查标题、注册 Agent、创建版本、查看配置、Version 返回/凭据显示等核心入口；必要时滚动到视口，检查边界、中心点命中与相互不重叠，保留真实点击与后置条件 |
| AC47-05 | 长内容只在设计允许范围内溢出 | 使用长 Agent ID、endpoint、digest、JSON 行；整页与 main 不意外横向滚动，表格/JSON 可局部横向滚动或按设计截断，相关操作仍可达 |
| AC47-06 | 核心管理路径可点击完成 | Agents → 管理 → Agent → 查看配置 → Version → 返回 Agent；每次都验证目标 URL 和目标页真实内容。打开创建版本对话框后可正常取消，不以强制点击代替可达性 |
| AC47-07 | 创建表单可经正常纵向滚动提交 | 在两个基线 viewport 填写名称、Environment、Agent、Active Version、Dataset 固定快照、Evaluator 与并发；并发和底部取消/提交可达；检查实际 POST body 与预期字段一致，并验证成功跳转和详情已加载 |
| AC47-08 | 不把不可用版本误选为可执行版本 | Fixture 保留 archived 版本作为反例；创建表单只提供 active 版本，不因 mock 缺失而退化为无有效版本状态 |
| AC47-09 | 新断言能拒绝假绿色与遮挡 | 临时将 Version 详情响应改为数组时，ready 断言必须失败；临时遮挡主要内容/操作时，命中断言必须失败；恢复后目标测试通过 |

布局标准允许 1px 舍入误差、正常纵向滚动和设计内的局部横向滚动，不要求像素级截图一致。不能通过增大 viewport、去掉长内容、只检查 loading/error 页、`force: true` 或删除几何断言规避失败。

### 3.3 Issue #48：运行说明必须与配置及实测一致

主要文件：`CONTRIBUTING.md`，核对 `services/console/playwright.config.ts`、`services/console/vite.config.ts` 与 `services/console/package.json`。

| 编号 | 必需标准 | 验收方法及通过证据 |
|---|---|---|
| AC48-01 | 默认端口与变量准确 | 文档列出 API `18080`、Console `18083` 和 `ARGUS_E2E_API_PORT` / `ARGUS_E2E_CONSOLE_PORT`；与当前配置一致 |
| AC48-02 | 命令可复制并测试当前源码 | 提供先 build 再 `test:e2e` 的分步命令，或等价 `make console-e2e`；用覆盖端口的文档命令实际运行，不能只做静态阅读 |
| AC48-03 | 不复用开发服务 | 说明 Playwright 自行启动两个 `127.0.0.1` 测试服务，`reuseExistingServer: false`；API 使用按端口区分的 test SQLite，启动前重建。Console proxy 使用同一 API 端口变量 |
| AC48-04 | 范围与占用是不同问题 | 说明端口为 1–65535 的整数，两个服务须使用不同、空闲且有绑定权限的端口；并行测试使用不同端口对。无效值被配置拒绝，合法范围不保证绑定成功 |
| AC48-05 | 排查不伤害现有开发环境 | 提供占用检查方法，优先更换端口；禁止通过改为服务复用或不确认归属就 kill 开发进程解决冲突 |
| AC48-06 | 运行结束不留监听服务 | 运行前后检查指定两个端口。正常结束后无测试 LISTEN 进程；记录 `lsof` 无匹配返回 1 的含义。异常强杀与 SQLite 文件保留单独说明 |
| AC48-07 | 文档不夸大验收边界 | 明确真实 API 创建/Frozen Manifest 场景不代表完整 Agent/Worker/Langfuse 执行完成；不得把静态 Compose 校验写成目标环境部署验收 |

配置加载核验应覆盖两个变量的非法值 `0`、`65536`、`1.5`、`invalid`，以及 `1`、`65535` 的边界值。边界值只需通过 `--list` 检查，不要求绑定特权端口。

## 4. Luna 的执行与验证顺序

### A. 对齐起点与授权边界

1. 在目标仓库读取 `AGENTS.md`、原实施方案、本验收说明及必要的相关 Issue/评论。
2. 记录当前 HEAD、分支与已有改动；确认上述三个提交或等效改动存在。上述 commit 只描述本次已知交付，不能假定另一个 checkout 自动拥有相同状态。
3. 如任务包含线上 Issue 状态复核，实时读取 GitHub；不要把原方案中“OPEN、无评论”的当日观察当作永久事实。
4. 检查 Python 3.12、Node 22、pnpm、依赖与配置所需 Chrome。不要复制前一轮机器特有的 Node 缓存路径；按当前环境选择可用运行时，并记录偏差。
5. 有他人未提交改动时保留它们；不得 reset/clean 或整文件回退来清理临时实验。

### B. 按 #46 → #47 → #48 逐项完成

- 先映射 AC46，再运行其目标测试与灵敏度检查；通过后进入 #47。
- 对照 AC47 检查正确响应、真实内容、几何约束及操作后置条件；运行原场景和新场景，不只运行新增 4 项。
- 对照 AC48 检查文档与实际配置，运行端口边界检查及文档中的全量命令。
- 验证未通过时先分类：产品缺陷、测试/fixture 缺陷、环境限制或范围外问题。附失败证据，禁止直接改预期值制造通过。
- 本任务如果仅复核且全部通过，不制造空提交。确需修补时，在已获实施授权范围内先复现、最小修复，必要验证通过并检查暂存 diff 后按 Issue 建立本地原子提交；push/PR/合并/关闭另行授权。

### C. 执行完整门禁

以下命令从仓库根目录执行：

```bash
# 记录起点
pwd
git branch --show-current
git rev-parse HEAD
git status --short
node --version
pnpm --version
.venv/bin/python --version

# 目标测试先 build，避免对陈旧 dist 验收
pnpm --dir services/console build
ARGUS_E2E_API_PORT=28080 ARGUS_E2E_CONSOLE_PORT=28083   pnpm --dir services/console test:e2e launches-filter.spec.ts
ARGUS_E2E_API_PORT=28080 ARGUS_E2E_CONSOLE_PORT=28083   pnpm --dir services/console test:e2e responsive.spec.ts

# 全量前后检查：lsof 无匹配返回 1，不代表端口残留
lsof -nP -iTCP:28080 -iTCP:28083 -sTCP:LISTEN
ARGUS_E2E_API_PORT=28080 ARGUS_E2E_CONSOLE_PORT=28083 make console-e2e
lsof -nP -iTCP:28080 -iTCP:28083 -sTCP:LISTEN

# 唯一本地质量门禁，已包含 typecheck、Token lint、Vitest 等
make validate

# 确认契约与工作区
# 如起点已存在他人契约改动，按起点比较，不归罪于本任务或覆盖它们。
git diff --check
git diff --exit-code docs/openapi.json services/console/src/api/schema.d.ts
git status --short
```

先检查端口再运行；若 `28080/28083` 已占用，选择新的空闲端口并同步替换验证命令。不要把 `lsof` 命令直接串入 `&&` 链而令“端口空闲”的返回 1 阻止测试。避免在同一 checkout 同时执行会重建 `dist/` 的门禁与运行中的浏览器测试。

端口配置加载检查的单例命令：

```bash
# 应拒绝，且错误明确指出变量和 1–65535 整数范围
ARGUS_E2E_API_PORT=0 pnpm --dir services/console test:e2e --list

# 应可列出测试，不启动或绑定该端口
ARGUS_E2E_CONSOLE_PORT=65535 pnpm --dir services/console test:e2e --list
```

对照 AC48-04 依次测试两变量的全部指定值，记录退出码与必要错误摘要，不把边界值的枚举通过当作绑定通过。

### D. 可恢复的灵敏度检查

临时扰动只用于证明测试能识别偏差，不算产品修复：

1. 保存即将修改文件的当前内容，确保不覆盖他人改动。
2. 分别执行 AC46-07、AC47-09 中的三类独立扰动，每次只改变一个条件。
3. 定向运行对应测试，记录失败断言：结果集合不符、Version 数据未正确渲染、元素被遮挡。
4. 恢复仅此次扰动，再次运行正常目标测试；检查 diff 中没有扰动残留。
5. 不用正常产品逻辑的破坏来制造 RED，也不能仅凭“命令非零退出”判定灵敏度通过——必须确认失败是预期断言而非语法错误或服务启动失败。

## 5. 完成判定与证据要求

### 5.1 逐项结论必须可追溯

Luna 最终提供一张验收表，逐项列出所有 AC 编号，不得只给总 PASS：

| AC 编号 | 状态 | 文件/用例或运行证据 | 未满足原因/补救 |
|---|---|---|---|
| AC46-01 | PASS / FAIL / BLOCKED / NOT RUN | Fixture 定义与契约类型位置 | 无差距写“无” |
| AC46-02 | 同上 | Agent 场景、请求及结果集合断言 | 同上 |
| … | 每个编号独立一行 | 继续列出其余所有 AC，共 23 项 | 不合并为 Issue 总结代替逐项证据 |

- **PASS**：当前 checkout 有直接证据满足该项；代码审查型标准可用准确代码/配置位置证明，行为型标准必须有运行证据。
- **FAIL**：已执行且观察到违反标准的结果；报告失败断言与影响。
- **BLOCKED**：必要环境或输入缺失而无法完成验证；说明解除条件，不能计作通过。
- **NOT RUN**：尚未执行；明确原因，不用历史绿色代替。

仅当三个 Issue 的全部必需 AC 都为 PASS、全量 Console E2E 与 `make validate` 通过，且临时扰动恢复、范围与风险披露完整，才能声明“本次工作完成”。`make validate` 内因 `TEST_POSTGRES_URL` 缺失而跳过的既有 PostgreSQL 检查可作为披露的本地环境限制，不等于 PostgreSQL 集成验收通过；新增目标用例不得跳过。

### 5.2 最终交付必须包含

1. 价值与目标的达成结论，而不是新增文件数或测试数。
2. AC 逐项状态表，以及发现差距时的最小修补说明。
3. 当前分支、起始/最终 HEAD、相关 commit；无新增代码时注明“复核通过，无新增提交”。
4. 命令、退出状态、实际通过/失败/跳过数、运行时版本及可定位的证据文件；若无日志文件，保留必要运行摘要。
5. 三类扰动为何按预期失败、恢复后如何证明正常通过。
6. 全量 E2E 前后端口检查结果；API/schema 无任务新增 drift。
7. 工作区状态、他人改动与临时文件处理结果；禁止把原有修改写成自己产生。
8. 清晰区分本地测试、Mock E2E、真实 API 创建、远端 CI、真实运行链路与目标环境验收。

### 5.3 已有结果只作复核基线

下表为2026-10-08 Luna 复核轮**实际执行**的结果，运行环境：Node v22.22.0、pnpm 10.5.2、Python 3.12.4、分支 `codex/issues-46-48-console-e2e`。后续轮次必须以自己执行的当前证据重新填写，不能直接复制本表宣布验收完成。

| 检查 | 复核轮实际结果 |
|---|---|
| `pnpm --dir services/console build` | PASS（先于E2E，避免陈旧dist） |
| #46 目标 E2E | `launches-filter.spec.ts` 5 项通过（6.9s） |
| #47 响应式 E2E | `responsive.spec.ts` 22 项通过（13.8s）：原 18 项 + 新 4 项 |
| 全量 Playwright | `make console-e2e`（覆盖端口）50 项通过（36.0s） |
| 端口占用 | `28080/28083` 运行前后 `lsof` 均无匹配（rc=1） |
| 端口配置边界 | 两变量 × {0,65536,1.5,invalid} 均被拒并提示 1–65535；{1,65535} 通过加载，共 12 项符合预期 |
| Vitest | `make validate` 内27 文件 / 397 项通过 |
| Python | `make validate` 内 252 passed / 3 skipped；未配置 `TEST_POSTGRES_URL` |
| 本地门禁 | `make validate` PASS（含 ruff、Token lint、typecheck、build、Compose 静态校验） |
| 契约drift | `git diff --exit-code docs/openapi.json services/console/src/api/schema.d.ts` rc=0 |
| 扰动 1（AC46-07） | mock 忽略 `agent_id` → 期望 3 行 / 实际 6 行失败；恢复后通过 |
| 扰动 2（AC47-09） | Version 详情返回数组 → 标题缺少 `1.0.0`（渲染为 `@ARCHIVED`）失败；恢复后通过 |
| 扰动 3（AC47-09） | 遮挡主要内容 → `unobstructed: false` 失败；恢复后通过 |
| 扰动残留 | 两份 spec 与提交版本 `git diff` 为空；`test-results/` 为空 |
| 复核轮改动范围 | 仅新增本验收说明文档（`docs/plans/2026-10-08-issues-46-47-48-luna-acceptance.md`），无产品代码改动；合并 squash 后无独立 commit 可查 |

测试数量是识别意外漏跑的参考，不是写死的产品验收条件。若当前代码新增了合理测试，按当前枚举与运行结果核对；若数量减少，解释原因并证明本任务范围未被遗漏，不能仅沿用历史数字。
