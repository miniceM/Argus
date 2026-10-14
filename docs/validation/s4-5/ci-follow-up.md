# PR #181 CI 失败跟进

核验日期：2026-10-14。关联 #5、#128、#132。

## 固定失败版本

发布版本 `9295236050829c36ff9f31a5da5ecf868767122f` 的 [CI 38526109503](https://github.com/miniceM/Argus/actions/runs/38526109503) 已终止为 FAILURE。

- Full Python Tests：`test_both_snapshot_and_case_are_required[sqlite-query2]` 的共享准备夹具未形成 COMPLETE 必需证据。该夹具在 checkpoint 创建后使用 2 秒真实墙钟采集预算，前置合同冻结与 CI 调度开销也消耗该预算；夹具原先只断言 READY。
- S4 Local Langfuse Integration：HTTP-only serial 的响应到 READY 为 12299.149ms，超过冻结 12 秒截止加 250ms 测试收尾上界。终止时 29 passed / 1 failed，不能记为真实集成通过。
- Code Quality、Docker / Compose、Console Quality & E2E 成功；Cloud 的政策跳过不构成真实 Cloud 通过。

## 单一目的修复

冻结输入夹具只在局部注入既有逻辑 Clock 与 quiet sleep；保留 checkpoint 的原始时间、2 秒合同以及生产采集代码。新增延迟 2.1 秒的准备回归，在 SQLite 和隔离 PostgreSQL 得到 RED 2 failed；修复后相关七模块 GREEN 342 passed / 0 skipped，并提高夹具断言为 READY 且 OBSERVED_COMPLETE。真实 deadline、取消与超时测试继续使用各自原有时钟，未放宽其断言。独立只读审阅未发现阻断问题。

真实测试辅助调度器原先在剩余预算小于 50ms 时仍睡 50ms；改为睡眠不超过 Runtime.remaining()，与正式协调器已有行为一致。新增 13ms 剩余预算的确定性回归：RED 1 failed，GREEN 1 passed。未增加 deadline，未修改 250ms 收尾断言，未改变生产合同、来源资格、完整度或存储预算。

本轮完整本地 `make validate` 8 步通过：Python 2333 passed / 0 skipped / 86 real deselected（163.71s），Console 864 passed，契约同步、Token lint、typecheck、build、Compose 均通过。该次全量 pytest 收集在轮询回归新增之前，因此新增轮询测试以独立 GREEN 1 passed 记录，不计入 2333。三个相关文件 ruff 与 git diff --check 通过。

HTTP-only 真实复验为 1 passed / 66 deselected（139.55s）：10 次独立校准样本保留原 deadline，ROOT_ONLY 和真实制品往返断言通过；另有一次真实 Reader 工厂准入探针。版本、命令、实际 Trace/Item/Attempt、冻结摘要和延迟见 [真实复验回执](ci-http-only-real-receipt.json)。定向复验不替代完整真实集成。

修复已作为 `701cf1a` 推送至 PR #181，新的 [CI 38528223428](https://github.com/miniceM/Argus/actions/runs/38528223428) 已启动；结果以其终态为准。这两个修复不等价于最终 CI 通过。T8 Judge 与 T9 全量验收仍未完成。

```bash
python -m pytest -q tests/test_trajectory_inputs.py tests/test_trajectory_api.py tests/test_trajectory_required_evidence.py tests/test_trajectory_comparison.py tests/test_trajectory_acquisition.py tests/test_trajectory_worker.py tests/test_trajectory_coordinator.py
python -m pytest -q tests/test_trajectory_real_polling.py
make validate
python -m pytest -vv tests/test_trajectory_observation_integration.py -m trajectory_local -k 'reader_qualification_repeated_real_samples and http_only'
```

前三个含数据库的命令使用隔离 PostgreSQL 的 `TEST_POSTGRES_URL`；真实命令使用仓库已准备的 Agent 环境与隔离 Langfuse，连接失败不跳过或回退 Cloud。
