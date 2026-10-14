# S4.1 / S4.2 轨迹 Manifest 1.3

关联 #128、#5，依赖 PR #173；核验日期 2026-10-14。

`LaunchService.create_launch(..., trajectory_contract=contract)` 是内部显式冻结入口。省略参数仍创建原 Manifest 1.2；本增量没有公开启动请求字段、自动来源推断或 Worker 自动采集。正式启用还需来源校准、qualified Reader 工厂与后续 trajectory Binding。

## 冻结与精度

- Manifest 1.3 的 `trajectory` 保存 `contract_json`、`trajectory_contract_digest`、`selected_source_profile_id`、`selected_source_profile_version`。`contract_json` 为既有局部 codec 的 canonical JSON TEXT，嵌入 Manifest JSONB，Decimal 无需经过 float 或全局 serializer。解析时验证字节 canonical 性、完整类型及摘要。
- 先冻结完整 profile 集合、选择算法、映射、脱敏、所需能力及全部限额为独立字符串载荷，再从不可变 AgentVersion 声明选来源。调用方的私有绑定不改变实际分支；后续嵌套容器修改也不影响已经冻结的载荷。
- Agent 区块保存非空来源声明；选中分支只属于执行 provenance。`measurement_digest` 在原 Binding 测量摘要基础上加入完整 trajectory contract digest，因此两个框架使用同一合同保持测量身份相同；任何合同语义变化影响测量及启动请求幂等身份。
- 无合同请求不增加幂等 payload 字段，维持旧算法。Binding 自身仍使用既有 1.2 格式，Manifest 及 measurement 三层版本为 1.3；不提前启用 Snapshot `evidence_contract_version="trajectory-1.0"`。

## 读取与错误

`load_trajectory_contract` 仅读冻结载荷，按注册版本解析；1.0 / 1.1 / 1.2 和没有声明版本的历史载荷不回填合同。旧 schema 携带 trajectory、未知版本、非 canonical JSON、错误摘要、profile 声明冲突或版本不匹配均拒绝，不使用字符串大小比较版本。

既有 `resolve_execution_plan` 与 `evaluate_frozen_item` 共用校验：在执行或重评前验证完整合同及声明的 measurement digest / contract_digests，损坏时不产生分数，返回 failed / unknown 与有限 `TRAJECTORY_MANIFEST_*` 原因。合同容器被篡改时关闭原值 serializer warning，再校验并拒绝，不把原配置写入异常文本或日志。此处的形状校验不证明 Reader / 来源 / assembler 已通过实际运行准入；后续启用仍必须完成资格检查。

## 验收

实际 RED→GREEN：缺少轨迹 Manifest 模块；共享重评入口仍为损坏合同产生分数；合同变更但原 measurement 未更新仍可执行；非法容器触发含原值 serializer warning。测试覆盖跨框架测量一致、合同语义变更、配置隔离、幂等冲突、十二种非法载荷、三层测量冲突、注册历史版本无回填及 PostgreSQL JSONB 精确往返。

单次独立整切片审阅：Critical / Required / Optional 均无。最终隔离 PostgreSQL 目标集 **722 passed**（JUnit 0 failure / error / skip，19.048s），含真实 JSONB 精度回归；`make validate` 八步全部通过，**1552 Python / 847 Console**，Token lint、契约快照零漂移及 Compose 校验通过。真实本地采集兼容门禁 **43 passed**（JUnit 0 failure / error / skip，152.630s），验证已有两框架／HTTP、checkpoint 与 Coordinator 的原业务结果；它不证明本增量尚未启用的 Worker 1.3 采集已完成。真实 Cloud 未运行。首次完整门禁曾因新测试 import 排序失败，已修正并以上述最终通过结果复验。

```bash
python -m pytest -q tests/test_trajectory_manifest.py tests/test_launch_manifest_freeze.py tests/test_issue_81_frozen_execution_identity.py tests/test_issue_83_quality_policy.py tests/test_issue_85_snapshot_revision.py tests/test_issue_86_comparability.py
make trajectory-test
make validate
```
