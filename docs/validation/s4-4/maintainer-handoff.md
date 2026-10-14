# Issue #131 维护者交接

日期：2026-10-14。按用户调整，#131 的剩余实施由维护者完成。
本轮保留 PR #181 已提交实现，
并保留已开发的 Provider seed/thinking 保护切片；其余 S4 工作继续。

本说明只调整实施责任，**没有修改原 S44、S45 或 M5 验收要求，也没有把缺失证据计为通过**。
Issue #131 保持开放；母 Issue #5 和 #132 的原始 Judge 依赖未因此完成。

## 已有成果

- 严格配置、输出解析、Step/scope 校验、冻结 Prompt/Model/实现/策略与统计函数。
- 实际 Checkpoint/Artifact 准备入口签发 seal，最小脱敏输入与权威 criteria 来源核验。
- 有界、可取消的 OpenAI-compatible HTTP，未知 usage 不补零。
- PostgreSQL 预算预留/SENT/结算、租约接管、取消清理及未知已发结果保守记账，
  迁移 020 追加 nullable `judge_budget_state_json`。
- 显式不可变 Registry/version、Binding 与异步准备结果/同步 typed 汇总桥接。
- QualityPolicy 默认 `== PASS`，diagnostic optional、release required，
  diagnostic 必要规则和未获准 critical 配置在冻结边界拒绝。
- 固定 Qwen3.8 模型文件与两个格式/tokenizer 探针的真实制品；
  两请求各重复一次，不是独立人标校准集。
- 已开发的有限 seed/thinking 参数与降级保护，见
  [本轮 Provider 选项记录](provider-options-progress.md)。

## 维护者仍需完成

1. 固定真实 Provider deployment 和模型制品身份，冻结所有影响实际请求的选项，
   验证结构化输出能力；目录 ID、HTTP 200、接口 Schema 声明不能替代准入证明。
2. 获准 tokenizer/模板对实际请求进行有界计数，冻结 profile 与全量 cohort 的最坏预算；
   不将两个格式探针的计数或字节估计当作通用 Token 上界。
3. 正式配置 Writer/Registry 准入和 Launch Manifest 写入，拒绝客户端伪造资格。
4. Worker 短事务 prepare → 释放 DB 锁 → async await → typed 汇总 → generation/lease CAS；
   deterministic trigger、diagnostic 定值采样与 release 全量覆盖分别验证。
5. 异步 evaluation-only recovery；空目标不请求 Provider，成功值原样保留，
   Agent 零重调用，迟到旧 generation 不写质量结果。
6. 独立人标 development/holdout、near-duplicate 隔离、风险切片样本和 repeat；
   Wilson 单侧上界、误放行与分歧率报告，阈值不满足为 UNKNOWN。
7. 同风险确定性 + Judge 或独立双 Judge 的真实来源及联合校准，
   不以改名、相同 Prompt/Model 的重复调用充当第二来源。
8. 补齐 S44 全部、S45-08 Judge 阶段 B、M5-08 Judge 覆盖部分、
   M5-09 Judge 准入部分及其余相关最终验收证据。

## 继续保持的运行边界

现有 Judge 的 `release_eligible=false`，原因 `JUDGE_ADMISSION_UNAVAILABLE`。
不默认启用、不绕过 Release Gate、不放宽 required UNKNOWN 或 critical 阻断。
人工校准、正式 Profile 和 Worker/recovery 未完成，不能宣布 #131 或完整 S4 已完成。
原始 79+10 矩阵保留，相关项标记交接/待验，不删除标准或计为 skip PASS。

当前代理继续 #127–#130、#132 确定性部分及最终版本的真实链路、Console、契约、
投影/恢复与证据核验；不等待 GitHub Actions 才推进。
本 PR 和文档提供可评审交接材料。
