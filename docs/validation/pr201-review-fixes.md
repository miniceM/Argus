# PR #201 Review 修复验证

日期：2026-10-16。修复基线：`f474516ab33eba8341ddd7ba99b79dd42514d74e`。

## 修复范围

1. Run Token Score：仅在 `usage_coverage == 1.0` 且非 partial 时写入 outbox。API 保留已观测 token 总量与原有 coverage/partial 字段；partial 数值只是下界，不是正式总消耗。
2. Console：partial 优先于数值分支，平均值显示 `—`，展示缺失用例数量与 unavailable reason；分母优先使用同一冻结 metrics 的 `total_cases`，不使用当前已加载列表长度。
3. Run-item 策略：仅未配置时默认 legacy；显式空值、拼写错误等非法配置抛错，不降级。
4. 输入/输出分项：分别校验完整性；任一用例缺少该分项时返回 null，真实零值仍有效。
5. 历史回填：按 limit=500、offset 翻页至短页；非法响应报错；输出 fetched/planned/skips 数量。dry-run 和实际写入共用完整分页结果。
6. 平台 Score：稳定 ID 使用 `platform:tokens_total`，显示名称保持 `tokens_total`，与同名 Evaluator 隔离且重试 ID 稳定。

## 自动化证据

- RED：Python 首轮 6 failed / 23 passed；Console 首轮 1 failed / 860 passed，覆盖 partial 数值、未知分项、非法策略、501 条分页和平台 Score ID。
- GREEN：定向 Python 41 passed，包括缺 Usage 的重试 attempt、同名 Evaluator、dry-run/实际回填 501 条。
- 浏览器回归：`issue-86-comparability.spec.ts`，Chromium 5 passed；新增用例验证 partial 平均值隐藏、2/4 缺失提示和原因。HTTP fixture，不代表真实模型或 Langfuse Cloud 验收。
- `make validate`：PASS。契约同步、token lint、typecheck、Console 861 项单测/build、Python 1374 passed / 7 skipped / 31 deselected、Compose 静态检查均通过。7 项跳过依赖未配置的隔离 PostgreSQL；31 项 trajectory_local 未纳入普通门禁。
- `ruff check services tests scripts/backfill_langfuse_run_items.py` 与 `git diff --check`：PASS。

## 边界

本轮未执行付费模型调用、真实云端回填、push、merge、远端配置修改或 Review thread resolve。
修复不会自动重写历史不可变 snapshot 或撤回已发布 Langfuse Score。平台指标 ID 改用新命名空间后，历史旧 ID 的清理需要独立授权与运维方案，不能宣称已自动去重。
未变更 API 字段或数据库 schema；不引入评测 SDK 到业务 Agent。
