# 站点密钥与升级

消费者是使用 Argus 离线包的部署管理员。此修复补齐凭据接入、升级可靠性与恢复证据，复用现有
ManagedSecretProvider 和受限文件，不引入 Vault、登录系统或生产部署权限。数据库持有密文，站点持有主密钥。

## 新安装

init 在 ROOT/secrets 创建独立随机的 keyring.json、admin-token（目录 700、文件 600）。
install 以两个只读文件挂载到 /run/argus-secrets，并设置对应 *_FILE 变量；密钥不进入镜像、源码或 release。
首次启动前使用镜像中真实 Provider 验证配置。不会在输出中显示 Token。
管理员在受限终端按需读取 ROOT/secrets/admin-token 用于 Console 写操作；它不是登录 JWT。
不要把文件内容贴入 Issue、共享日志或验收报告。

## 已由旧工具管理的站点

继续使用现有 site.json/active.json，运行新版 linux-upgrade.sh；不要重复 init/adopt。
候选生成阶段在切换源码挂载前处理旧配置：

- 原来未启用且数据库无密文：自动创建站点随机文件。
- 现场曾配置 /app/config/.secrets：解析原只读挂载，保留原文件，将字节原样复制到 ROOT/secrets。
- 已配置路径但文件丢失、权限过宽、稳定目录与旧文件内容冲突：停止，不静默生成或覆盖。
- 已有密文但没有原 Keyring：停止，先从独立保管介质恢复原 Keyring。

迁移前以候选镜像、network none、不导入 app.main 的探针实际解密所有历史修订。
Key ID 相同但密钥字节错误同样失败；不输出明文，不执行数据库迁移。
后续升级沿用稳定路径与原字节；切换源码不会切走密钥。旧 .secrets 保留供核查，普通源码备份排除该目录。
其他自定义挂载仍拒绝自动接管，需明确适配。

## 备份和恢复

普通 backups/backup-* 中包含数据库密文及 secret-dependencies.json（受限路径、SHA256），不包含 AES 主密钥文件。
每次备份另建 ROOT/secret-recovery/<时间>/，其中保存两份受限文件及校验清单。
应将这个集合复制到独立受控介质，与普通数据库备份分别保管；同一物理盘无法承担灾备。
不应对整个 ROOT 做可公开共享的打包，它包含站点配置和密钥。

恢复前验证 secret-dependencies.json 指向的原文件权限与字节摘要；失败时先恢复对应独立密钥副本，不能生成替代 Keyring。
旧工具产生、未记录独立密钥依赖的备份不自动回滚，需核对原文件及恢复方案后适配，避免恢复后密文永久不可用。
管理 Token 轮换与 AES Keyring 轮换分别处理；不为更换管理 Token 而重建主密钥。

## 代理和页面

Console 镜像通过启动钩子读取实际 resolv.conf，使用 Nginx upstream resolve + zone，DNS 缓存有效期 2 秒。
Runner 更换 IP 时 Console 无需重启；原 URI/query 保留。可用 ARGUS_DNS_RESOLVERS 显式指定 DNS。
/health 仅为存活检查，部署预检另经 Console 代理核对 system/info.build_id。
凭据环境默认读取服务环境，允许显式覆盖；跨环境绑定仍拒绝。
503 的安全响应头 X-Argus-Error-Code 区分管理授权文件和 Provider 不可用；未知代理 503 不被误诊为主密钥缺失。

## 回归

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s deploy/airgap/tests -v
RUN_DOCKER_TESTS=1 PYTHONDONTWRITEBYTECODE=1 python3 deploy/airgap/tests/test_docker_restore.py -v
RUN_DOCKER_TESTS=1 ARGUS_TEST_CONSOLE_IMAGE=本次Console镜像 ARGUS_TEST_DEMO_IMAGE=本次Demo镜像 \
 PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s deploy/airgap/tests -p test_console_dns.py -v
RUN_MANAGED_COMPOSE_TEST=1 ARGUS_TEST_RELEASE=本次staging/release.json \
 PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s deploy/airgap/tests -p test_managed_compose.py -v
```

最后一项使用正式 initial_model、独立 PG/Redis/网络与动态应用镜像，关闭 Worker 和 YAML 导入，
不连接真实 Langfuse、不发布宿主端口；只清理测试自己创建的资源。不得把该证据写成完整 Langfuse 业务验收。
