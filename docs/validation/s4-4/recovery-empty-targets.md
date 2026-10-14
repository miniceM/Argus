# T8 接线前置：空重评集合保留成功结果

核验日期：2026-10-14。依据 S4.4 的 recovery prepare/await/finalize 方案；
本切片修复既有同步 recovery 的目标集合语义，不表示正式 Judge 已接通。

## 问题与行为

所有 Binding 的成功结果已持久化、但 Item 收尾状态失败时，重评应复用结果并完成收尾。
旧代码 `only_binding_ids=needed_ids or None` 将空集合转换为 None，后者表示执行全部
Evaluator，造成重复调用并可能覆盖原成功值与 provenance。

现在原样传递集合；冻结合同仍解析与验证，空集合不执行任何 Evaluator。
完整已有结果继续参与原总结和 QualityPolicy，最终落库沿用 generation、lease、取消检查。
不调用 Agent，不更改 Checkpoint、冻结输入、已保存 Snapshot 或预算合同。

## 验证

- RED：新用例运行 **1 failed / 18 deselected**；两个原成功 Binding 均被误调用。
- GREEN：evaluation-only retry、trajectory inputs、rule integration、execution recovery
  四模块 **126 passed / 0 skipped**（132.64 秒）。断言 Evaluator 和 Agent 均零调用、
  execution attempt 仍为 1、evaluation attempt 的 target_bindings 为空并成功收尾，
  完整序列化结果（值、证据、时长及 provenance）与旧记录相同。
- 独立只读复核未发现确认阻断；原 generation/lease CAS 未被绕过。
- Ruff 与 `git diff --check` 通过。
- 在 `b8f7bc318f0d0bce734a80b6f0798e9fcb1ebe59` 加本次代码及测试修改运行
  `make validate`，退出码 0，8 步完成：Python **2559 passed / 0 skipped / 86 real
  deselected**（181.91 秒）、Console **864 passed**，契约同步、Token lint、typecheck、
  build 和 Compose 校验通过。86 项单独真实摄取门禁不计作本次运行通过。

```bash
pytest -q tests/test_issue_84_eval_only_retry.py tests/test_trajectory_inputs.py \
  tests/test_trajectory_rule_integration.py tests/test_trajectory_execution_recovery.py
make validate
```

本次使用隔离 PostgreSQL 17，通过 `TEST_POSTGRES_URL` 指向测试实例。
同步 main 的 #192 回执步骤名修复已纳入验证：此前两个 Cloud 回执/发布器模块
**52 passed**；同步后的完整门禁 **2558 passed / 0 skipped / 86 real deselected**。
GitHub 与本地的 main 合并文件树相同，整合保留双方提交历史。

后续工作按计划继续：正式 trajectory_judge Binding、验证后的准备结果、预算 profile、
Provider/Token 准入、Worker/recovery 异步链路、真实校准和最终验收。
GitHub Actions 在后台运行；继续开发不等待远端 CI，最终验收仍须检查适用结果。
