# Console 在大内存页内核上启动失败

症状：`zone "argus_runner" is too small`，Console 不断重启。

Nginx upstream zone 源码要求 `size >= 8 * ngx_pagesize`（`src/http/modules/ngx_http_upstream_zone_module.c`）。
原配置 `64k` 在 4 KiB 页上可用，但在 16 KiB、64 KiB 页内核上不足；ARM64 镜像标签不代表内核页大小。
在目标宿主机运行 `getconf PAGESIZE` 确认。现改为 `1m`，覆盖这三种页大小。
参考源码：https://github.com/nginx/nginx/blob/release-1.29.3/src/http/modules/ngx_http_upstream_zone_module.c

以前的离线导入只执行 `nginx -v`，没有读取模板生成后的配置，因而遗漏此问题。
现在导入经正常 entrypoint 执行 envsubst，再运行 `nginx -t`；使用回环上游和 DNS 地址，保持 `--network none`，不依赖 Runner 已运行。
此检查必须在目标内核执行，通过后才继续安装/升级。Mac Docker 检查不能替代鲲鹏验收。

## 临时恢复当前 Console

正式修复应使用包含上述改动的新版本离线包。急需恢复时，可仅修改当前 Console 容器的模板；
这属于临时现场修改，重建容器会丢失，不能据此宣称旧镜像已修复。不会改动密钥、数据库、Runner 或 Langfuse。
以下脚本从现有 site.json 精确定位 Argus Console，备份原模板，仅允许将已知的 `64k` 配置替换为 `1m`。
Docker cp 支持已停止/重启中的容器，不需要先成功启动 Nginx。

```bash
python3 - <<'PY'
import json, os, re, subprocess, tempfile
from datetime import datetime
from pathlib import Path
root = Path('/data/argus-offline')
project = json.loads((root / 'site.json').read_text())['project']
cids = subprocess.check_output(['docker', 'ps', '-aq', '--filter',
    'label=com.docker.compose.project=' + project, '--filter',
    'label=com.docker.compose.service=argus-console'], text=True).split()
if len(cids) != 1:
    raise SystemExit('需要唯一 Console 容器，实际数量：' + str(len(cids)))
cid = cids[0]
backup = root / 'evidence' / ('console-pages-' + datetime.now().strftime('%Y%m%d%H%M%S'))
backup.mkdir(parents=True, mode=0o700)
original = backup / 'default.conf.template.before'
subprocess.run(['docker', 'cp', cid + ':/etc/nginx/templates/default.conf.template', str(original)], check=True)
text = original.read_text()
fixed, count = re.subn(r'(?m)^(\s*zone\s+argus_runner\s+)64k(\s*;)', r'\g<1>1m\2', text)
if count != 1:
    raise SystemExit('模板不符合预期，保留现场并停止；未写入任何配置')
with tempfile.TemporaryDirectory() as td:
    path = Path(td) / 'default.conf.template'
    path.write_text(fixed)
    os.chmod(path, 0o644)
    subprocess.run(['docker', 'cp', str(path), cid + ':/etc/nginx/templates/default.conf.template'], check=True)
subprocess.run(['docker', 'restart', cid], check=True)
subprocess.run(['docker', 'exec', cid, 'nginx', '-t'], check=True)
subprocess.run(['docker', 'exec', cid, 'wget', '-qO-', 'http://127.0.0.1:8080/health'], check=True)
print('Console 配置与健康检查通过，原模板备份：' + str(original))
PY
```

最后确认 Console 页面和 API 代理可用；本步骤不代表业务评测或 Langfuse 端到端验收。
