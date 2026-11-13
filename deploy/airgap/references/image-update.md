# 日常更新：加载镜像，修改现有 Compose

适用：内网已有正常配置的 Argus，已有 Langfuse 保持运行，本次交付确认没有新增迁移或必改配置。
交付方负责核对差异；不让部署人员自行猜测是否兼容。首次安装、旧部署接管和涉及数据库迁移的版本另附专项步骤。

## 交付内容

- `argus-images-<SHA前12位>.tar.gz`：Docker 可直接加载的 Argus 应用镜像，不含数据库或 Langfuse 镜像。
- `IMAGES.json`：完整源码 SHA、镜像标签与身份、相对上一交付的迁移/配置变更结论。
- `SHA256SUMS`：文件校验。
- `README.md`：本版本的镜像标签和下面几步操作。

每个文件不超过 280,000,000 字节时不分卷。不附带日常升级不需要的站点状态机和接收/接管脚本。
超限才使用分卷并在该包 README 给出固定的合并命令。校验清单自身的 SHA256 由交付方单独告知。

## 内网操作

在交付包目录校验并加载（文件名以本包 README 为准）：

```bash
sha256sum -c SHA256SUMS
docker load -i argus-images-<SHA前12位>.tar.gz
```

回到**原部署目录**，备份当前 Compose 文件和它使用的环境文件，然后按本包标签修改现有服务的 `image` 字段。
保留原项目名、端口、环境变量、密钥文件、卷及外部网络。Demo 只在原来已部署且本次需要更新时处理。
不要把仓库演示 Compose 覆盖到现场。

在停写窗口排空未结束 Launch 后，沿用原来的 Compose 命令，只重建选定应用：

```bash
docker compose -f compose.yaml config -q
docker compose -f compose.yaml up -d --no-deps --no-build --pull never eval-runner argus-console
docker compose -f compose.yaml ps
docker compose -f compose.yaml logs --tail 80 eval-runner argus-console
```

若 Demo 也需要升级，在 `up` 的服务列表追加 `demo-agent-v1 demo-agent-v2`。
原命令使用 `-p`、`--env-file`、多个 `-f` 或其他文件名时原样保留；避免误建另一个项目。
不先执行 `down`，不升级/重建数据库、Redis 或 Langfuse。

配置和迁移都没有变化时，启动失败可以改回备份中的旧镜像标签，按相同方式重建应用。
如果本版本执行过迁移，按交付方给出的兼容性和备份恢复说明操作，不默认认为旧镜像兼容新 Schema。

## 从旧工具切换到手工维护

已有自己的 `compose.yaml`：继续使用原文件即可。

旧工具生成的是 `compose.json`（同样是有效的 Compose 配置），实际文件位置在 `active.json` 的 `deployment` 目录，
项目名在 `site.json` 的 `project`。首次切换时只需确认这两个值，继续沿用实际配置文件和项目名，**无需重新 init/adopt**。

手工修改后，以实际 Compose 为部署事实；旧工具的 `model.json`、`active.json` 不会自动同步。
以后不要混用旧 `linux-upgrade.sh` / `linux-rollback.sh`，避免用旧模型覆盖现场修改。旧备份和密钥文件保留。

## 交付方的责任

固定应用源码 SHA，构建 linux/arm64 镜像，核对 OCI revision、Runner 内嵌 SHA 与实际归档身份。
交付前比较上一交付的 `migrations/` 与必需配置；镜像不一定包含挂载在宿主机的迁移文件。
当前 Runner 镜像的迁移目录依赖外部挂载，因此新增迁移时必须额外交付对应文件和明确备份步骤，不能只改标签。

在公网侧执行源码门禁和镜像启动/Console 配置检查。目标鲲鹏内核、真实 Langfuse 和业务/UI 验收由内网完成，
公网构建或 Mac Docker 成功不能代替这些证据。
