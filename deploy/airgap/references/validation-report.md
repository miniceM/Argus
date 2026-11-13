# 本次工具验证报告

日期：2026-11-09（Asia/Shanghai）。结论：Skill操作工具的本地检查和隔离存储恢复通过；真实Argus镜像构建、麒麟/鲲鹏运行、已有Langfuse业务E2E及浏览器UI验收尚未执行。交付的是可复用Skill工具包，不含本次Argus应用镜像或内网验收结论。

## 已执行

| 检查 | 结果与范围 |
|---|---|
| 固定源码审计 | 读取miniceM/Argus完整SHA 516ac091dbe4785bea2c755d5708920713359550的Dockerfile/Compose/启动/迁移/API/测试/版本；关键文件哈希清单随包 |
| Python3.8语法兼容 | 用AST按3.8语法解析操作脚本通过；实际宿主执行为Python3.11，未在宿主Python3.8解释器上运行 |
| Shell入口 | 12份shell脚本bash -n及各自--help通过；未因帮助命令触发Docker部署 |
| 本地自动检查 | 最终显式启用隔离Docker测试，21项全部通过，耗时30.245秒；详见validation-results/tests.log |
| 打包到接收流程 | 真实Git提交/未提交内容隔离、git archive、含空格输出路径、工具拷贝、gzip/分卷/双层校验/安全解包/源码比对通过；另对实际目标SHA的1120个源码文件完整执行打包/接收；两者镜像构建均替换为模拟Docker归档，不能算实际三镜像构建成功 |
| 拒绝路径 | 篡改/缺失/混入分卷、外层清单hash错误、内部额外文件、路径穿越/链接/重复成员、错误架构/内嵌SHA、旧迁移变动、备份失败阻止候选Runner均通过 |
| 隔离与身份判定 | 候选只改变应用并保留基础配置/凭证；仅Runner可连LF；LF内部URL选定容器；PG/Redis DNS歧义及错误数据库拒绝；PARTIAL_FAILED终态处理通过 |
| 远端证据判定 | 模拟响应下正确身份通过，错误Score subject/source/value/generation、Experiment join、Trace与重复Score拒绝；分页重复游标拒绝。未查询真实LF作为此项测试 |
| 实际PG/Redis恢复 | 本机ARM64 Docker全新随机临时项目，使用已有postgres:17/redis:7，不pull，不接真实LF；custom dump、临时库pg_restore、Schema/数据及表授权恢复、Redis AOF恢复均通过 |
| 恢复时保全 | 升级后数据库保留为argus_hold_*、原Redis卷保留、PG容器ID及测试外部网络ID不变；数据库改名前事务日志含恢复库和新Redis卷，检查通过 |

存储恢复测试明确mock了Argus应用启动；测试外部网络是本次临时空网络。因此其PASS仅说明真实存储操作与数据保全，不能证明真实Argus启动或真实Langfuse环境未受影响。测试清理仅操作自身随机project/site标签，不运行全局prune或down -v。

## 未执行，必须保留NOT_RUN

- 本次目标SHA的make validate、Console测试、trajectory-test、远端CI或Cloud验收；本Skill没有替用户创建源码PASS回执。
- Mac上的实际Runner/Demo/Console三镜像构建，以及由其生成的正式大体积分卷包；打包流程测试使用模拟镜像。
- 麒麟/鲲鹏920 ARMv8.0上Python/SSL/psycopg/cryptography/Nginx原生启动、整机离线网络条件和持久化重启。
- 已有Langfuse4.68.0/ClickHouse26.11.1.782上的Dataset、Trace、Experiment与v3 Scores联查。
- 新SHA正常v1/v2 Launch、不可达Agent、冻结Snapshot与Gate业务端到端，以及Console/LF浏览器UI。
- 现场已有Registry/历史Snapshot/策略/业务凭证恢复，以及真实业务回滚后的验收。
- Managed EVAL、真实LLM、Token/费用与Agent内部轨迹；Builtin测试不代替这些能力。

## 复现工具测试

在Skill目录外也可以运行，变量指向解压目录：

```bash
python3 -m unittest discover -s "$SKILL/tests" -v
RUN_DOCKER_TESTS=1 ARGUS_TEST_REPORT="/受控目录/docker-restore-result.json" \
  python3 "$SKILL/tests/test_docker_restore.py" -v
```

第二条只适合已有本地ARM64 PG17/Redis7镜像且可创建临时Docker资源的测试机。现场业务验收按[验收清单](acceptance-checklist.md)执行；API报告保持API_PASS_UI_AND_SOURCE_EVIDENCE_PENDING，最终ACCEPTED由操作者根据完整证据记录。
