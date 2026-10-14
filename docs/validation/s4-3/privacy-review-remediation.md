# T6 独立审阅：敏感字段与格式化 SSN 修复

核验日期：2026-10-14。此记录对应本地未提交 T6，尚未纳入 PR #181 已发布 HEAD；不构成 T6 或 S4 全量验收。

## 问题与修复

独立审阅发现 `social_security_number` / camelCase 等拆分字段未被敏感字段家族识别，且格式化 SSN 可被 `identifier` 字符串分类接受。修复包含两层：冻结策略拒绝上述敏感字段路径；即使使用 `customer_id` 等安全别名，格式化 SSN（包括嵌入字符串和标量数组）也标记 `REDACTED`，不保留原值。

JSONPath 的 `.identifier` 键语法与 `pii-v1` 字符串内容分类是不同合同。此修复保持合法业务标识符 `synthetic-001` 的既有字符串语义，不改变 JSONPath 语法。未声明能识别任意 PII；获准路径与有限字符串分类仍为主要边界。

## 验证

- RED：新增字段及安全别名回归实测原实现保留原值，7 个用例失败；字段测试最终按既有冻结合同明确断言 `TRAJECTORY_REDACTION_SECRET_PATH_FORBIDDEN`，别名测试断言空投影、`REDACTED`、不完整。
- GREEN：以下隔离 PostgreSQL 定向回归 **177 passed / 0 skipped**，4.87s，覆盖脱敏、参数匹配、HUMAN 及不可变制品存储。

```bash
TEST_POSTGRES_URL='<isolated PostgreSQL URL>' .venv/bin/python -m pytest -q tests/test_trajectory_redaction.py tests/test_trajectory_arguments.py tests/test_trajectory_human.py tests/test_trajectory_evidence.py
```

修复后的 `make validate` 已通过：Python **2169 passed / 0 skipped / 86 real deselected**（122.36s），Console **858 passed**，契约同步、Token lint、typecheck、build 与 Compose 静态检查通过。完整真实集成仍是独立必需门禁。修复前启动的完整真实门禁不用于证明本修复；受影响消费者需补验。整期独立审阅仍在进行，尚未提交或宣称阶段完成。

## 本次修复源文件摘要

| 路径 | SHA-256 |
|---|---|
| `services/eval-runner/app/trajectory/privacy.py` | `d77456abb06fe552a86d41dc4fde38a255ed6e3f8d3d50f2a8b44ddba597aaa1` |
| `tests/test_trajectory_redaction.py` | `fce463c76f8fbf05d5cc1b523d21f4b041b0b554071e6d2dc3a0d6bba6e8ecde` |

## 后续缩写字段修复（尚待最终门禁）

第二组审阅复现 `SSNNumber`、`SSNNUMBER`、`customerSSNNumber`、`SSNValue`、`SocialSecurityNo`、`PANNumber` 的敏感字段路径被错误接受。新增回归 RED 为 **6 failed / 4 passed**；修复 acronym/Pascal 分词及 compact 家族后，脱敏、业务场景、参数、HUMAN、制品回归 **184 passed / 1 PostgreSQL skipped**（4.55s）。这一轮未连接 PostgreSQL，不能替代前一轮或最终存储验收。

真实业务 fixture 新增独立 `privacy_ssn`：安全别名 `customer_id` 只携带格式化 SSN，而不是让银行卡号同时触发整值隐藏。fixture 测试 RED 为 **1 failed / 1 passed**，场景修复后与脱敏测试 **50 passed**；随后增加缩写字段测试。两种框架的真实消费者测试均新增此场景，尚待实测。

本轮 Ruff 与 `git diff --check` 通过。已启动的完整真实门禁有失败，尚未结束；保留完整失败输出进行根因分析，不将其记为通过。

本次源文件摘要（区别于上一小节的修复版本）：

| 路径 | SHA-256 |
|---|---|
| `services/eval-runner/app/trajectory/privacy.py` | `cf14e0e3a91067ccbfee80e439c44a7b4d4a6e1c9ce2e66d4d8339a5dbe67d7e` |
| `tests/test_trajectory_redaction.py` | `df3b720f43d6829f9fd14dec3090e0352912094d7d0026b1ed002757c5ff9f0f` |
| `tests/fixtures/trajectory_agents/business.py` | `207be002fe20ece26da4f7de148443029aab7a8a3701cc1bfdcfd5e84db91045` |
| `tests/test_trajectory_fixture_privacy.py` | `9e64bc79e3a27662d6cef09ced2e69be24292dfca82b2e969771d478af04753d` |
| `tests/test_trajectory_observation_integration.py` | `357301e9fa3af37c7c7bf54d2db31a90450890bfcb49c224b1959cf6bf008a73` |

## 新冻结入口治理与修复后真实消费者

独立审阅确认，新建 Launch 仅使用非参数规则时，仍可提交无 `pii-v1` 的合同并保存原文。新建入口回归取得 **1 failed**（未拒绝），修复后 Manifest 冻结回归 **35 passed / 1 PostgreSQL skipped**；新建拒绝及既有 1.3 无策略合同原样读取回归 **2 passed**。治理检查放在既有相同幂等请求返回之后、新建版本与 Dataset 解析之前，所有新轨迹合同（包含内部 capture-only）必须固定 `pii-v1`；加载历史合同的语义未改变。合成正式规则 fixture 同步采用治理合同，明确旧策略测试现在在新建入口拒绝，不放宽其拒绝要求。受影响规则/sequence/required 回归 **258 passed / 49 PostgreSQL skipped**，需数据库门禁补验。

修复后的真实消费者补验 **3 passed / 81 deselected**（178.81s）：两种框架各 30 次签名来源校准、分别通过银行卡号场景和独立格式化 SSN 场景；原恢复失败用例单独通过。四个隐私场景均确认来源原始哨兵确实存在，且不可变制品、Snapshot、Outbox、诊断 API、Argus 日志记录、生产 Score projector 和实际 Langfuse Score 读取不含原文。元数据证据见 [privacy-consumer-receipt.json](privacy-consumer-receipt.json)，不保存源原始参数或私钥。此轮补验先于新建入口修复，不能替代新入口与最终完整门禁。

修复前完整真实门禁最终为 **85 passed / 1 failed**（909.99s），Pydantic AI before-acquisition 恢复评测结果为空。其默认临时目录被其他短测的 pytest 保留清理删除，尚未确定是否为失败根因；单例通过不证明根因。修复后的完整真实门禁采用独立 basetemp，带 PostgreSQL 的 `make validate` 与有界独立审阅复核也已启动，当前均未记录为通过。

## 同族压缩字段终修与验证纪律

有界复核再次发现 `SSNVALUE` / `SSNFIELD` / `SSNDATA` 及 SocialSecurity ID/Identifier 变体。7 个新增回归 RED（原路径未拒绝）；统一按 SSN 分词前缀和 SocialSecurity 紧凑家族识别后，脱敏及 Manifest 回归 **97 passed / 1 PostgreSQL skipped**（1.68s），Ruff 通过。独立复核正在确认已报告变体的拒绝行为。

此前修复新建入口后的数据库 `make validate` 已为 **2178 passed / 0 skipped / 86 real deselected**（129.12s），Console 等步骤及 Compose 通过；但最后一组家族修复发生于门禁期间，不能将此结果当作最终源码的完整验收。当前已冻结生产源码，重新启动稳定源码的带 PostgreSQL 门禁；先前真实门禁继续等待终止并保留输出，之后才运行稳定版本的完整真实门禁。源码修改可干扰冻结实现摘要；不能将受到修改干扰的失败归因于产品逻辑或临时目录清理，也不能据此记完整阶段通过。

独立审阅者最终有界复核结论：此前报告的缺内容策略新建绕过已由公共 LaunchService 入口阻断，历史加载不变；已报告的 SSN 压缩字段及 SocialSecurity ID/Identifier 家族均在冻结策略前拒绝，包括 `SSNVALUE: "123456789"`。复核为只读直接验证，不是长测试套件通过声明。以上 Critical 修复可关闭；S43-07 仍需补充“单次轨迹内容变化不导致配置不兼容”的直接 Snapshot 比较回归，整期门禁尚待完成。

## 稳定源码与比较覆盖补验

冻结生产及业务 fixture 源码后，`make validate` 已完成：Python **2185 passed / 0 skipped / 86 real deselected**（127.13s）；Console、契约同步、Token lint、typecheck、build、Compose 通过。87 个生产/fixture Python 源文件 SHA-256 复核无变化。

新增 `tests/test_trajectory_comparability.py::test_different_observed_arguments_keep_persisted_snapshots_comparable`，**1 passed**（1.43s）：分别在独立数据库经真实采集、制品持久化与 Snapshot 创建代码生成两个 COMPLETE Snapshot，使用受控 Mock HTTP 来源改变工具参数；两侧 Baseline 资格校验通过，实际读取的参数和轨迹 digest 不同，轨迹合同及三层配置 digest 相同，正式 `assess_comparability` 返回可比且三个维度均 MATCH。本测试只证明冻结比较合同，不将 Mock HTTP 来源计作真实 Reader 准入。未更改生产实现；全量门禁在新增测试之前已完成，最终验证应包含该用例。

上一轮完整真实门禁终止为 **86 passed / 0 skipped**（923.12s）；该轮起始之后发生过最后一组家族修复，仍仅作为历史验证，不作为最终源码版本验收。源码冻结后的完整真实门禁已重新启动；包含新增 S43-07 比较用例的 PostgreSQL 质量门禁也已启动。期间仅补写文档/验证记录，不修改生产或真实 fixture 源码。

包含新增比较回归的最终 `make validate` 已为 **2186 passed / 0 skipped / 86 real deselected**（131.06s），Console **858 passed**、契约同步、Token lint、typecheck、build、Compose 通过；源码摘要再次复核 87 个文件无变化。本轮 PostgreSQL 是专用隔离容器，门禁终止后已停止并自动删除，未操作其他服务。

最终稳定源码的完整真实门禁已终止为 **86 passed / 0 skipped**（942.05s），最终 Console E2E **128 passed**（1.2m）。归档时 87 个生产/fixture 文件仍与冻结摘要一致。最终真实运行中的 60 份治理校准、四个隐私消费者场景、实际 Score ID，以及 Snapshot/Artifact 合同、canonical 摘要、scope 和脱敏策略重新验证结果见 [final-stable-receipt.json](final-stable-receipt.json)。本地阶段门禁完成，发布 CI 待核验；T7–T9 和完整 S4 验收仍未完成。
