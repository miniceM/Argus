---
name: argus-airgap-update
description: 为 miniceM/Argus 交付 ARM64 离线应用镜像；日常默认加载镜像并修改存量 Compose，复用已有 Langfuse、数据库、Redis、网络和密钥。首次安装或数据库迁移另走专项流程。
---

# Argus ARM64 离线交付

面向公网 Mac ARM64 构建、麒麟/鲲鹏内网部署。**日常默认交付镜像文件，由操作者修改现有 Compose 的 image 字段。**
不为普通镜像升级要求重新安装工具、init/adopt、维护多份状态文件或串联接收/升级脚本。

## 默认路径：只更新 Argus 应用

先读 [镜像更新说明](references/image-update.md)。

1. 确认本次目标 ref，固定完整 Git SHA。核对当前交付与上一版的应用、`migrations/` 和必需环境/挂载差异。
2. 复用已构建且身份匹配的镜像，或从该 SHA 的 git archive 构建 linux/arm64 镜像。Runner 用真实构建参数注入 SHA，不能在运行时伪造版本。
3. 只交付本次需要的 Argus 镜像：Runner、Console，按需附 Demo（一个镜像供 v1/v2 使用）。不打包/升级存量 Langfuse、ClickHouse、PG、Redis。
4. 用标准 docker save 导出镜像，gzip 压缩。交付一个镜像包、IMAGES.json、SHA256SUMS 和简短 README。
   README 写出本次固定标签和可复制命令；镜像清单注明相对上一版的迁移/配置差异，不能让操作者自己推断。
5. 单文件不超过 280,000,000 字节时不分卷；超限才分卷。独立告知校验清单自身 SHA256。
6. 在公网侧核对镜像架构/OCI revision/Runner 内嵌 SHA；执行适用源码门禁和真实镜像配置检查。
   Console 经正常 entrypoint 渲染后执行 nginx -t；ARM64 标签和本机测试不代表鲲鹏内核已验证。
7. 内网步骤收敛为：校验 → docker load → 备份并修改原 Compose 的镜像标签 → 停写排空 → 只重建指定应用 → 看状态/日志及现场验收。
   沿用原工作目录、项目名、环境文件和 Compose 参数，使用 --no-deps --no-build --pull never。

沿用现有 Langfuse 对接、数据库/Redis、网络、卷和密钥。没有配置差异时不要求重新输入 URL、网络名、项目名或密钥。
不提供第二套日常升级入口或新的自动部署状态机。不得用仓库演示 Compose 覆盖现场。

## 有迁移或必改配置时

镜像更新的前提要由交付方审核。本仓库当前 Runner 的 migrations/config/data 依赖宿主机挂载；
新增迁移不能只改镜像标签，必须交付对应文件及专项说明。保留已发布迁移 checksum，启动新 Runner 前备份。
新增/调整配置应给出最小 Compose 差异，不默默重建站点。

Managed Secret 沿用现有主密钥与管理 Token，规则见 [站点密钥](references/managed-secrets.md)；
数据库备份不能替代密钥备份。密钥丢失、冲突或历史密文不可解密时停止，不生成替代密钥掩盖问题。
涉及 Schema 的恢复按兼容性与指定备份处理，不把旧镜像切回当作通用数据回滚。

## 专项路径：仅在需要时使用

首次安装、旧部署接管、全量备份恢复或复杂迁移，按 [高级流程](README.md#高级流程首次安装接管与备份恢复)、
[操作约束](references/operations.md)、[源代码审计](references/source-audit.md) 选择既有脚本。
保留这些工具作为专项能力，不把整套流程作为每次镜像升级的前置条件。

旧工具生成的 compose.json 可继续作为存量 Compose 使用，项目名保持原值。
切换为手工维护后，旧 model.json/active.json 不会自动同步，不混用旧升级/回滚脚本覆盖现场。

## 交付边界

- 只更新 Argus；不操作现有 Langfuse/ClickHouse 或基础镜像/数据卷，不 down -v、不 prune。
- 原有数据库/密钥/持久化配置保持，外部写入需停写，未结束 Launch 不自动取消或重放。
- 源码、构建、CI、本机 Docker 与内网验收分开报告。未实际连接内网时明确 NOT_RUN。
- 操作者最终确认目标 CPU 启动、既有凭据、历史数据、Console 与业务评测；不把镜像导入成功当作验收完成。
