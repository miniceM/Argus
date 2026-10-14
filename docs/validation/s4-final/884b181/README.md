# S4 当前版本真实集成与逐项审计入口

核验日期：2026-10-14。实际测试提交：
`884b1810c68b3701877209dfdfb57f1eefd1a34b`，关联 PR #181。

本目录新增当前提交的证据，保留此前各版本记录。
**真实集成通过不等于完整 S4 或 Issue #131 完成**。
Judge 剩余实施与准入见[维护者交接](../../s4-4/maintainer-handoff.md)；
原始 79 项分期和 10 项整体验收标准保持不变。

## 实际运行

| 门禁 | 结果 | 范围与限制 |
|---|---|---|
| `make trajectory-test` | 86 passed / 0 skipped / 0 failed / 0 errors；退出码 0；JUnit 961.09 秒 | 隔离本地 Langfuse、原生 OTel 与 Pydantic AI、HTTP-only、迟到/分页、恢复、正式规则消费者及实际投影 |
| `make console-e2e` | 129 passed；退出码 0；约 1.2 分钟 | 浏览器交互含受控 API 响应；不能替代真实后端采集或真实在线 Trace 打开 |
| 四类工具规则专项 | 68 passed；退出码 0；1.51 秒 | 确定性合成证据，独立保存逐测试 JUnit |
| required/optional 资格专项 | 98 passed；退出码 0；21.36 秒 | SQLite 和隔离 PostgreSQL、Snapshot/Baseline/Gate、事务回滚与历史兼容 |
| 固定 Snapshot API 专项 | 50 passed；退出码 0；16.39 秒 | 身份隔离、不可用原因、冻结读取和精确数值 |
| 轨迹诊断 Vitest 专项 | 11 passed；退出码 0 | 保存逐测试 JSON；迟到响应、缓存隔离、三态与双向定位 |

此前 Provider 选项切片的完整 `make validate` 为 Python 2658 passed /
0 skipped / 86 real deselected、Console 864 passed、8 步门禁通过。
本目录的真实测试补齐独立入口；不把被排除的真实测试当作此前门禁已执行。
该次门禁详情见[Provider 选项记录](../../s4-4/provider-options-progress.md)。

复现命令（依赖和镜像须先按仓库说明准备）：

```bash
evidence_dir=$(mktemp -d)
PYTEST_ADDOPTS="--basetemp=$evidence_dir/runtime --junitxml=$evidence_dir/junit.xml --tb=short -x" \
  S4_PYTHON=.venv/bin/python make trajectory-test
make console-e2e
python -m pytest -q tests/test_trajectory_rules.py
TEST_POSTGRES_URL="<隔离 PostgreSQL URL>" python -m pytest -q tests/test_trajectory_required_evidence.py tests/test_trajectory_api.py
pnpm --dir services/console exec vitest run src/features/launches/__tests__/CasesTraceTab.trajectory.test.tsx
```

## 真实观测和归档

- [真实运行回执](real-receipt.json)与 [JUnit](real-junit.xml.gz)：86 个实际执行节点，
  无失败、错误或跳过；日志摘要显示 961.10 秒，JUnit 精确总时长 961.09 秒。
- [源码指纹](source-manifest.json)：126 个生产/迁移/夹具/合同文件，
  在测试运行期间捕获，退出后逐文件 SHA-256 复核一致。
- [执行身份回执](execution-identities-receipt.json)与
  [冻结版本摘录](execution-identities.json.gz)：只读提取本次隔离数据库的
  231 个 Launch，保存 Dataset/Agent/Evaluator/Runner、Binding 及策略摘要，
  不导出业务 payload。**实际 `runner.build_id` 为测试默认值 `test-build-001`**；
  不重标为当前 Git SHA，也不将本次运行当作正式构建准入证明。
  后续由管理员让真实测试入口显式传入当前 Git SHA 并补验。
- 三个模块的字段矩阵分别归档为
  [s400](s400-field-matrix.json.gz)、
  [s401](s401-field-matrix.json.gz)、
  [s402](s402-field-matrix.json.gz)。
  共 239 条记录，含重复采样、校准和查询观测，**不是 239 个独立 Dataset Case**。
- 四个双 Snapshot 投影场景保留 **40 个不同的实际远端 Score ID**。
  回执逐场景保存 Snapshot/Score 关系；同 generation、生产 Outbox `SYNCED`、
  旧 Snapshot 原文不变均有实际断言，不以旁路上传替代生产投影。
- 检查本次 99 个日志/矩阵/回执文件（含 Agent 子进程日志），
  未发现所列凭据、认证值、URL 密码和合成敏感哨兵模式。
  字段矩阵生成器只保存字段类型/存在率、ID 和关系，不保存原始参数或响应。
  此为有界扫描和数据最小化审查，不是任意秘密的通用检测证明。
  新增执行身份摘录及其回执另做有界扫描，不计入上述 99 个文件。
- [Console 回执](console-e2e-receipt.json)保存当前三份截图及哈希；
  手机诊断截图已人工查看。

## 逐项验收仍在核对

`acceptance-audit.json` 保留全部 89 个原始 ID、标准来源、逐项断言与制品引用。
`PASS` 仅用于已核对当前执行证据的对应范围。
`NOT_RUN` 表示该项最终审计尚未完成，不否定历史测试，也不等于实现缺失。
`BLOCKED` 表示仍依赖维护者完成 Judge；混合项的确定性部分可以已有证据，
但完整项仍不计通过。

以下剩余数据验证按用户更新原则交管理员，不再由当前代理启动：

- S40-01 的宿主 pytest 全局断网约束：现有 Agent socket guard、本地 Docker、
  loopback 与清理 Cloud 配置已验证，未声称本次宿主系统全局断网。
- S40-12 的类别矩阵：通用字段矩阵保留 metadata 存在率/类型和 phase 分组；
  status、visibility、HUMAN 来源等真实断言还需逐类别对齐，不能当作原始 DTO 已归档。
- M5-06 的真实在线 Trace 打开，与受控浏览器链接测试分别核验。
- 其余逐项审计、适用 CI/Cloud 发布证据，以及维护者负责的 Judge 依赖。

GitHub Actions 在后台运行；本地开发与证据核验继续推进，不等待 CI。
当前代理只继续静态检查及交接材料整理；所有后续需要数据的测试、
校准和验收见[管理员交接](../administrator-validation-handoff.md)。
本次同 HEAD 的 [CI 状态快照](ci-snapshot.json) 中，Code Quality、
Docker/Compose、Console Quality & E2E、S4 Local Langfuse Integration 已成功；
Full Python Tests 与 Cloud Integration Policy 尚在进行。
i18n 补丁和镜像构建成功，Main Cloud / remote acceptance 跳过，不计真实发布验收。
此快照不代表后续文档提交或完整 CI 通过。
