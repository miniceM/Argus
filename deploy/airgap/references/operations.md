# 操作、持久化与恢复约束

Managed Secret 以 [站点密钥与升级](managed-secrets.md) 为准：密钥位于稳定站点目录，普通数据库备份仅记录依赖，独立密钥恢复集合分别保管。

## 状态和文件

| 路径 | 含义 |
|---|---|
| releases/完整SHA/payload | 校验过的交付文件，不能覆盖 |
| releases/完整SHA/source | 同SHA的源码/配置/数据/迁移，启用前与源码归档比对 |
| site.json | 固定站点/PG/Redis/网络身份和初始必要配置，600，含敏感信息 |
| deployments/时间-SHA | 版本自己的compose.json、model.json、deployment.json |
| active.json / previous.json | 目前/上一部署指针，包含验收状态 |
| backups/backup-时间 | 升级前完整备份与清单，不自动删除 |
| backups/rescue-before-rollback-时间 | 回滚前升级后状态的保全副本 |
| transaction.json | 尚未结束操作的阶段、候选、备份与恢复资源身份 |
| last-backup.json | 最近完整备份的路径与SHA256SUMS自身SHA256 |
| evidence/ | 合成验收/操作回执，不包括site.json或完整Compose |
| .operation.lock | 防并发操作；异常断电留下时需先核实进程和事务 |

脚本新建目录按700、新建配置按600限制；既有ROOT目录也应由操作者核对权限。只复制并共享经过审查的evidence；备份含数据库、凭据配置与可能的业务内容，按数据备份权限保管。

## 升级序列和持久化

1. 固定SHA、源码质量与制品；接收校验后导入镜像，当前CPU启动检查。
2. 核对LF全项目容器/镜像/挂载/网络ID、Argus PG/Redis容器/镜像/命名卷。PG/Redis不能挂Langfuse卷或发布端口。
3. 核对数据库schema_migrations中每个version/checksum在目标源码中完整存在且相同。新增迁移单独列出，不猜测SQL可逆性，不修改已发布脚本。
4. 所有生产者停写；关闭Console，拒绝尚有非终态Launch的更新。排空时不得盲目重放有副作用的Agent；超时不代表远端未执行。
5. 停Runner/内置Worker、Reconciler、Syncer及Demo；再次核对未结束Launch数。没有仅靠HTTP开关就能证明全局停写，操作者需管理直连API与定时任务。
6. PG用pg_dump custom格式备份；pg_restore --list后**实际恢复到随机临时数据库并核对全部public表计数**。只删除本次自己创建的临时验证库，原Argus库不删除。
7. Redis执行SAVE，停止本项目Redis，复制完整/data（包括RDB、AOF目录/manifest）；备份校验完成才启动同一Redis容器。
8. 保存上一版本所有本地镜像、源码/config/data/migrations与受限配置；校验完成发布备份路径/清单身份。
9. 只启动新Demo、Runner、Console，不重建PG/Redis。Runner导入main时执行迁移，因此禁止绕过备份先run/import app.main。
10. 核对运行镜像内嵌SHA、API build_id、迁移集合和不变的基础服务/外部网络，再做独立业务/UI验收。

PG dump保留对象所有者和ACL，在原PG实例/角色仍保留的前提下恢复；不承担PG集群角色、服务器配置或数据库大版本灾备。操作账号必须可备份、建临时数据库、恢复对象/权限、终止本库连接和改数据库名；默认独立PG的POSTGRES_USER具备这些能力，自定义最小权限账号应先适配，恢复演练失败不得继续。

新安装Redis显式开启AOF everysec和RDB；断电持久化仍受Redis策略和磁盘可靠性约束，不能承诺零丢失。旧部署默认RDB只要save非空可接管；AOF转换必须把启动参数持久化后做重启验证，不能只CONFIG SET。备份使用停机文件副本避免AOF重写中拷贝不一致。

迁移恢复和保存的源码需要足够空间：备份目录放dump/Redis/镜像，PG卷还需额外恢复库。脚本做估算检查，实际磁盘不足仍立即停止。备份在同一物理盘不能代替跨介质灾备；将完整备份和独立清单hash另存受控离线备份介质。

## 回滚序列

必须指定备份目录和当时记录的SHA256SUMS自身hash；验证站点ID、项目、基础镜像/PG卷一致、该备份实际恢复演练通过。恢复到此时间点，备份之后Argus数据不自动合并。

先停止应用，救援备份当前PG/Redis状态。将旧dump恢复到新的随机库并比对，创建新Redis卷写入旧完整/data。随后保存恢复资源名称到事务日志，再改名保留当前Argus数据库，恢复旧库原名；新Redis卷通过同一镜像重建本项目Redis容器。旧镜像、源码挂载、配置和迁移同时恢复。

原PG容器/数据卷不删除。升级后数据库argus_hold_*、原Redis卷、rescue备份都保留，验收后按明确保留期限另行处理；脚本不自动删除。Langfuse已有Trace/Score不回滚，按明确Launch/Snapshot/Trace身份区分证据，不能把历史结果给新候选复用。

## 故障处理

先运行linux-status.sh并保存受限状态。脚本返回非零时不将失败转PASS，不通过跳过检查继续。

| 现象/事务阶段 | 行动 |
|---|---|
| 分卷/内部校验错误，尚未接收 | 换全新传输目录重传同批次；不用损坏包启动任何容器 |
| 已存在同名不同ID镜像 | 保留两份包核对来源；以新SHA发布合法新版本，不覆盖现有身份 |
| CPU检查Illegal instruction/132 | 留当前运行版；Mac重做兼容制品，先在目标CPU检查 |
| 外部网络/镜像/数据卷身份不符 | 停止；查明独立变更，人工确认实际新基线，不自动改site.json |
| 未结束Launch | 原服务保留，按业务契约排空或授权取消；不直接改SQL状态 |
| PREPARED / BACKUP_IN_PROGRESS / BACKUP_COMPLETE | 未启动候选Runner时可用linux-recover-backup.sh；脚本确认Schema仍精确匹配旧源后启动原服务 |
| RUNNER_STARTING_MIGRATIONS_MAY_HAVE_RUN或后续升级失败 | 已可能迁移，使用记录的升级前完整备份做linux-rollback.sh，不直接切旧镜像 |
| 初次安装失败 | 修复原因后相同SHA的linux-install.sh --resume；不改项目/新建替代数据卷 |
| init创建资源时失败，尚无site.json | 核对本次project命名卷、私网及io.argus-offline.site标签，保全现场后单独处理；不把残留资源自动当空站点，也不删除未知卷 |
| 恢复演练失败/空间不足 | 保留旧库和应用停写状态，修复存储/权限；不宣称已有可恢复备份 |
| 回滚在数据交换前失败 | 原库仍在；保全transaction/rescue/候选配置，按记录诊断，不删临时资源 |
| DATABASE_SWAP_PREPARED / DATABASE_RENAMED | 查看日志中held_database/restored_database与实际pg_database；先保持应用停机，确认原名是否存在，再恢复命名，绝不盲目重复rename或DROP |
| DATA_RESTORED后失败 | 数据已恢复；按target_deployment及restored_redis_volume核对实际挂载，恢复相同旧制品；不能用升级后源码启动旧库 |
| API成功但Score/Experiment超时 | 保留Launch/Trace/Snapshot ID，只重查同一身份和LF Worker/ClickHouse状态；不POST制造新评分冒充成功 |
| UI静态资源/代理/历史Snapshot错误 | 业务验收失败，保留API证据和浏览器错误；必要时走完整回滚 |
| 操作锁遗留 | 确认owner.json进程已不存在、无另一终端操作且事务已诊断后，手工移走锁目录；不要删除事务日志来绕过恢复 |

恢复命名只允许对事务明确记录的本项目Argus数据库操作。若需人工介入，在PG容器中先通过psql -d postgres查询实际数据库名/连接、备份表计数，再决定完成原名恢复或保留现场。数据库交换并非跨PG/Redis的分布式原子事务，所以必须持续停写、保留阶段日志与原始数据。

## 自定义站点适配

adopt只接受标准六服务、独立命名PG/Redis卷、Runner三个只读config/data/migrations挂载。额外Secret文件/私有CA/认证代理、其他工作节点、未知网络或自定义DB布局要先显式适配；脚本拒绝是为了避免丢失凭证恢复能力或误连其他数据库。保留真实ARGUS_MANAGED_SECRET_KEY_FILE和管理授权文件，凭证密文恢复后还要做不泄密的业务调用验证。

站点变更若是升级PG/Redis大版本、Langfuse/ClickHouse或重建原网络，应单独规划迁移和灾备，不纳入“只更新Argus”。
