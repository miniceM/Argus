# 根因修复回执

日期：2026-11-13。用户授权范围：实现根因修复、更新离线工具并制作供手工传入内网的包。

## 完成内容

- 将离线工具纳入 deploy/airgap，随完整源码 SHA 版本化，并加入 make validate。
- 首次安装随机生成站点级 Keyring/管理 Token；只读挂载、受限权限，不写入镜像或源码。
- 旧 config/.secrets 原样迁移，升级保留密钥；迁移前用候选镜像实际解密全部历史修订。
- 普通数据库备份排除 Keyring；单独 secret-recovery 集合保存受限文件，恢复前核对依赖。
- Nginx 基于实际容器 DNS 动态解析 Runner；部署预检经 Console 核对真实 build_id。
- 页面默认读取服务环境，管理授权配置与 Provider 错误分别显示安全诊断。
- 保留上一批交付工具已验证的 OCI manifest/config Image ID 兼容处理；wrapper 禁用 pycache，防止修改已校验载荷。

## 已观察到的验证

- RED：接口安全错误代码缺失；页面环境/503 错误映射回归失败；旧 Console 镜像在 Runner 换 IP 后 20 秒内不能恢复。
- GREEN：接口凭据测试 36 passed；Console 相关单测 9 passed；新 Console 镜像无重启自动恢复，同 URI/query 保持正确。
- Console 全量 E2E：141 passed。环境相关用例显式选择其要测试的 production；保留真实服务环境默认值断言。
- 真实隔离 PG/Redis 备份恢复：PASS，包含表计数、Schema、ACL、Redis 数据与保留升级后数据。
- 正式 initial_model 的初步隔离 Compose 回归：PASS。使用上一版 Runner/Demo 与修复后的 Console 测试镜像，
  创建凭据、持久化 AgentVersion 引用、真实 HTTP 调用 Demo、Runner 重建后再次调用、历史密文解密、PG 恢复演练均通过。
  此初步证据不冒充最终交付 SHA；打包后的精确制品与跨 SHA 测试回执另随交付包提供。
- make validate：PASS（Python 1736 passed / 39 skipped / 32 deselected；Console 915 passed；Compose 静态校验通过）。
- 工具单元测试：34 项，31 passed、3 项容器集成默认跳过；容器集成分别显式执行，结果另记。
- 最终精确制品回执放在交付目录 evidence；源码 SHA、镜像/分卷身份由 PACK-RECEIPT 固定。

## 边界

未连接内网，未修改已有 Langfuse/ClickHouse、业务容器或基础服务。上述测试使用独立临时资源，
合成密钥随临时目录清理。完整 Langfuse 业务 E2E、鲲鹏 CPU、现场历史数据/UI 验收仍须内网执行。
旧备份没有独立密钥依赖记录时拒绝自动回滚，需先核对原文件和恢复方案；不会静默制造新密钥。
现场已公开固定管理 Token 的轮换与 AES 主密钥轮换分别处理，升级不擅自覆盖已有文件。
