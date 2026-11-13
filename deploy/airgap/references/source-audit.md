# 固定源码与部署契约审计

审计日期：2026-11-09（Asia/Shanghai）。仓库：miniceM/Argus。此次获取的 origin/main 为完整提交 `516ac091dbe4785bea2c755d5708920713359550`，提交说明为 `docs: narrow Argus scope to governed evaluation and release evidence (#233)`。这只是本次审计快照；未来使用时重新获取、解析并审查目标 SHA。

本次使用独立 detached 快照读取真实代码，没有修改用户原仓库的工作树、分支或已有未提交文件。关键文件的 SHA256 在 [source-files.sha256.json](source-files.sha256.json)。该清单用于定位本次审计代码，不能代替每次交付包自己的内部校验清单。

## 已验证历史与本次工作范围

原对话用户确认 Langfuse v4.68.0 与 ClickHouse26.11.1.782 ARMv8.0兼容制品在鲲鹏运行，OTLP Trace、Dataset创建/读取、v3 Score查询和UI Score显示通过。旧Argus离线包为 `369bad665abd639d5950df026fe6ca5855fc0dbf`，镜像/源码/ARGUS_BUILD_ID内部校验、分卷校验和gzip完整性通过。

这些历史记录没有证明Argus应用已在麒麟成功安装，也没有证明新SHA的Launch、Snapshot、Gate或真实模型验收。本Skill保留这一证据边界。此次只制作工具与文档，并在本机隔离项目测试存储恢复；未操作内网服务器或已有Langfuse部署。

## 构建、版本与测试环境

| 真实来源 | 观察 | 对部署的影响 |
|---|---|---|
| services/eval-runner/Dockerfile | python:3.14-slim；ARG ARGUS_BUILD_ID=dev；ARG RUNNER_VERSION=0.1.0；pip安装requirements；uvicorn app.main:app | 构建时传完整SHA并保留镜像内嵌值；目标机先测Python/SSL/psycopg/cryptography等原生导入 |
| services/demo-agent/Dockerfile | python:3.14-slim；独立HTTP Demo应用 | 同一Demo镜像运行v1/v2两个容器；不把平台评测SDK注入Agent |
| services/console/Dockerfile | Node26 Alpine、corepack0.36.0、pnpm10.5.2构建；nginx-unprivileged1.29 Alpine运行 | 公网Mac完成所有依赖安装/前端构建；内网只导入镜像并启动Nginx |
| services/console/package.json | Console组件版本0.2.0 | 与Runner0.1.0、CLI0.1.0分别记录，不虚构统一根版本 |
| services/argus-cli/pyproject.toml | argus-release-cli0.1.0，要求Python>=3.12 | 本Skill未在麒麟另装CLI；操作工具用宿主Python3.8+标准库，应用用镜像内Python，两者要求不同 |
| .github/workflows/ci.yml | Python3.12；Console相关任务Node22；单测PG16 Alpine | CI环境与本部署的Python3.14/Node26构建/PG17不同，CI不能代替目标镜像和目标CPU运行证据 |
| Makefile / scripts/validate.sh | make validate含静态、OpenAPI、Console、Python测试；console-e2e先build；trajectory-test独立门禁；部分工具缺失可产生SKIP | 保留实际日志与各项状态；需要轨迹能力时补独立门禁；SKIP或skipped_by_policy不能记为实测PASS |

三份Dockerfile及基础镜像标签、Python依赖范围不是完整可复现构建锁。Mac打包器记录本次Image ID、OCI revision和Dockerfile哈希；相同SHA重新构建也可能不同。对已有同tag不同Image ID的目标机明确停止，不能把不同制品冒充原包。

## 原Compose不能直接用于内网更新

根docker-compose.yml是完整PoC栈：Langfuse Web/Worker4.38.0、ClickHouse25.12及其PG/Redis/MinIO与Argus混合部署。Makefile up会build全栈，clean会down -v。这与已经验证的Langfuse4.68.0/ClickHouse26.11.1.782现场不一致。

因此本Skill生成只含六个Argus服务的独立Compose：Argus专用PG17、Redis7、Demo v1/v2、Runner、Console。Langfuse网络是external，PG/Redis是external命名卷，更新只指定四个应用且使用no-deps/no-build/pull never。新私网为internal桥接网络；只有Runner同时连Langfuse网络。原LF容器/镜像/挂载/网络ID均固定核对。

Runner在双网中使用通用postgres/redis名称有误解析到LF组件的风险。新站点使用argus-postgres/argus-redis别名；接管时保存实际Argus容器名作为下一次连接地址，且预检当前Runner的真实DNS解析必须唯一指向Argus实例，数据库名/用户必须与备份对象一致。若旧部署已经存在歧义，先诊断，不能备份错误数据库后继续升级。

## 启动、迁移、Registry与凭证

| 真实来源 | 已核对的行为 |
|---|---|
| services/eval-runner/app/main.py | 导入app.main时就初始化数据库/迁移；Worker/Reconciler/Syncer在同一Runner进程生命周期内。仅暂停浏览器不等于停写 |
| services/eval-runner/app/db.py / migrations | MigrationRunner按version及SQL文本SHA256检查已执行迁移，不提供down迁移。改旧SQL或只切回旧镜像都不能恢复旧Schema |
| services/eval-runner/app/config.py / registry.py | Registry以数据库为SSOT，YAML用于bootstrap；更新关闭ARGUS_AUTO_IMPORT_YAML，保留已有AgentVersion和配置 |
| services/eval-runner/app/credentials.py / secret_providers.py | 加密凭证与密钥/授权文件相关；生产环境不能把env开发凭证当正式方案。备份必须保存真实文件/环境，而非仅文件路径 |
| services/console/nginx.conf.template | Console静态资源由Nginx提供，API代理到RUNNER_HOST/RUNNER_PORT；首页HTTP200不足以证明API指向正确版本 |

load镜像的CPU检查刻意不导入app.main，避免备份之前执行迁移。正式升级顺序为：迁移不可变校验、停写/排空、停应用、PG完整dump与真实临时库恢复、Redis SAVE后停机复制/data、保全制品/源码/配置，完成校验后才能启动候选Runner。

旧部署接管只接受标准三份只读Runner目录、独立命名PG/Redis卷和六个服务。额外Secret/Config/私有CA挂载需显式扩展备份与恢复；不能自动删去未知挂载。实际.env/Compose与容器环境不符也会停止。

## 验收API与结果身份

基于models.py、api_launches.py、api_results.py、api_release_gates.py、release_gates.py、manifest.py、result_snapshots.py、worker.py、langfuse_projection.py、langfuse_sync.py及docs/openapi.json核对：

| 操作 | 实际契约 |
|---|---|
| 构建身份 | GET /api/v1/system/info的build_id；同时核对实际容器Image ID和镜像内嵌ARGUS_BUILD_ID |
| 注册测试Agent | POST /api/v1/agents；POST /api/v1/agent-versions，后者不是嵌套versions路由 |
| 创建/执行Launch | POST /api/v1/experiment-launches；POST /{id}/run返回202；GET /{id}/items?limit=500返回数组 |
| 终态 | COMPLETED、PARTIAL_FAILED、FAILED、CANCELLED；不能把PARTIAL_FAILED当运行中无限等待 |
| 冻结结果 | POST /{id}/result-snapshots返回RunSummary含snapshot_id；GET /{id}/result-snapshots/{sid}返回固定Detail，不回退latest |
| 发布策略/Gate | POST /api/v1/release-policies；POST /api/v1/release-gates/evaluate；固定策略版本/digest、环境、Launch与Snapshot |
| Manifest | 冻结AgentVersion、具体Dataset版本/Items、Evaluator id/version、质量策略、measurement_digest/contract_digests及Runner身份 |

合成验收使用新的Dataset、Agent和策略，不覆盖原banking-agent或业务数据；四个诊断Builtin evaluator显式固定1.0.0：intent_match、required_tool_match、pii_safe、escalation_match。六项银行用例下，v1预期2/6并Gate FAIL，v2预期6/6并Gate PASS。执行/评测成功与质量通过是不同事实。不可达Agent版本只调用现有Demo容器的未监听端口，预期执行失败及Gate UNKNOWN。

Langfuse v4读取接口沿用仓库核验脚本的字段与游标契约：v2/datasets、dataset-items、v2/observations、v3/scores、experiment-items。分页为data数组与meta.cursor，重复游标拒绝。Builtin真实远程调用Observation为TOOL remote-agent-http，Score来源API，稳定ID为score:<item_execution_id>:gen<dispatch_generation>:<metric>，subject必须指向同一Observation/Trace；Experiment Item还需匹配Experiment、Dataset和Dataset Item。

这一条Builtin链路不证明Managed source=EVAL、真实模型Token/费用或Agent内部轨迹已验收。相关需求必须另测并保留来源，未知不能填0。脚本只重试GET可见性查询，POST超时保留身份诊断。

## 每次目标SHA变化时重核对

比较本次源文件清单和目标文件，重点检查Dockerfile/runtime、环境变量、启动迁移时机、挂载与Secret文件、OpenAPI请求/响应、Dataset和Evaluator语义、Snapshot/Gate证据条件、Langfuse API/Score映射及测试入口。变化触及这些契约时先修改工具并测试，再制作正式包；不能只替换SHA照搬旧脚本。

本Skill本地工具实测及未测范围见 [validation-report.md](validation-report.md)，现场判定见 [acceptance-checklist.md](acceptance-checklist.md)。

## 2026-11-13 根因修复补充

上述 2026-11-09 表格为历史基线，不是本轮工具行为。当前工具由 deploy/airgap 随 SHA 维护：
支持标准两个只读 Managed Secret 文件、旧 config/.secrets 迁移；备份只记录独立密钥依赖，
额外未知挂载仍拒绝。Console 启动读取 DNS，upstream resolve 自动更新，预检通过代理核对 build_id。
细节与失败边界见 [managed-secrets.md](managed-secrets.md)。
本轮未新增迁移或变更请求/响应正文结构；OpenAPI 与生成类型重新导出后无漂移。
