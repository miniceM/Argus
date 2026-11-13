# Argus 离线交付工具（仓库维护）

本目录是离线工具的版本化事实来源；随目标完整 Git SHA 一同交付。宿主机技能目录的脚本应与本目录同步。
2026-11-13 起，安装/升级自动管理站点级 Managed Secret 文件，并在迁移前验证历史凭据解密；详细规则见
[站点密钥与升级](references/managed-secrets.md)。本地回归不代表鲲鹏内网验收通过。

# 操作指南

公网Mac构建ARM64离线包，在麒麟/鲲鹏920内网只更新Argus，复用已有Langfuse v4.68.0 + ClickHouse26.11.1.782 ARMv8.0兼容环境。代码审计固定于2026-11-09的 `516ac091dbe4785bea2c755d5708920713359550`，每次更新重新确认目标版本。

本包包含Skill、脚本、Compose示例、操作说明和验收清单，**不含具体版本Docker镜像**；用Mac脚本生成每次的镜像交付包。工具本地验证与内网现场验收分开，见 [验证报告](references/validation-report.md)。

## 安装和导航

把完整目录 `argus-airgap-update/` 放入 `~/.codex/skills/`，新会话可调用 `$argus-airgap-update`。麒麟不需要Codex，复制本目录即可运行脚本。

先使用单独提供的SHA256核对下载的ZIP或tar.gz，再解压；目录内SHA256SUMS可在Mac用 `shasum -a 256 -c SHA256SUMS`、麒麟用 `sha256sum -c SHA256SUMS` 复核。分卷包还会在内部校验整个toolkit。不要把“同一介质里存在一份清单”当作可信来源证明。

| 文件 | 用途 |
|---|---|
| SKILL.md | Agent入口与固定边界 |
| scripts/mac-pack.sh | 固定SHA构建三种ARM64镜像、源码、校验与分卷 |
| scripts/linux-receive.sh / linux-load.sh | 接收校验、安全解包、镜像导入与当前CPU启动检查 |
| scripts/linux-init.sh / linux-adopt.sh | 新站点初始化 / 接管旧部署 |
| scripts/linux-install.sh / linux-upgrade.sh | 首次安装 / 备份后只更新Argus |
| scripts/linux-backup.sh / linux-rollback.sh | 停写备份、真实恢复演练 / 数据与制品恢复 |
| scripts/linux-recover-backup.sh / linux-status.sh | 备份阶段恢复原服务 / 查看状态 |
| scripts/linux-verify.sh | 健康、身份、可选合成数据E2E与反例 |
| references/source-audit.md | 仓库版本、Dockerfile/启动/API/测试来源 |
| references/operations.md | 数据持久化、迁移、停写和故障恢复 |
| references/acceptance-checklist.md | 完整验收与证据要求 |
| assets/compose.example.json | 无凭据Compose示例，JSON是有效YAML |
| assets/source-validation.example.json / ui-receipt.example.json | 源码质量与手工UI验收记录模板 |

## 边界与前置条件

默认目录 `/data/argus-offline`，项目 `argus-kunpeng-e2e`。PG/Redis独立实例和命名持久卷，不发布宿主端口；只有Runner连接已有Langfuse外部网络。Runner/Demo端口为127.0.0.1:18080/18081/18082，Console默认10.102.0.70:18083。更新不重建PG/Redis，不改变其镜像ID或卷，也不改Langfuse/ClickHouse/MinIO及其数据。

不运行原仓库 `make up` / `make clean` / 全栈Compose；禁止 `down -v`、全局prune和删除既有数据卷。site.json、部署Compose/model和备份可能含凭据，目录700/文件600，不能上传Git或普通验收附件。

Mac需要Git、Docker Desktop/Buildx、Bash、Python3.8+，构建阶段可访问公网。源码/临时构建用本机APFS，输出可用ExFAT，留足构建缓存、完整镜像、压缩包及分卷空间。麒麟需要预装Docker/Compose V2（支持config JSON与pull never/no-build/no-deps）、Bash、Python3.8+；内网脚本不会运行pip/npm/git clone/pull/build安装工具或依赖。脚本可用 `ARGUS_PYTHON=/已安装的/python3` 选择解释器。

## 1. Mac固定版本和源码质量

路径按实际修改；含空格、中文的硬盘路径始终加引号。

```bash
REPO="$HOME/argus-build/Argus"
SKILL="$HOME/.codex/skills/argus-airgap-update"
# 首次才clone；已有仓库只fetch，不覆盖工作树。
# git clone https://github.com/miniceM/Argus.git "$REPO"
git -C "$REPO" fetch origin main
SHA="$(git -C "$REPO" rev-parse origin/main)"
printf '%s\n' "$SHA"
```

读目标SHA的Dockerfile、Compose、启动/迁移、API、测试及版本。按仓库要求在该SHA运行 `make validate`；涉及Console再运行 `make console-test` / `make console-e2e`。当前CI源码门禁Python3.12/Node22，与镜像Python3.14/Node26不同，不能互相代替。工作树不在目标SHA时用单独detached worktree验证；打包器自身从git archive导出，不改变当前分支。

证据绑定精确SHA、时间和日志；`skipped_by_policy`不是验收。按 `assets/source-validation.example.json` 留回执，可用 `--source-receipt` 随包携带。打包脚本不运行或伪造仓库测试结果。模板NOT_RUN只在真实执行后修改。

## 2. Mac构建和280MB分卷

```bash
OUT="/Volumes/马哒哒的 lenovo/argus-offline/releases/$SHA"
bash "$SKILL/scripts/mac-pack.sh" \
  --repo "$REPO" --ref "$SHA" --out "$OUT"
```

输出目录必须全新。可加 `--work-dir "$HOME/argus-build/tmp"`（父目录需存在）。默认只含Runner、Demo Agent、Console三种应用镜像。首次安装缺PG17/Redis7时可选 `--with-infra`，更新已有站点仍沿用其原镜像ID。

脚本从同一SHA构建真实Dockerfile，核对linux/arm64、OCI revision、内嵌SHA和Image ID；生成源码/迁移/工具载荷及内部SHA256SUMS，使用无macOS扩展属性的tar+gzip，然后分为 `argus-arm64.tar.gz.part-aaa/aab/...`，每卷最多**280,000,000字节（十进制280MB）**。完整读取gzip并复核分卷校验。

只有part-aaa正常，表示压缩包不超过280MB。保留 `PACK-RECEIPT.json` 和构建日志；通过独立可信渠道记录/交付 **SHA256SUMS.parts自身的SHA256**。同介质上的文件和清单不能单独证明可信来源。

基础镜像标签和依赖范围可能使相同源码SHA重建出不同Image ID。本包固定实际制品，不承诺字节级可重建；目标机已有同名不同ID镜像时拒绝静默替换。

## 3. 内网接收和导入

将已校验的Skill目录、transfer目录传入内网。每次开新终端都重新设置变量，避免沿用旧SHA。

```bash
SKILL="/data/tools/argus-airgap-update"
ROOT="/data/argus-offline"
SHA="516ac091dbe4785bea2c755d5708920713359550"  # 替换为本次真实完整SHA
PARTS_SHA="填写独立记录的64位SHA256"

bash "$SKILL/scripts/linux-receive.sh" --root "$ROOT" \
  --transfer "/data/transfer/$SHA" --parts-sha256 "$PARTS_SHA"
bash "$SKILL/scripts/linux-load.sh" --root "$ROOT" --sha "$SHA"
```

接收拒绝缺失/额外旧分卷、篡改、不连续、超280MB、内部校验不符、路径穿越与链接；完整通过后才发布 `releases/<完整SHA>`，不覆盖已有版本。load只用docker load，并在当前CPU以network none/pull never运行Python原生库与Nginx。Illegal instruction/132必须停在迁移前，回Mac准备兼容制品；架构标签不能代替ARMv8.0实际运行验证。

## 4A. 首次安装

只读获取已有Langfuse Web与真实网络。多个网络时必须明确选择，不取第一条。

```bash
LF_CID="$(/data/langfuse-compat/dc.sh ps -q langfuse-web)"
docker inspect "$LF_CID" \
  --format '{{range $name, $cfg := .NetworkSettings.Networks}}{{println $name}}{{end}}'
LF_NET="argus-langfuse-compat_default"  # 替换为实际输出

bash "$SKILL/scripts/linux-init.sh" --root "$ROOT" \
  --langfuse-container "$LF_CID" --langfuse-network "$LF_NET" \
  --postgres-image postgres:17 --redis-image redis:7 \
  --console-bind 10.102.0.70 --console-url http://10.102.0.70:18083 \
  --langfuse-dashboard-url http://10.102.0.70:13000
bash "$SKILL/scripts/linux-install.sh" --root "$ROOT" --sha "$SHA"
```

init交互读取测试Project Key并生成新Argus数据库/Redis密码。本地基础镜像必须已导入；包附带基础镜像时将参数换为release.json中 `images.postgres.ref` / `images.redis.ref`。不要用本次新基础镜像参数替换已运行站点。

新站点私网使用argus-postgres/argus-redis唯一别名，避免双网络中的通用postgres/redis解析到Langfuse组件。Runner通过已有外部网络访问 `http://langfuse-web:3000`；实际别名不同时显式改 `--langfuse-base-url`。

安装失败先status诊断；仅在相同SHA、相同站点修复原因后，使用 `linux-install.sh --resume`。不建立第二套项目/卷掩盖失败。

## 4B. 接管旧对话部署

已有compose.yaml、.env、source和六个运行中的Argus容器时使用adopt，不重复init：

```bash
bash "$SKILL/scripts/linux-adopt.sh" --root "$ROOT" \
  --project argus-kunpeng-e2e \
  --compose-file "$ROOT/compose.yaml" --env-file "$ROOT/.env" \
  --langfuse-container "$LF_CID" --langfuse-network "$LF_NET"
bash "$SKILL/scripts/linux-backup.sh" --root "$ROOT" --maintenance-window
```

adopt核对运行时与内嵌SHA、真实镜像/命名卷/网络，以及.env/Compose和实际容器环境是否一致，保存原配置和源码副本，不重启容器。当前Runner的PG/Redis地址必须唯一解析到Argus容器；发现通用DNS名歧义或备份数据库不符时停止诊断。通过后为下一次Runner启动改用实际Argus PG/Redis容器名。额外Secret文件等非标准挂载会拒绝自动接管；先适配并保留真实密钥/管理授权文件，不能只保留环境变量中的文件名。

旧Redis若用默认RDB，需确认save非空并备份完整/data。新安装模板显式AOF/RDB。把旧Redis切到AOF是独立维护操作，必须验证持久启动参数；仅在线CONFIG SET不构成重启持久化证明。

## 5. 每次升级

完成步骤1至3，关闭Console之外所有API/定时写入方，按业务契约排空未结束Launch。脚本不自动取消或重放Agent。

```bash
bash "$SKILL/scripts/linux-status.sh" --root "$ROOT"
bash "$SKILL/scripts/linux-upgrade.sh" --root "$ROOT" \
  --sha "$SHA" --maintenance-window
```

先校验已执行迁移checksum，再停应用、备份PG并实际恢复到临时库比对表计数、Redis SAVE后停机复制完整/data、保存旧镜像/源码/迁移/受限配置。成功后才允许候选Runner导入并迁移。

更新只指定四个应用，`--no-deps --no-build --pull never`；前后核对PG/Redis容器/镜像ID和卷不变、Langfuse全项目容器/镜像/挂载/网络不变。Registry是数据库SSOT，升级关闭YAML自动导入。active/previous指向独立deployment目录，不覆盖旧源码/配置；健康通过仍待业务验收。

## 6. 验收

```bash
bash "$SKILL/scripts/linux-verify.sh" --root "$ROOT"
bash "$SKILL/scripts/linux-verify.sh" --root "$ROOT" --demo
```

基础检查健康、实际镜像/完整SHA、Schema、数据隔离、Console代理和LF ready。demo新建独立合成Dataset/Agent/三次Launch与固定策略，不覆盖原banking或业务数据：v1预期2/6且Gate FAIL，v2预期6/6且Gate PASS，不可达Agent预期UNKNOWN；另验跨Launch、错误环境和不可变策略反例。

真实读取Langfuse v4 Observation/Experiment Item/v3 Score，按Trace/Observation/Dataset Item/稳定Score ID和数值核对。Builtin source=API不证明托管评分source=EVAL已通过；真实模型、Token/费用另行验证，不用0代替未知。脚本只重试只读可见性查询，POST超时保留ID诊断，不立刻重跑。

证据在 `evidence/verify-时间/`，自动结果最多为API_PASS_UI_AND_SOURCE_EVIDENCE_PENDING。继续完成 [验收清单](references/acceptance-checklist.md) 中浏览器UI、历史Snapshot、业务Registry/凭证恢复和持久化检查，记录截图与最终决定；不把API healthy当作整体验收。

## 7. 回滚

关闭全部写入，确认恢复到明确备份时刻。备份后数据会保留在隔离库/卷和rescue包，不自动合并。使用升级operation.json中记录的回滚点；另做过备份时不能只凭“最后一次”猜测。

```bash
BACKUP="/data/argus-offline/backups/backup-实际时间"
BACKUP_SHA="填写当时记录的SHA256SUMS自身64位SHA256"
bash "$SKILL/scripts/linux-rollback.sh" --root "$ROOT" \
  --backup "$BACKUP" --backup-sha256 "$BACKUP_SHA" \
  --restore-data --maintenance-window
bash "$SKILL/scripts/linux-verify.sh" --root "$ROOT"
```

先保全升级后状态，再将旧dump恢复到新数据库核对，升级后库改名保留，旧库恢复原名；Redis复制到新卷，原卷保留，使用同一镜像重建本项目Redis容器。旧镜像/源码/迁移/配置同时恢复。PG容器/卷和Langfuse网络不改，Langfuse已写Trace/Score不删除。

仅切镜像不能回滚迁移。失败/中断先status，按 [operations.md](references/operations.md) 的事务阶段恢复；不要盲目启动旧Runner或手工改事务文件。

## 8. 工具自检

```bash
python3 -m unittest discover -s "$SKILL/tests" -v
RUN_DOCKER_TESTS=1 python3 "$SKILL/tests/test_docker_restore.py" -v
```

第二条要求已有本地ARM64 PG17/Redis7，在全新临时Docker项目实际验证数据恢复，不下载镜像、不接入真实Langfuse。详细实测/未测范围见验证报告。
