# S4.1 首批：标准轨迹数据契约

本批实现 [01 技术契约](../../plans/2026-10-13-s4-trajectory/01-technical-design.md) 的七个领域类型和局部 JSON 编码器，为后续只读采集、组装及冻结制品提供输入。对应 #128 的首个增量，完整 S4.1 验收仍需后续采集、来源校准、脱敏、组装与持久化。

## 类型和读取约定

`app.trajectory.schema` 提供 `TrajectoryScope`、`TrajectoryStep`、`TrajectoryCompleteness`、`TrajectoryProvenance`、`StandardTrajectory`、`TrajectoryEvidenceRef` 与 `TrajectoryAcquisition`。新格式仅注册 `schema_version="1.0"`，未知字段及版本被拒绝。

采集 `PENDING/READY/UNAVAILABLE`、观测完整度和六项能力覆盖各自表达事实。READY 必须有完整度和制品引用，且不可重试；UNAVAILABLE 必须有稳定原因码，无轨迹/制品引用。UTC 时间、时长、参数或结果缺失使用 null；不补当前时间、零时长或 MISSING 参数路径。物理 root 的 parent 可位于选定闭包之外。

Scope ID 使用 1–64 位非空、无空格/控制字符的 ASCII；`dispatch_generation` 沿用现有记录的正整数。Trace ID 为非零、32 位小写十六进制。Digest 沿用仓库的 `sha256:<64 lowercase hex>`。Step ID 等于 Observation ID，轨迹内 Step ID 与展示 index 唯一，所选 root 必须存在。

所有字段赋值受 frozen model 约束，但 dict/list 不承诺深度不可变。来源归属、可信因果、字段 allowlist 和内容脱敏由后续模块验证；通过类型校验本身不能证明来源可信或完整。制品不可变性由后续 insert-only 持久化保障。

## 精确 JSON 和边界

冻结制品、局部响应及摘要必须使用 `app.trajectory.codec.canonical_dumps/canonical_loads`，不使用 Pydantic 默认 JSON serializer 或先经 float 解码。Decimal 编码为 JSON number，大整数保留原值；键排序、紧凑 UTF-8、UTC 六位微秒、负零归零、去掉多余小数尾零。结构化错误只包含稳定代码；处理 Pydantic 错误时使用 `errors(include_input=False)`，不记录原输入。

JSON 字符串及对象键最多 4096 UTF-8 bytes；完整文档深度最多 32（根值深度为 0，子值加 1，计入轨迹/Step 外层结构），schema 与 codec 采用同一边界；数字系数最多 64 位、指数绝对值不超过 64，展开后的整数部分不超过 64 位。NaN/Infinity、float、非法 Unicode、重复对象键均拒绝。独立制品总预算默认 2 MiB，编码逐段检查字节，解码前检查总字节及容器嵌套。steps、因果引用、参数可见性条目和 warnings 均设 1000 项硬上限；后续冻结合同可采用更低预算。

静态 JSONPath 仅支持 `$`、`.identifier`、`[unsigned_integer]`，最多 32 段；identifier 为 `[A-Za-z_][A-Za-z0-9_]*`，数组下标使用无前导零的十进制写法且最多 64 位。编译输出仅为字符串/整数 token tuple，不访问对象属性或执行表达式。通配、过滤器、递归、切片、函数及动态键均拒绝。

## 本地复现

```bash
.venv/bin/python -m pytest -q tests/test_trajectory_schema.py
make validate
```

首轮 RED 为缺少 `app.trajectory` 模块；新增容量回归实证编码器在越界后仍访问后续值（1 failed/1 passed），改为逐段限界后通过。独立审查指出完整轨迹外层与字段局部深度不一致，补完整模型边界回归并确认 RED 后，统一文档深度校验。目标测试 85 项通过；完整门禁结果随本批 PR 提交记录。

本地 `make validate` 通过：880 Python、847 Console 测试，Token lint、typecheck、build、契约快照同步与静态 Compose 校验通过。4 项现有 PostgreSQL 专用测试因 `TEST_POSTGRES_URL` 未配置而跳过；31 项真实采集测试仍由独立门禁执行。当前增量未接 API、数据库或运行态路径；既有真实门禁及 PostgreSQL 的远端结果以本 PR 的 CI 为准。
