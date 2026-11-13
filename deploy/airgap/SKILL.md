---
name: argus-airgap-update
description: 为 miniceM/Argus 制作 Mac ARM64 离线交付包，在麒麟/鲲鹏内网完成首次安装、旧部署接管、仅 Argus 更新、备份恢复回滚与分层验收；复用已有 Langfuse 外部网络，不升级 Langfuse/ClickHouse 或数据库镜像。
---

# Argus ARM64 离线更新部署

面向公网 macOS ARM64 构建端与完全无公网的麒麟 Linux / 鲲鹏920部署端。默认目录 `/data/argus-offline`，Compose 项目 `argus-kunpeng-e2e`。固定完整 Git SHA，交付三个 Argus 镜像、同 SHA 源码/迁移、版本/镜像清单、双层 SHA256 与 280,000,000 字节分卷。

首次使用读 [README](README.md)；按任务读 [源代码审计](references/source-audit.md)、[升级与恢复约束](references/operations.md)；最后完成 [验收清单](references/acceptance-checklist.md)。脚本仅用 Bash、Python3.8+标准库及预装 Docker/Compose V2，内网不下载工具或依赖。

## 基线与不变量

- 原对话中用户确认 Langfuse v4.68.0、ClickHouse26.11.1.782 ARMv8.0兼容运行，OTLP、Dataset、v3 Scores与 UI Score 通过；旧 Argus 包 `369bad665abd639d5950df026fe6ca5855fc0dbf` 的归档/分卷/gzip校验通过。**没有 Argus 已部署或完整 E2E/Gate 已通过的用户证据。**
- 本次仓库审计是2026-11-09远端 main `516ac091dbe4785bea2c755d5708920713359550`。未来更新重新读目标 SHA 的 Dockerfile、Compose、启动/迁移、API、测试和版本，不能把此 SHA 当未来最新版。
- 只操作独立 Argus 项目。Langfuse/ClickHouse及其PG/Redis/MinIO、数据卷和配置目录不变。固定已有外部网络 ID；多网络必须明确选择，不能取第一条。只有 Runner 接入该网络。
- 六个 Argus 容器：独立PG17、Redis7、Demo v1/v2、Runner、Console；五种镜像。Runner内含Worker/Reconciler/Syncer。PG/Redis无宿主端口，使用独立命名持久卷；新站点Redis启用AOF/RDB，接管旧站点核验实际持久化。
- 升级沿用PG/Redis的Image ID、容器和卷，仅对四个应用执行 `up --no-deps --no-build --pull never`。原仓库 `make up` / `make clean` / 全栈Compose不是内网入口。
- 禁止 `down -v`、全局prune、混用其他项目卷、改已执行迁移、运行时伪造新SHA、无证据latest回退。arm64标签不能证明ARMv8.0原生库可运行。

## 执行路径

### 固定版本与Mac交付

确认目标ref后一次解析为完整SHA，后续只用该SHA。核对源码质量证据与SHA一致；`skipped_by_policy`不算实测。Runner版本读取真实Dockerfile，Console package版本是独立组件版本，不硬编码成同一值。

运行 `scripts/mac-pack.sh --repo ... --ref "$SHA" --out ...`。脚本从git archive构建，不切换用户分支、不带未提交文件；本机APFS作临时构建目录，输出可在ExFAT。默认仅含三种Argus镜像，首次安装缺基础镜像才可选 `--with-infra`，不授权改变已有基础镜像。

核对linux/arm64、OCI revision、Runner内嵌SHA与Image ID。记录PACK-RECEIPT及分卷清单自身SHA256，独立交付后者。相同SHA重建可能产生不同Image ID；交付固定实际制品，不承诺字节级可重建。打包成功不等于源码测试、目标CPU或业务验收通过。

### 内网接收、首次安装或接管

工具事实来源为目标 SHA 中 deploy/airgap；宿主机技能脚本应与它同步。Managed Secret 规则必读 [站点密钥](references/managed-secrets.md)。

先校验本Skill，再执行 `linux-receive.sh --parts-sha256 ...`。拒绝分卷缺失/混入旧卷/篡改/不连续/超280MB、内部校验不符、路径穿越与链接。通过后才发布不可变 `releases/<完整SHA>`。

`linux-load.sh`只加载已校验镜像，在当前CPU以 `--network none --pull never` 做Python/原生库/Nginx启动检查；失败停在迁移之前。

- 未部署：`linux-init.sh`固定已有LF容器/网络和本地PG17/Redis7镜像，生成新密码和独立卷，再 `linux-install.sh`。
- 旧对话部署：`linux-adopt.sh`读取原.env/Compose/真实容器，核对SHA/镜像/卷/网络并保存源码与受限配置，不重启服务。标准两个 Managed Secret 只读文件和旧 config/.secrets 支持安全迁移，其他未知挂载仍拒绝自动接管。
- 已由本Skill管理：沿用site.json/active.json，禁止重复init或换项目名“升级”。

### 备份后仅更新Argus

进入已明确的停写窗口，暂停Console之外的API/定时生产者。未结束Launch按业务幂等/取消契约处理；不自动取消或重放。

`linux-upgrade.sh --sha ... --maintenance-window`先核对迁移checksum，再停应用，生成PG custom dump并真实恢复到临时库、比对表计数；Redis SAVE后停机复制完整/data；归档上一版镜像、源码/迁移/配置。全部备份检查完成后才启动新Runner（导入main即迁移）。

升级关闭YAML自动导入，因为Registry以PG为SSOT；保留现有凭证、加密和其他Runner环境。构建身份来自镜像，不能用Compose新SHA掩盖旧镜像。

失败保留transaction.json与现场，先 `linux-status.sh`。仅备份阶段且Schema未变可用 `linux-recover-backup.sh`；Runner已启动或可能迁移后走完整数据恢复回滚，不能盲目启动旧镜像。

### 恢复回滚

明确用户已授权恢复到指定备份时刻，解释备份后Argus新增数据不会并入旧数据。运行 `linux-rollback.sh --backup ... --backup-sha256 ... --restore-data --maintenance-window`。

先保全升级后状态；旧dump恢复到新库并核对后，当前库改名保留、旧库恢复原名。Redis恢复到新卷，原卷保留；旧镜像、源码/配置/迁移一并恢复。Langfuse已写Trace/Score不删除、不回滚，用新验收ID区分。

中断于数据库改名/Redis切换阶段按operations.md处理，不删保留库/卷，不编辑事务状态伪造成功。

### 分层验收与报告

先 `linux-verify.sh`；真实端到端加 `--demo`，新建独立合成Dataset/测试Agent/三次Launch，不覆盖业务或原banking注册数据。

验证v1 2/6、v2 6/6、不可达Agent的UNKNOWN；固定Manifest、Snapshot、策略、环境及Gate；从已有LF读取Observation、Experiment Item与v3 Score，核对同一Trace/Observation/Dataset Item和稳定Score ID。覆盖跨Launch、错误环境及策略不可变拒绝路径。仅重试GET可见性，不重发超时Agent调用。

Builtin的source=API不冒充托管评分source=EVAL；真实模型、Token/费用、托管Judge/Code需求另按目标代码契约验收，未运行标NOT_RUN。不得伪造内部轨迹。

API报告不代替UI、源码质量、历史数据抽查或发布授权。脚本仅报告API_PASS_UI_AND_SOURCE_EVIDENCE_PENDING；完成checklist后由操作者留下最终ACCEPTED/REJECTED。交付列出固定SHA、镜像/分卷/备份身份、证据路径、未完成项和回滚点，不输出Secret/认证Header/完整受限Compose。
