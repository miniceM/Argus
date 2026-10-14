# Worker 假 Reader 测试时钟与 CI 跟进

核验日期：2026-10-14。本记录覆盖测试修复，不构成 T8 或 S4 整体验收。

## 失败与修复范围

远端 `01c1c5d374ded8e5fb21ee10258a9b924041b45f` 的
[CI 38544002251](https://github.com/miniceM/Argus/actions/runs/38544002251)
中，Code Quality、Docker/Compose、Console Quality & E2E、S4 Local Langfuse
Integration 成功；Full Python Tests 为 **2457 passed / 1 failed / 86 deselected**，
整体 CI 失败。Cloud Job 仅执行无凭据摘要，Main Cloud Job 跳过，不计真实 Cloud 验收。

失败用例 `test_worker_collector_qualifies_after_reservation_and_preserves_original_deadline`
使用假 Reader，却让 fixture 准备和 CI 调度消耗原 2 秒预算，导致重负载下返回 PARTIAL。
只在该用例内注入受控时钟，并参数化 0 / 2.1 秒准备延迟；保留原 checkpoint、
2 秒 deadline、准入发生在数据库预约之后、READY/OBSERVED_COMPLETE 及提交断言。
生产采集实现、真实集成和独立截止时间过期测试不变。

## RED / GREEN 与门禁

- RED：原实现增加 2.1 秒准备延迟后，**1 failed / 1 passed / 25 deselected**；
  延迟场景无法满足 OBSERVED_COMPLETE。
- GREEN：Worker、acquisition、coordinator、inputs 四模块，
  **161 passed / 0 skipped**（26.44 秒）；测试文件 Ruff 通过。
- 完整门禁：基于预算提交 `6ec7a445e8ac523b70bae5c6896154d8cb1c55dc`
  加本次测试修改运行 `make validate`，退出码 0，8 步完成；
  Python **2556 passed / 0 skipped / 86 real deselected**（176.55 秒），
  Console **864 passed**；契约同步、Token lint、typecheck、build、Compose 校验通过。
- 独立只读审阅未发现确认阻断；审阅不替代上述实际测试。

使用隔离 PostgreSQL 的标准复现命令：

```bash
TEST_POSTGRES_URL=postgresql+psycopg://postgres@127.0.0.1:62768/postgres make validate
```

端口须替换为测试实例实际监听端口。该命令不执行单独的真实摄取门禁；
86 项真实测试被明确 deselect，既有真实运行按原 SHA 保留。
修复推送后的新 HEAD CI 尚须重新取得结果，不能沿用旧 HEAD 的成功 Job 作最终验收。
