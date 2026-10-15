# Issue #183：同环境正式基线本地验收

日期：2026-10-15（Asia/Shanghai）。方案：`docs/plans/2026-10-14-issue183-environment-active-baseline-luna-guide.md`。

## 身份与授权边界

- 基于 `origin/main` 的 `9155d035f4eae3a2352e72bc9244723f19d570d1` 创建独立分支 `codex/issue183-environment-baseline`。
- 实现提交：`2c1c77d`（环境迁移/冻结）、`6696bfe`（绑定/选择）、`d51937a`（比较/Gate）、`25b19b0`（API 聚合）、`89e8a2e`（Console）、`6353c0a`（集成验收）。本记录的后续 docs 提交不改变功能。
- 原分支及无关未跟踪方案保留；仅本地提交。没有 push、PR、merge、发布或远端配置变更。
- 本地 Python 3.12、Node 24.13.0、pnpm 10.5.2；CI Node 22 未在本轮远端复测，不能用本地通过替代远端 CI。

## 行为与验收映射

| 标准 | 本地结果 | 主要证据 |
|---|---|---|
| AC01 管理列与迁移 | PASS | `test_issue183_launch_environment.py`：SQLite 与真实 PostgreSQL 回填、事务回滚、receipt、幂等和 JSON 不变 |
| AC02 创建冻结 | PASS | 同文件两创建入口、规范化及自定义环境；真实 API E2E 创建 test/qa-blue |
| AC03 同环境绑定 | PASS | `test_issue183_baseline_environment.py`：匹配、跨环境、缺失和非法冻结环境 |
| AC04 显式旁路 | PASS | 同文件自动/显式 Candidate 拒绝无效 Binding，原无 Binding 回归保留 |
| AC05 比较与 Gate | PASS | `test_issue183_comparison_environment.py`：总览/Case、UNKNOWN-safe、旧缓存与新引擎幂等 |
| AC06 不补造证据 | PASS | 迁移 JSON 不变、管理默认值不提升旧快照、严格冻结环境读取 |
| AC07 历史修复 | PASS | HTTP INVALID/reason/真实 revision；CAS 修复、Console 警告与真实 revision 提交 |
| AC08 原生过滤 | PASS | HTTP 四条件交集/空集/分页/非法参数；真实 API 环境过滤，Console 组合与重置 E2E |
| AC09 精准聚合 | PASS | `test_issue183_launch_baseline_projection.py`：精确 Snapshot 指向、未知环境 false/null、生成契约 |
| AC10 冻结/动态分离 | PASS | 恢复 Launch 不误消徽标；Binding 切换更新投影、既有 Candidate 引用不变；单条及动作回归 |
| AC11 无基线探测 N+1 | PASS | 1/50 个不同 Launch 均新增 1 条聚合 SQL；列表/筛选/刷新无 Baseline GET。既有 progress SQL 未优化，不宣称列表总 SQL 常数 |
| AC12 Console | PASS | 列表/Modal 单测、失效绑定 CAS、四条件/重置 E2E；360/390/1280 环境控件边界与键盘；原 64px 行高断言保留 |
| AC13 契约/文档 | PASS | OpenAPI/schema 同步门禁、正常 fixtures 补齐；`TECHNICAL_DESIGN.md` 同环境与历史修复语义 |
| AC14 自动化 | 见下表 | 本地与部署/远端证据分列，不以历史绿色代替 |

## 自动化记录

按 S1→S5 实际先跑 RED：分别 9、6、5、3、4 个失败，之后最小实现转绿。
正常 fixture 的旧字段缺失在回归中修复；缺失冻结证据的负向测试保留。

| 命令/层级 | 结果 | 边界 |
|---|---|---|
| 4 个 `tests/test_issue183_*.py`，配置隔离 PostgreSQL | **32 passed** | PostgreSQL 17 本地任务专属容器，UUID schema 创建/销毁；不是生产迁移 |
| 全后端 `pytest -q tests -m "not trajectory_local"` | **1247 passed / 31 deselected** | 使用隔离 PostgreSQL，S4 trajectory_local 专项未运行 |
| `pnpm --dir services/console typecheck` | PASS | 生成类型未手写替代 |
| `pnpm --dir services/console lint:tokens` | PASS | 语义 Token |
| `make console-test` / 最终 validate Vitest | **854 / 856 passed** | 最终 53 文件，包括新增环境解析 2 项 |
| `make console-e2e` | **129 passed** | 独立 SQLite/Runner/Vite，端口 28080/28083；真实 API 用例无 mock，其他用例按文件使用 mock |
| `make validate` | **PASS，8/8** | 8 步，契约 drift 不跳过 |
| `git diff --check`、ruff | PASS | 最终提交前复核 |
| 生产 DB 历史 Binding 审计/迁移 | **NOT RUN** | 未连接部署数据库，未修复任何生产 Binding |
| 远端 CI、Cloud、部署验收 | **NOT RUN** | 无推送/发布授权，本地结果不证明远端成功 |

最终功能验收对应 `6353c0a`（本地后端与 Console 同一 checkout），随后仅编辑设计和本验收文档。
测试日志保存在本机 `/tmp/issue183-{target-final,validate,e2e-acceptance}.log`，非远端 CI receipt；
任务专属 PostgreSQL 容器测试后删除，不保留临时数据库。现有 deprecation 与 bundle-size warning 未作为失败，未降低门禁。

## 迁移与运维注意

1. 发布前先在部署库副本审计历史 Manifest 来源。迁移只规范管理列，不能修复历史正式证据。
   `comparison.environment` 非空优先，其次冻结 `agent.environment`，否则管理默认 production；
   非法非空值使整个 `018` 回滚。保留异常 Launch ID，审计来源后另行决定修复，不吞错登记成功。
2. 历史 Binding 环境不兼容时 GET 仍保留真实 revision 与诊断；新 Candidate、比较及 Gate fail closed。
   应在目标环境重新评测，用合格 Snapshot 和实际 revision 替换，409 后读取新状态再确认。
   禁止删除旧 Binding 重置 revision、修改 Snapshot 环境或用管理列补证据。
3. 后端 API 与 Console 契约同步部署；客户端不得从 Launch 管理环境推导 Snapshot 资格。
   `release-gate-v4` 不复用 v3 PASS，旧 GET 只保留审计事实；恢复旧宽松引擎不是安全回滚方案。
4. 迁移追加，不改已发布迁移；回滚应用前评估新环境边界和新增列兼容性，保持快照不可变。
5. 本轮不改变业务 Agent、Langfuse 职责、Trace Context、质量阈值或 Demo 2/6、6/6 回归预期。
