# 2026-11-13 本次交付工具适配

仅本次工具快照调整，未修改已安装技能或 Argus 源码。

- 真实 Docker Desktop 28.5.1 containerd 后端的 inspect Image ID 是 OCI manifest digest；传统后端为 config digest。load 同时支持二者，但 OCI manifest 必须重新计算 SHA256 并精确引用归档中的同一 config；无关同 tag、损坏及错 config 仍拒绝。
- 新增 6 项回归测试，实际 RED 为 4 errors、2 passed，修正后工具全套 26 passed、1 skipped。Docker 恢复专项未在本轮运行。
- 原技能 SHA256SUMS 对 pack.py 的记录过期；本次审阅实际脚本，并重新生成工具快照校验清单。
- ExFAT 会生成 ._ AppleDouble 文件，原脚本严格文件集合检查会因此拒绝。本次源码、staging、归档及分卷全部在本机 APFS 生成与验证；仅最终归档、分卷与交付证据复制到外置盘。不把 AppleDouble 放入载荷。
- 现场仍需执行 linux-load 的当前鲲鹏 CPU 原生导入检查；Mac 的成功不能代替 ARMv8.0 验收。
- Managed Secret 的额外文件挂载仍须按 operations 的自定义站点条款先适配；本次不自动生成/轮换/迁移现场密钥。
