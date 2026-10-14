# T6 签名审批来源的真实消费记录

核验日期：2026-10-14。状态：部分实施验证通过，T6 尚未验收。

本记录描述签名消费接线时的历史验证。后续 HTTP、重复校准、正式消费者与无 PostgreSQL 跳过的门禁见 [最新增量记录](governed-source.md)。

本记录验证隔离审批 fixture 的来源协议，不证明真实人员已审批，也不授予正式 Reader 准入。审批私钥仅在独立进程内存，readiness 仅公开公钥。两种业务 Agent 均未加入评测 SDK。

## 版本

基线提交为 `594ee8fbf855669c2e40ebdfeb9687464d7e510c`；T6 当前为未提交工作树。随附 `human-signed-source-files.json` 记录测试时相关实现和测试文件的 SHA-256，不能用基线提交单独重现本记录。后续整期交付须记录提交版本及正式校准凭据。

## 命令与结果

在已准备独立 Agent 环境与隔离 Docker 镜像的仓库中运行：

```bash
.venv/bin/python -m pytest -q tests/test_trajectory_observation_integration.py \
  -k 'trusted_human_source or closed_approval_source_without_human_event' \
  -m trajectory_local --maxfail=1
.venv/bin/python -m pytest -q tests/test_trajectory_human.py \
  tests/test_trajectory_evidence.py tests/test_trajectory_authority_publisher.py
make validate
```

- 真实消费：**4 passed / 0 skipped / 61 deselected**，24.45s。
- 定向回归：**71 passed / 1 PostgreSQL skipped**；跳过原因为缺少隔离 `TEST_POSTGRES_URL`，不记作通过。
- `make validate`：PASS；Python **2015 passed / 127 PostgreSQL skipped / 84 real deselected**，Console **858 passed**；Token lint、typecheck、build、契约同步及 Compose 校验通过。
- Ruff 与 `git diff --check` 通过。

## 已验证断言

原生 OTel 与 Pydantic AI 的真实 `approved` 请求各产生一条已签 HUMAN 事件与一致关闭清单。签名绑定实际 Trace、workflow、Observation 和 Runner Root，跨 Root 重放被拒绝。`serial` 与工具 `escalate` 不产生 HUMAN，已签空关闭清单可证明无介入；工具名不能证明人工事件。

来源合同只在所选 profile 冻结公钥及回执字段。审批 HTTP 父节点按精确 SPAN 名称与 business instrumentation 属性映为结构节点。观测通过真实分页 Reader 与 Acquisition 收敛形成制品，保存后重新加载，再用冻结 EvaluatorBinding 得到正例 true、零事件 false。

真实测试曾复现合法签名制品被存储脱敏校验拒绝；单元回归 RED 为 2 failed。修复后存储层重验有限协议、冻结公钥、签名与 scope，排除结构关闭清单的业务事件计数，并校验 HUMAN 完整度。伪造签名、额外字段、错误名称、因果边和 scope 均被拒绝。

## 尚待验收

正式 Reader 校准与来源矩阵、完整治理规则的真实正式 Snapshot/API/Outbox/日志数据最小化、隔离 PostgreSQL 回归、T6 独立审阅及统一验收矩阵仍未完成。审批 HTTP 输入和容量边界还需补齐。不得据本记录关闭 Issue 或勾选 T6 整期验收。
