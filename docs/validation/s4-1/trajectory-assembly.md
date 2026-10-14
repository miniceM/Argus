# S4.1 冻结轨迹组装

关联 #128，依赖 PR #167；核验日期 2026-10-13。

`assemble_trajectory(scope, observations, contract)` 消费已绑定批次，复用 Root 图与显式因果校验，返回现有 `StandardTrajectory`。它只表达观测证据，不运行规则、Judge 或质量门禁。公开 DTO 与生产调用签名不变。

## 完成协议与能力

- 每个业务 workflow 必须有唯一、有效的终结 inventory，准确覆盖自身在内的全部 event ID；事件/序号唯一，序号从 1 连续到 final，terminal 自身位于 final。缺号、额外/缺少事件、冲突、无效声明或未知身份均不能由 quiet window 补足。
- Root 与获准结构包装不参加业务 inventory，不把其 NONE 能力混入工具聚合。REQUESTED 用于请求覆盖，EXECUTED 用于执行和显式时长，BLOCKED 保持原 phase；参数覆盖仅用工具角色，ordering 使用包括普通 EVENT 在内的业务事件的已验证关系，只排除声明 inventory 的终结事件。无工具的完整 inventory 可以证明本协议内零调用；不代表未埋点行为不存在。
- 来源能力是上限。PARTIAL/NONE 不升级；缺字段、时钟矛盾、异常父组件、截断或未完成采集按相关能力降级，已确认的独立执行/因果 Step 继续保留。只有 required 能力全部 COMPLETE、inventory 无缺口、采集收敛且未截断时才为 OBSERVED_COMPLETE。无已知子记录/缺口且未截断的单 Root 来源才为 ROOT_ONLY；预算裁剪至仅 Root 展示仍为 PARTIAL，两者工具能力均 NONE。

批次新增可选内部 `ObservationConvergence`，包含规范化输入摘要、分页耗尽标记和稳定观察时长。它绑定完整 scope、合同、profile、所有候选/拒绝行及来源完整性，变动后旧证明拒绝；到达顺序、临时 index 不改变绑定。Normalizer 不从原始 metadata 读取证明，默认无证明。内部 Acquisition 后续须在同一批次分页耗尽并稳定跨过冻结 quiet window 后提供它；当前生产 Reader 的单次消费链只验证最终组装，并明确保持 PARTIAL，不能把测试工厂生成的证明当作真实采集验收。

## 参数与稳定制品

只有已验证的同 workflow/call/tool request→action 配对，才能将请求投影复制到尚未采集参数的 action。保留 AVAILABLE/MISSING/REDACTED/INVALID/未采集差异；action 已有 REDACTED/INVALID 不被请求覆盖，双方已采集但矛盾时保留原值和诊断、降低参数覆盖。不会恢复隐藏值或读取原始请求。

展示顺序沿用 UTC/null/父深度/ID 稳定排序，连续重建 index；显示 index 不生成因果。截断后移除悬空因果引用，非 Root 的缺失展示父节点置 null 并诊断。Root 的外部物理父 ID 保留，但不成为因果引用。

最终 UTF-8 canonical 产物受冻结 artifact 字节限额约束；超限稳定裁剪展示序列的非 Root 尾部并降级，Root 始终保留。Root 与最小产物仍不能容纳时返回安全 `TRAJECTORY_ARTIFACT_ROOT_LIMIT`，不保存超限内容。摘要绑定 scope、脱敏输入/输出、能力/完整度、最终 Step 与 provenance 配置身份，排除自身摘要；参数数值、时间、显式 duration 变化影响摘要。重复行、API 到达顺序、超过阈值的 quiet 时长和组装耗时不改变相同最终语义产物。只输出计数/耗时日志，不含身份或内容。

## 验证与后续

初始 RED 为缺失 assembler 模块；普通 EVENT 缺时间未降 ordering、Step 截断仅剩 Root 误报 ROOT_ONLY 两项额外回归分别实际 RED→GREEN。33 项组装测试与既有相关测试共 377 项通过，ruff 通过。覆盖 inventory 缺口/冲突、terminal 自身、多个 workflow、零工具、来源上限、证明绑定失效、参数状态、独立证据、并行、Root/字节截断及精确摘要往返。单次独立整切片审查没有确认的 Critical/Required；未运行测试，不替代主代理实证，不二次审查。

修复后的最终自有隔离 PostgreSQL 定向 381 项通过，JUnit 为 0 failure / 0 error / 0 skip，容器按 ownership 清理。`make validate` 八步全部通过：1357 项 Python、847 项 Console、契约快照零 drift、Token lint、构建及 Compose 静态校验。独立真实 Langfuse 集成 40 项通过，JUnit 为 0 failure / 0 error / 0 skip，耗时 105.43 秒；原生 OTel / Pydantic AI / HTTP-only 各 10 条生产 Reader→Normalizer→最终 Assembler 消费链，真实 inventory、参数状态投影、显式关系及摘要/反转到达顺序稳定性均被断言。最终 20 条两框架产物为 PARTIAL，10 条 HTTP-only 为 ROOT_ONLY，未提供静默证明时没有完整度升级。这是最终修复后的证据，不采用修复前的首轮门禁数字。

```bash
python -m pytest -q tests/test_trajectory_assembler.py
make trajectory-test
make validate
```

T1 尚需生产 Acquisition 的有界重查/静默证明、不可变制品持久化及 checkpoint 恢复；本增量不启用正式 Binding、Manifest/Snapshot 资格分支，不回写历史。后续准入工厂须冻结组装算法身份。整套 S4 与相关 issue 保持开放，CI 修复继续由 PR #162 所在任务负责。
