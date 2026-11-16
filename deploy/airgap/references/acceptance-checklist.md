# 更新与回滚验收清单

每次复制本清单到独立evidence目录。结果只能填PASS、FAIL、NOT_RUN或NOT_APPLICABLE并说明原因；未填、跳过或证据不足不能算通过。敏感站点配置/数据库备份只记路径与hash，不附内容。

## 记录身份

| 字段 | 本次填写 |
|---|---|
| 操作者、日期、停写窗口 | |
| 目标完整Git SHA / 上版完整Git SHA | |
| Runner / Demo / Console Image ID | |
| 分卷清单SHA256、分卷数量/单卷字节数 | |
| 原PG/Redis镜像ID、容器ID、独立卷名 | |
| 原LF Web/Worker/ClickHouse等容器/镜像/挂载摘要 | |
| LF外部网络名与ID、Argus私网 | |
| 升级前备份路径、SHA256SUMS自身hash、恢复演练证据 | |
| 操作deployment、transaction、operation回执路径 | |
| 业务验收目录 / 浏览器截图 / 最终决定 | |

## A. 原环境与源码质量

- [ ] 实际LF仍为已验证的v4.68.0，ClickHouse仍为26.11.1.782 ARMv8.0兼容制品；保存实际镜像ID，不能仅凭tag推断。历史PoC通过不冒充本次验收。
- [ ] 目标SHA来自miniceM/Argus，三份Dockerfile、源码/配置/迁移同SHA；未夹带未提交内容。
- [ ] 同SHA的源码门禁make validate与适用Console测试有完整实测证据；执行环境按当前仓库要求。
- [ ] 远端CI绑定精确head/base/测试身份；未运行或skipped_by_policy不作Cloud验收。
- [ ] Runner/Console组件版本来自实际代码；Git SHA、组件版本和镜像ID分别记录。
- [ ] 依赖范围/基础镜像标签可变已披露；交付归档和Image ID固定，未宣称字节级重建。

## B. 打包、传输与CPU

- [ ] 独立记录分卷清单自身hash；每个分卷hash、数量、连续aaa起序与精确文件集合通过。
- [ ] 单卷≤280,000,000字节；一卷也正常；未混入旧版本分卷。
- [ ] gzip完整、内部SHA256SUMS全部一致、源码归档/ARGUS_BUILD_ID/release.json一致。
- [ ] 三镜像linux/arm64、OCI revision为目标完整SHA，Runner镜像内嵌ARGUS_BUILD_ID一致。
- [ ] 当前鲲鹏CPU Python/SSL/psycopg/cryptography/Nginx实际启动通过，无Illegal instruction/132。
- [ ] 内网无pull/build、pip/npm/apt/yum下载；缺失制品或工具时明确失败，未回退公网。
- [ ] 非法路径、链接、篡改、缺失、同tag不同Image ID均拒绝；对应本工具测试证据可追溯。

## C. 隔离、持久化与备份

- [ ] 六个Argus容器属于固定独立项目；PG/Redis不属于LF项目、不共享LF卷、不发布端口。
- [ ] 只有Runner连LF外部网络；PG/Redis/Demo/Console只在Argus私网；无通用DNS误连。
- [ ] PG17/Redis7镜像ID固定，更新前后PG/Redis容器ID、命名数据卷不变。
- [ ] Redis实际AOF或RDB开启；新站点AOF everysec/RDB参数持久在启动配置中。
- [ ] 所有写入方停写，无未结束Launch；没有为升级自动取消/重放有副作用调用。
- [ ] 旧迁移不可变checksum通过，新增迁移清单审阅；Runner启动迁移前已完成备份。
- [ ] PG custom dump、pg_restore --list和**临时库真实恢复+所有public表计数比对**通过。
- [ ] Redis SAVE成功、停机后完整/data复制并校验；保存AOF manifest/目录和RDB。
- [ ] 旧镜像、源码/config/data/migrations、受限配置和必要Secret真实文件均已保全。
- [ ] 密文凭证恢复所需密钥/授权文件已核对；非标准额外挂载未被静默丢弃。
- [ ] 备份目录校验、清单自身hash、独立离线备份副本与可用空间均有证据。

## D. 正常路径与真实关联

- [ ] Runner / Console / Demo healthy；Runner API与Console代理system/info的build_id都等于目标SHA。
- [ ] 运行容器Image ID与交付清单一致，非单纯检查环境变量；Schema集合/迁移checksum一致。
- [ ] LF原项目容器、镜像、挂载、外部网络ID与升级前完全一致；未覆盖ClickHouse兼容镜像。
- [ ] unique合成Dataset创建、Item写入和读取；输入/期望输出与仓库六个用例一致，不覆盖原业务数据。
- [ ] 两个真实HTTP Demo Launch各6项；v1质量2/6、v2质量6/6，执行成功和质量失败分开。
- [ ] Frozen Manifest固定AgentVersion/规格、Dataset具体版本/Items、Evaluator id+1.0.0、质量/测量/比较契约、Runner SHA、环境。
- [ ] 每次调用有真实Trace/Observation；LF Observation与本地Item精确一致，没有伪造内部轨迹。
- [ ] LF Experiment / Dataset Item / root Observation / Trace join全部一致，不能只看有个链接。
- [ ] v3 Scores稳定ID、subject、name、数值与实际Builtin结果一致，source=API如实记录。
- [ ] 固定Snapshot、ReleasePolicy版本/digest及环境：v1 Gate FAIL，v2 Gate PASS。
- [ ] 重读历史Snapshot内容和digest不变，不用最新summary替代冻结证据。
- [ ] Managed EVAL、真实LLM、Token/费用：若本次明确要求，补来源/attempt/模型/环境证据；否则记录NOT_RUN，不冒充通过。

## E. 异常与拒绝路径

- [ ] 不可达合成Agent版本：真实执行失败、无通过质量、固定Snapshot/Gate UNKNOWN，其他正常Launch不受影响。
- [ ] 跨Launch读取Snapshot返回404；跨Launch Gate不能借用其他快照。
- [ ] 同名同版本ReleasePolicy修改被409拒绝，不改原策略。
- [ ] 候选环境与策略环境不符被409拒绝。
- [ ] Score/Experiment可见性超时保留原ID和状态；只重试GET，不反复POST制造“成功”。
- [ ] 备份/恢复演练失败会阻止候选Runner启动，保留事务；恢复前不伪造PASS。
- [ ] Schema迁移失败/原生库失败/基础服务身份变化均停在明确阶段；有相应处置记录。

## F. 浏览器与历史抽查（人工）

- [ ] 浏览器Console首页和静态资源正常，无明显错误；system/info代理连到本次Runner。
- [ ] 合成Agent版本、Launch进度、执行/评测/质量/同步状态表达分别正确。
- [ ] v1/v2/不可达Launch的Manifest、Snapshot、Gate与API证据相符。
- [ ] 切换历史Snapshot不会跳到latest；无效/跨Launch身份不悄悄回退。
- [ ] LF UI Dataset、Experiment、Trace/Observation和Score显示正确，链接指向已有内网实例。
- [ ] 更新前已有Registry/AgentVersion、历史Launch/Snapshot/基准/策略/审计记录可读，内容/digest未被修改。
- [ ] 自定义凭证/密钥文件业务调用仍可用，日志/UI/证据中无Secret泄露。
- [ ] 受控维护窗口内重启Argus组件后，Registry、历史数据和Redis队列仍符合预期；LF身份不变。
- [ ] 截图/浏览器错误记录带日期、URL和明确SHA/Launch/Snapshot；不含Secret。

## G. 数据恢复回滚演练

- [ ] 在独立测试或已授权窗口实际恢复指定备份；明确接受回到备份时刻，不合并后续写入。
- [ ] 已备份升级后状态；恢复旧Schema、表计数、选定历史记录/digest与必要密钥，而非只切镜像。
- [ ] 恢复旧镜像内嵌SHA与旧源码/迁移；API和Console代理一致。
- [ ] 升级后PG库保留为argus_hold_*；原Redis卷与rescue副本保留；没有删除原卷。
- [ ] 回滚允许为恢复数据重建Argus Redis容器，但保持同一Redis镜像ID；PG容器/卷和LF网络不变。
- [ ] 恢复后健康/历史/UI检查通过；已有LF Trace/Score未删除，以新测试ID区分。
- [ ] 中断阶段、保留资源和后续保留期限有记录；不用事务文件手工标成功。

## 最终记录

最终决定：`PENDING / ACCEPTED / REJECTED`。操作者：____。时间：____。

必需项未运行、失败或不可证明时保持PENDING/REJECTED，并列明未完成项与回滚点。API_PASS不等于整体ACCEPTED，质量Gate也不等于最终发布授权。

## Managed Secret 根因回归

- [ ] 新站点随机密钥/管理 Token，文件 600、目录 700，镜像和源码不含密钥。
- [ ] 旧 config/.secrets 原样迁移；升级前后字节一致；原文件未覆盖。
- [ ] 所有历史修订在迁移前通过真实 Provider 解密，错误密钥或缺失权限失败关闭。
- [ ] 普通 DB 备份不含 Keyring；独立 secret-recovery 集合已另存受控介质。
- [ ] Runner 换 IP 后 Console 无重启恢复；Console 代理 build_id 等于目标 SHA。
- [ ] 凭据页面环境与服务一致；业务 AgentVersion 使用相同环境，原凭据仍能调用。
