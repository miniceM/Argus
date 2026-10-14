# Reader 再校准后的完整真实集成

核验日期：2026-10-14。运行期间源码和 HEAD 固定为 `78f1a7cf91e556a73e4b20425a440f1334691485`。

`make trajectory-test` 完整执行三个真实集成模块：**86 passed、0 skipped、0 failed、0 errors，954.935 秒**。JUnit 逐例计数与结果均已核对；未放宽 deadline、容量、归属或规则断言。

本次实测恢复了 Reader 摘要变更后的完整门禁，覆盖真实原生 OTel、Pydantic AI、HTTP-only、采集与恢复、冻结规则及 Snapshot 投影。四个双 revision 场景共保存 **40 个不同实际远端 Score ID**，每个场景两个 Snapshot、10 个 Score，生产 Outbox 已 SYNCED，原 Snapshot 未变。

## 可复现制品

- [运行回执](full-real-78f1a7c-receipt.json)：JUnit 哈希、计数、模块 metadata 哈希及实际投影身份。
- `full-real-78f1a7c-junit.xml.gz`：完整 86 个 testcase，未包含捕获的 stdout/stderr 或 properties；解压后可按回执核对原 JUnit 哈希。
- [源码清单](full-real-78f1a7c-source-manifest.json)：从固定 Git 版本复原的 118 个源码/合同文件哈希；这是运行结束后的版本复原，不冒充运行前采样。
- 三个 metadata gzip：`full-real-78f1a7c-s400.json.gz`、`full-real-78f1a7c-s401.json.gz`、`full-real-78f1a7c-s402.json.gz`。共 239 条记录，含重复轮询/校准记录，不能当成 239 个独立 Case。每份解压后与原 metadata 逐字节相同。

回执去重时排除了 pytest 的 `*current` 符号链接，避免重复计算投影。metadata 由现有 recorder 输出字段统计、受控身份和因果信息，不包含完整业务输入/输出或凭据。

复验使用全新目录，避免覆盖历史证据：

```bash
evidence_dir=$(mktemp -d)
PYTEST_ADDOPTS="--basetemp=$evidence_dir/runtime --junitxml=$evidence_dir/junit.xml --tb=short -x" \
  S4_PYTHON=.venv/bin/python make trajectory-test
```

## 范围与后续

本次运行在同步 `origin/main@b7933f8` 之前完成，不能替代同步后的回归或最终 S4 验收。Judge 的正式执行器、跨 Worker 持久预算、真实模型能力准入与独立 holdout 校准仍待实施。

后续同步提交 `035f40ea17bba51424501ae41d3df52521c6335e` 保留主线比较零计数与 Actions workflow path 修复，无冲突。比较、Gate 和 Cloud 验证器七个目标模块 **143 passed**；该源码版本的 `make validate` 8 步通过，Python **2493 passed、0 skipped、86 real deselected**（181.00 秒），Console **864 passed**，OpenAPI/生成类型、Token lint、typecheck、build 与 Compose 校验通过。它不替代上述固定旧 HEAD 的真实集成版本标记。

对应 HEAD 的首轮 CI 有三个异常 Job；GitHub 注解均报告 Actions 内部错误。已重跑失败 Job，重跑结果须另行核验，异常或跳过不计通过。
