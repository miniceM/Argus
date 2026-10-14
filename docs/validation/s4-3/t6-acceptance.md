# T6 参数治理与高级规则验收证据

核验日期：2026-10-14。对应 PR #181 的 T6 增量。**本地阶段门禁通过，发布 CI 待核验**：完整真实门禁、PostgreSQL 质量门禁及 Console E2E 均已完成；前一发布 HEAD 的 CI 不能证明本增量。最终回执见 [final-stable-receipt.json](final-stable-receipt.json)。

## 范围与合同

本期实现 `tool_argument_match`、`max_same_tool_repeats`、`step_sequence`、`require_human_escalation` 四类 opt-in boolean 规则及 `pii-v1` 治理。参数比较精确、类型不转换；可见缺字段是违规，隐藏/未采集/非法值为 UNKNOWN；已有确定违规保留 FAIL。重复与顺序只采用冻结 scope/phase 和已证明因果。HUMAN 只能来自冻结公钥验证的外部签名事件；负例还需要签名闭合清单，工具名及自报文本不能建立人工事件。

新建轨迹 Launch 必须声明内容策略，捕获模式也不豁免；历史加载与相同幂等请求不回写既有合同。不可变制品保存时重新按冻结内容策略验证四类投影，而非仅接受来源的 visibility 或制品摘要。

## 逐项覆盖

| 项目 | 自动化与观测入口 | 必须证明的断言 | 当前证据 |
|---|---|---|---|
| S43-01 | [参数规则](../../../tests/test_trajectory_arguments.py)、既有路径 parser 测试 | 有限 JSONPath；非法/敏感路径、操作符及 literal 在冻结阶段有限错误拒绝 | 稳定质量门禁通过 |
| S43-02 | [参数规则](../../../tests/test_trajectory_arguments.py)、[投影](../../../tests/test_trajectory_redaction.py) | 严格 JSON 类型、Decimal 精确；确定不匹配 false，隐藏/无效/未观察 no_result | 稳定质量门禁通过 |
| S43-03 | 同上 | 明确观察的缺字段与整份未采集区分；前者违规、后者 UNKNOWN；已证 FAIL 优先 | 稳定质量门禁通过 |
| S43-04 | [重复调用](../../../tests/test_trajectory_repeats.py) | 固定身份路径/phase/scope；连续因果；并行或身份缺失 UNKNOWN；一条匹配调用可有界证明；原参数不进入结果 evidence | 稳定质量门禁通过 |
| S43-05 | [顺序](../../../tests/test_trajectory_sequence.py)、[签名 HUMAN](../../../tests/test_trajectory_human_authority.py)、[正式 HUMAN](../../../tests/test_trajectory_human.py)、[真实来源](../../../tests/test_trajectory_observation_integration.py) | 有限 DAG 子序列、不同 Step；可信同 scope HUMAN 与闭合零事件；签名/Root/Trace/workflow/事件集复核；不猜测来源 | 完整稳定真实门禁 86 passed；其中两框架共 60 次治理校准 |
| S43-06 | [摘要隔离](../../../tests/test_trajectory_arguments.py) 的 `test_redaction_and_quality_policy_changes_affect_only_their_own_frozen_identities` | Redaction 改动不改变 mapping，改变 policy/contract/measurement；只改 QualityPolicy 不改变测量身份 | 稳定质量门禁通过 |
| S43-07 | [持久化 Snapshot 比较](../../../tests/test_trajectory_comparability.py) | 两侧 COMPLETE/资格有效；实际参数和轨迹摘要不同；冻结配置相同且三维 MATCH | 1 passed（1.43s）；已纳入最终质量门禁 |
| S43-08 | [隐私真实消费者](../../../tests/test_trajectory_observation_integration.py)、[制品治理](../../../tests/test_trajectory_human.py)、[策略冻结](../../../tests/test_trajectory_manifest.py) | 真实来源先存在合成哨兵；Artifact/Snapshot/API/Argus 日志/Outbox/实际 Score 均不保存或输出原文；拒绝缺策略新建和同族敏感路径 | 最终真实门禁含四个隐私场景及 8 个实际 Score；回执保存出口断言与 ID |
| S43-09 | [typed 投影回归](../../../tests/test_issue_87_langfuse_projection.py)、真实消费者 | boolean 保留 `original_value`；text/无序 category 不投数值；legacy 映射不漂移 | 稳定质量门禁；真实 boolean Score metadata 已读回 |
| S43-10 | [Manifest 兼容](../../../tests/test_trajectory_manifest.py)、[required 资格](../../../tests/test_trajectory_required_evidence.py) | 历史 1.0–1.2 与早期 1.3 不回填；新 schema 显式解析；新建无治理合同拒绝 | 新拒绝 RED→GREEN；历史读取/原 digest/未改写验证通过 |
| S43-11 | `make validate`、`make trajectory-test`、既有 Console E2E、Demo 基线测试 | 契约快照同步、目标/全量/真实门禁、Console、Demo 2/6 与 6/6；CI 对应发布版本 | 最终质量门禁 2186 passed / 0 skipped，Console 858；最终 E2E 128 passed，真实 86 passed / 0 skipped；发布 CI 待核验 |

单元/合同中的受控 Mock HTTP 只证明算法与冻结边界，不算真实 Reader 资格。HUMAN 来源资格和隐私出口另使用实际隔离 Langfuse / OTel / Pydantic AI / 审批系统；不以文件名或纯签名单测替代真实来源验证。

## 可复现命令

```bash
# 已准备的仓库 Python 环境；TEST_POSTGRES_URL 指向隔离测试数据库。
make validate

# 避免其他 pytest 的保留策略清除运行中的真实门禁证据。
s4_test_tmp=$(mktemp -d)
.venv/bin/python -m pytest -q tests/test_trajectory_backend_integration.py tests/test_trajectory_observation_integration.py tests/test_trajectory_rule_integration.py -m trajectory_local --basetemp="$s4_test_tmp"

.venv/bin/python -m pytest -q tests/test_trajectory_comparability.py

ARGUS_E2E_API_PORT=28080 ARGUS_E2E_CONSOLE_PORT=28083 make console-e2e
```

## 版本与观测记录

- [真实治理来源](governed-source.md) 与 [历史治理回执](governed-source-receipt.json)：早期 60 次来源校准及正式四规则/恢复证据，摘要对应其捕获版本，不能当作当前版本指纹。
- [隐私修复全过程](privacy-review-remediation.md)：各次有效 RED、策略收口、历史兼容、门禁干扰及修复后实测。
- [隐私真实消费者回执](privacy-consumer-receipt.json)：60 次校准、四个隐私场景、实际 Score ID 与出口断言，未保存原始敏感参数或私钥。
- 完整真实长测期间曾发生源码变更与默认临时目录保留清理；相应结果是历史诊断证据，不作为最终稳定源码的完整验收。
- 整期独立审阅发现的缺策略新建及敏感字段变体 Critical 已经修复并有界只读复核关闭；S43-07 覆盖缺口已补自动化回归。
- [最终稳定回执](final-stable-receipt.json)：三个最终门禁日志摘要、87 个生产/fixture 源文件 SHA-256、六组各 10 份治理校准、四个隐私场景、8 个 Score ID，以及 8 份 COMPLETE / 4 份 DIAGNOSTIC Snapshot 的身份与制品引用。归档时重新验证合同摘要、轨迹 canonical 摘要、scope/Attempt 归属及冻结脱敏策略；不保存原始参数、凭据或签名私钥。

T7 的 Outbox 租约/重投/断连恢复生命周期、T8 Judge、T9 79+10 全量审计仍待实施，本期出口读取证明不替代这些阶段。
