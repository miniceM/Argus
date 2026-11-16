# 离线部署 Managed Secret 故障根因调查

调查日期：2026-11-13。检查对象：Argus 提交
`bb30177e9d0a30dbbd4840b5f67bd4e02004172e` 及该次实际交付工具。
本记录是源码核查与隔离复现，不是根因修复已交付、内网重新部署或业务验收通过的声明。

## 结论

问题主要在应用能力与离线部署契约没有形成闭环，不是主密钥应该写进镜像。
镜像包含 Managed Secret 实现；站点专用 Keyring、管理授权文件需要在安装时安全配置，
并在升级、备份和恢复时保持可用。上一包只交付了前半段，将站点适配留给人工；
凭据创建功能因而无法开箱运行。该缺口应进入正式安装/升级流程，而非反复提供临时脚本。

## 已验证的原因

| 层面 | 代码事实 | 影响 |
|---|---|---|
| 站点初始化 | 离线 `deploy.py:initial_model` 仅配置 config/data/migrations 三个源码挂载，没有两个 Managed Secret 文件变量或挂载 | 默认安装没有启用凭据写入所需配置 |
| 更新 | `candidate_deployment` 复用旧环境配置，仅替换镜像与三个源码挂载 | 更新镜像不会补齐缺失的站点 Secret；手工放入旧 config 的文件也不会跟随挂载切换 |
| 错误诊断 | `api_credentials.authorize` 在管理授权文件不可读时也返回 503；`CredentialsPage` 把所有 503 都解释为主密钥缺失 | 页面提示不能单独证明具体缺失的是哪个文件，更不能证明 Nginx 缓存旧 IP |
| 代理 | Nginx 模板的 proxy_pass 是启动时展开的静态主机名，没有动态 upstream resolve 配置 | Runner 换 IP 后，持续运行的 Console 可能仍连接旧地址 |
| 健康检查 | Console `/health` 固定返回 200；升级流程检查容器健康，`up` 并不保证重启配置未变化的 Console | API 代理已失效时容器仍可被判为健康 |
| 环境默认值 | CredentialsPage 使用固定 production 初始值，没有从已有 system/info.environment 初始化 | 在 argus-kunpeng-test 等站点容易创建到错误环境；这不引起主密钥 503，但会影响后续凭据绑定 |
| 验收范围 | 原生库导入、Nginx 静态页、源码/API 单测与前端测试未覆盖实际离线 Compose 的 Managed Secret 创建和升级存续 | 镜像/分卷校验通过不能证明该安装方式下凭据可用 |

应用中的拒绝逻辑本身需要保留。`credentials.protected_file` 拒绝缺失、不可读或
组/其他用户可读的文件；ManagedSecretProvider 还校验 Keyring 的 active key 和
32 字节 AES 密钥。正确修复是补齐部署，不是放宽权限、在镜像里放共享密钥或匿名降级。

## 实际复现与证据边界

- [部署流程合成复现](deployment-probe.json)：核对实际交付 toolkit.tar.gz 的 SHA256；
  其 initial_model、candidate_deployment、snapshot_mounts、activate_apps 与检查的工具函数 AST 一致。
  验证默认配置缺少 Secret、升级后旧 config Secret 不再位于新挂载中、源码备份会带入旧 config Secret。
  所用文件均为合成内容，不是现场密钥。
- [真实 Docker 复现](dns-probe.json)：使用本次实际 Console 镜像与隔离的合成 HTTP 后端。
  初始代理成功；强制后端服务名保持不变而 IP 改变后，API 访问失败，但 `/health` 仍成功；
  只重启 Console 后相同请求恢复。URI 和查询参数均保持一致。
- 镜像内 nginx.conf.template 与该 SHA 仓库模板逐字一致；本机没有发现镜像打包遗漏模板。
- [应用反例测试日志](application-tests.log)：运行既有缺少主密钥/授权配置、错误环境/已停用凭据绑定测试，2 passed。
- 未连接内网。现场脚本一次修改了密钥、环境、Runner 和 Console，因此不能据此归因现场所有故障；
  上述 Docker 复现证明代理问题真实存在，不证明它一定是现场最初 503 的来源。
- Nginx 文档经 Context7 `/websites/nginx_en` 核对：动态 upstream `resolve` 需要 resolver 与共享 zone；
  对应开源支持自 1.27.3 起可用。参考 [HTTP upstream 文档](https://nginx.org/en/docs/http/ngx_http_upstream_module.html#resolve)。

## 临时处理为何不能变成正式方案

1. 将 `.secrets` 写进 `/app/config`，意味着密钥跟随某个 release/source 目录。
   升级会把该挂载切到新 SHA 的源码目录，但保留旧环境变量路径；下次升级可能再次不可用。
2. `snapshot_mounts` 会完整复制 config，所以该做法会把主密钥带入包含数据库备份的备份集合，
   破坏既有文档要求的独立安全备份边界；也会改变原本应不可变的解包源码文件集合。
3. 固定管理口令且每次覆盖文件会造成共享口令和意外轮换。正式流程应生成或接入站点独有随机授权，
   已有文件不覆盖、不静默轮换；管理授权与登录 JWT 是不同契约。所示脚本没有签发用户 JWT。
4. `docker cp` 改的是容器可写层，重建后会丢失，且镜像 ID 无法表示这项现场变更。
   若只需刷新 DNS，复制模板并不能解决下一次 IP 变化；模板修复必须进入源码和镜像。

已暴露在说明/对话里的固定管理授权应另行轮换。不要为轮换管理授权而重新生成 AES 主密钥；
现有密文及历史备份仍依赖原 Keyring。

## 根因修复与必需验收

以下是调查时提出的修复要求；本轮实现与验收单独记录于 [根因修复回执](fix-receipt.md)，现场验收仍未执行。

1. **站点 Secret 生命周期**：复用现有 ManagedSecretProvider 和受限文件方式。首次安装在源码、
   release 及数据库备份目录之外配置站点专用文件，以只读方式挂载；现有企业文件可直接接入。
   升级保持路径和内容，禁止静默生成替代旧 Keyring；旧密文存在但密钥缺失时失败关闭。
   为现场 config/.secrets 提供明确的迁移/核验路径，历史解密成功后再切换挂载；不把主密钥打入普通备份。
2. **部署可用性检查**：迁移前核对配置与文件权限，安装后在容器中验证 Provider，且不输出密钥。
   增加安全的配置状态/能力诊断，分开管理授权不可用、加密 Provider 不可用与代理不可达。
   未配置该能力时明确显示不可用，不能让用户提交后才从笼统 503 猜配置。
3. **代理自恢复**：在受支持的 Nginx 镜像中固化动态 DNS 解析配置，保持 API URI、查询参数与超时契约。
   在 Runner 重建并更换 IP、Console 不重启的情况下验证有界恢复；独立检查 Console 到真实 Runner 的
   system/info 与 build_id，保留 liveness 与业务 readiness 的区别。
4. **环境与交互**：凭据创建默认读取 system/info.environment，保留显式选择其他环境的能力；
   不因默认值问题放宽版本/Launch 的环境隔离规则。对稳定错误代码映射安全提示，不显示原始代理响应。
5. **实际离线部署闭环**：用正式交付的 Compose 和脚本，在隔离测试站点验证首次安装 → 创建凭据 →
   Agent 引用/调用 → 更新重建 Runner → 原凭据仍可解密/调用 → Console API 自动恢复。
   覆盖已有密钥不覆盖、未配置/权限错误/密钥丢失失败关闭、无明文回显、旧备份恢复依赖及错误环境拒绝。
   源码门禁、镜像导入、该部署闭环和鲲鹏现场验收分别记账。

消费者是使用离线包进行首次安装和持续更新的 Argus 部署方；修复降低接入成本并提高执行可靠性。
权威配置仍归站点部署，密文归现有数据库；不扩建 Vault/IdP、不改 Langfuse、不放宽授权或环境边界。
