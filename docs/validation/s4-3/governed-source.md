# T6 受治理来源校准与正式消费者记录

核验日期：2026-10-14。状态：本记录覆盖的验证通过，T6 整期尚未验收。

本记录接续 [签名来源消费记录](human-signed-source.md)。版本以 [receipt](governed-source-receipt.json) 中的基线提交和各实现/测试文件 SHA-256 为准；当前为未提交工作树。完整 S4 和 T6 的统一验收清单保持开放。

## HTTP 与重复调用边界

实际 HTTP 回归初始 **14 failed / 1 passed**：非法 JSON、无效 Trace、未知路由、重复查询参数和空关闭 scope 容量未正确处理。修复后请求在写入任何记录前校验；POST body 限 4096 bytes，GET query 限 128 字符/两个字段，ID 严格非零，连接超时 5s。事件记录与关闭集合共用 1000 个 scope 上限，每 scope 最多 1000 个事件。关闭后新增事件和同 sequence 重放均返回 409，不生成新签名事件。重放另有有效 RED **1 failed**。

HTTP、签发、HUMAN 和回执定向回归 **84 passed**。重复调用规则的真实正式消费者还复现了单个目标调用被不同工具间未证明相邻关系阻断的问题。回归 RED **1 failed**；现仅在完整库存、目标身份/scope/timing 可用、ordering 完整且目标调用唯一时证明其重复上界为 1，不产生新因果边。多个目标调用的相邻证明不足仍为 UNKNOWN。四个扩展规则定向回归 **141 passed**。

## 来源校准

原生 OTel 与 Pydantic AI 各使用同一审批进程内存公钥，在 `approved`、`serial`、`escalate` 三组各执行 10 个独立真实 Attempt，共 **60 样本**。每组版本摘要一致，Trace ID 不重复；每个样本都通过真实分页 Reader、Acquisition 收敛、制品保存/重载与 HUMAN 规则断言。已签 `approved` 有一个 HUMAN；其余两组关闭清单为空，不能由工具名制造 HUMAN。

只有 30 个该来源样本全部通过后，测试控制面才批准对应完整合同、Reader 实现摘要和部署 API 摘要。准入仅限受控 fixture；临时私钥不持久化，新审批进程更换公钥后必须重新校准。此处不替代生产来源的人员授权和语义审查。迟到、HTTP-only 与旧 Reader 校准继续由既有真实门禁覆盖，未据本记录重新宣称全部通过。

## 正式消费者

每个来源执行 `approved` 和 `serial` 两个正式 Launch，同时冻结参数匹配、重复调用、可信 Step sequence、HUMAN 四个 EvaluatorBinding：

- `approved` 的四类结果均为 true，质量为 PASS。
- `serial` 的 HUMAN 结果为 false，其余为 true，质量为 FAIL；证据仍为 COMPLETE。
- Snapshot 保留同一个冻结证据引用；诊断 API 与冻结制品 canonical 往返一致。
- 重评禁止访问业务 Agent 和 Reader，结果值及 evidence 与原测量一致。

两项参数化真实测试 **2 passed / 0 skipped / 65 deselected**，125.46s。四个正式 Launch 各有两个 COMPLETE Snapshot revision，共 **8** 个；receipt 核对其引用和四类结果。测试库共保留 **64 OBSERVED_COMPLETE** 和 **4 PARTIAL** 不可变制品，后者是采集过程的版本，未成为正式 Snapshot 的引用。receipt 只保存 ID、公开 authority 策略、摘要和布尔测量，不保存业务原文、私钥或连接凭据。

## 命令与门禁

```bash
.venv/bin/python -m pytest -q tests/test_trajectory_authority_http.py \
  tests/test_trajectory_authority_publisher.py tests/test_trajectory_human.py \
  tests/test_trajectory_human_authority.py
.venv/bin/python -m pytest -q tests/test_trajectory_repeats.py \
  tests/test_trajectory_human.py tests/test_trajectory_arguments.py tests/test_trajectory_sequence.py
.venv/bin/python -m pytest -q tests/test_trajectory_observation_integration.py \
  -k governed_reader_qualification_and_formal_consumers -m trajectory_local --maxfail=1
make validate
```

最后一条在 `TEST_POSTGRES_URL` 指向新建的隔离 PostgreSQL 17 容器时运行：**2159 Python passed / 0 skipped / 86 real deselected**，125.34s；Console **858 passed**，Token lint、typecheck、build、OpenAPI/生成类型同步和 Compose 校验通过。容器仅暴露回环临时端口，数据位于临时文件系统，运行后已停止并自动移除；未使用现有业务数据库。CI 仍使用其既定 PostgreSQL 16，远端 CI 尚未据本地工作树重跑。

Ruff 和 `git diff --check` 通过。T6 仍需完成敏感哨兵在 Snapshot/API/日志/Outbox/Score 的全链路验证、完整真实回归、必要 Console E2E、独立审阅与逐项验收；本记录不授权关闭 Issue 或合并 PR。
