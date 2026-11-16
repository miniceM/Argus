#!/usr/bin/env python3
"""离线交付共用实现；仅使用 Python 3.8+ 标准库。"""
import contextlib
import copy
import datetime
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request

CHUNK = 280000000
APPS = ('demo-agent-v1', 'demo-agent-v2', 'eval-runner', 'argus-console')
SERVICES = ('postgres', 'redis') + APPS
IMAGES = {'eval-runner': 'eval-runner', 'demo-agent-v1': 'demo-agent',
          'demo-agent-v2': 'demo-agent', 'argus-console': 'console'}
SHA_RE = re.compile(r'[0-9a-f]{40}')
TERMINAL_LAUNCH_STATUSES = ('COMPLETED', 'PARTIAL_FAILED', 'FAILED', 'CANCELLED')


class Stop(RuntimeError):
    pass


def need(condition, message):
    if not condition:
        raise Stop(message)


def deployment_root(value):
    root = Path(value).resolve()
    protected = (Path('/data/langfuse'), Path('/data/langfuse-compat'))
    need(root != Path('/') and not any(root == p or p in root.parents for p in protected),
         '安装根目录不允许指向系统根或 Langfuse 目录及其子目录')
    return root


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


def sha_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp-' + stamp())
    with open(temp, 'x', encoding='utf-8') as f:
        os.chmod(temp, 0o600)
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def run(args, *, data=None, stdin=None, stdout=None, timeout=300, env=None, log_stderr=False):
    """不回显命令参数或 stderr，避免命令、Compose、HTTP 错误泄露凭证。"""
    try:
        p = subprocess.run([str(x) for x in args], input=data, stdin=stdin, stdout=stdout or subprocess.PIPE,
                           stderr=subprocess.STDOUT if log_stderr else subprocess.PIPE, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Stop('%s 执行失败或超时；请在本机受限终端诊断' % args[0]) from exc
    if p.returncode and stdout is not None and p.stderr:
        stdout.write(p.stderr)
    need(p.returncode == 0, '%s 返回 %s；已停止（详细错误未输出，以免泄露配置）' % (args[0], p.returncode))
    return p.stdout if stdout is None else b''


def docker(*args, **kwargs):
    return run(['docker'] + list(args), **kwargs)


def inspect_one(kind, ref):
    return json.loads(docker(kind, 'inspect', ref))[0]


def files_under(root):
    root = Path(root)
    rows = []
    for p in sorted(root.rglob('*')):
        need(not p.is_symlink(), '不允许符号链接: ' + str(p))
        need(p.is_file() or p.is_dir(), '不允许特殊文件: ' + str(p))
        if p.is_file():
            rows.append(p)
    return rows


def checksums(root, name='SHA256SUMS'):
    root = Path(root)
    rows = files_under(root)
    with open(root / name, 'w', encoding='ascii') as f:
        for p in rows:
            if p.relative_to(root).as_posix() != name:
                f.write('%s  %s\n' % (sha_file(p), p.relative_to(root).as_posix()))


def read_sums(path):
    result = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        m = re.fullmatch(r'([0-9a-f]{64})  ([^\r\n]+)', line)
        need(m is not None, '校验清单格式错误')
        digest, name = m.groups()
        parts = PurePosixPath(name).parts
        need(name and not name.startswith('/') and '..' not in parts and '\\' not in name,
             '校验清单路径不安全')
        need(name == PurePosixPath(name).as_posix() and name not in result, '校验清单路径重复或不规范')
        result[name] = digest
    need(result, '校验清单为空')
    return result


def verify_sums(root, name='SHA256SUMS'):
    root = Path(root)
    expected = read_sums(root / name)
    actual = {p.relative_to(root).as_posix() for p in files_under(root) if p != root / name}
    need(actual == set(expected), '文件集合与校验清单不一致；缺失/额外文件均拒绝')
    for rel, digest in expected.items():
        need(sha_file(root / rel) == digest, 'SHA256 不符: ' + rel)
    return expected


def safe_extract(archive, destination):
    """拒绝路径穿越、链接、设备、重复成员；不依赖新版 tarfile filter。"""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    seen = set()
    with tarfile.open(archive, 'r:*') as tf:
        for m in tf:
            rel = PurePosixPath(m.name)
            need(m.name and not rel.is_absolute() and '..' not in rel.parts and '\\' not in m.name,
                 '归档包含不安全路径')
            normalized = rel.as_posix().rstrip('/')
            need(normalized not in seen and normalized not in ('.', ''), '归档路径重复或为空')
            seen.add(normalized)
            need(m.isdir() or m.isfile(), '归档禁止链接或特殊文件')
            target = destination.joinpath(*rel.parts)
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tf.extractfile(m) as src, open(target, 'xb') as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                os.chmod(target, 0o700 if m.mode & 0o111 else 0o600)


def image_archive(path, ref=None):
    with tarfile.open(path, 'r:') as tf:
        entries = json.load(tf.extractfile('manifest.json'))
        need(len(entries) == 1, '每个镜像 tar 必须仅包含一个镜像')
        row = entries[0]
        config = tf.extractfile(row['Config']).read()
        cfg = json.loads(config)
        need(cfg.get('architecture') == 'arm64' and cfg.get('os') == 'linux', '镜像不是 linux/arm64')
        if ref:
            need(ref in row.get('RepoTags', []), '镜像归档标签不符')
        return {'id': 'sha256:' + hashlib.sha256(config).hexdigest(),
                'architecture': cfg['architecture'], 'os': cfg['os'],
                'tags': row.get('RepoTags', []), 'config': cfg.get('config', {})}



def archive_image_ids(path, ref=None):
    """只接受归档的 config digest，或已校验且指向该 config 的 OCI manifest digest。"""
    info = image_archive(path, ref)
    identities = {info['id']}
    with tarfile.open(path, 'r:') as tf:
        try:
            index_file = tf.extractfile('index.json')
        except KeyError:
            return identities
        index = json.load(index_file)
        entries = index.get('manifests')
        need(isinstance(entries, list) and len(entries) == 1, 'OCI 镜像索引必须只有当前平台制品')
        descriptor = entries[0]
        digest = descriptor.get('digest', '')
        need(re.fullmatch(r'sha256:[0-9a-f]{64}', digest), 'OCI manifest digest 不合法')
        try:
            body = tf.extractfile('blobs/sha256/' + digest[7:]).read()
            need('sha256:' + hashlib.sha256(body).hexdigest() == digest, 'OCI manifest digest 不符')
            manifest = json.loads(body)
            need(manifest.get('config', {}).get('digest') == info['id'], 'OCI manifest 未指向同一 config')
        except (KeyError, AttributeError, ValueError):
            raise Stop('OCI manifest 结构或内容不符') from None
        identities.add(digest)
    return identities


def environment(config):
    return dict(x.split('=', 1) for x in config.get('Env', []) if '=' in x)


def verify_payload(payload):
    payload = Path(payload)
    verify_sums(payload)
    manifest = read_json(payload / 'release.json')
    sha = (payload / 'ARGUS_BUILD_ID').read_text().strip()
    need(SHA_RE.fullmatch(sha) and manifest['git_sha'] == sha, '构建身份必须为相同的完整 40 位 Git SHA')
    need(manifest.get('format') == 1, '离线包格式版本不支持')
    need(set(manifest['images']) >= {'eval-runner', 'demo-agent', 'console'}, '缺少 Argus 镜像')
    need(set(manifest['images']) <= {'eval-runner', 'demo-agent', 'console', 'postgres', 'redis'}, '包中有范围外镜像')
    need(manifest['source'] == 'argus-source-' + sha[:12] + '.tar.gz', '源码归档名不符')
    for name, item in manifest['images'].items():
        need(item['file'] == 'images/' + name + '.tar', '镜像文件路径不符')
        info = image_archive(payload / item['file'], item['ref'])
        need(info['id'] == item['id'], '归档 Image ID 不符: ' + name)
        if name in ('eval-runner', 'demo-agent', 'console'):
            need(info['config'].get('Labels', {}).get('org.opencontainers.image.revision') == sha,
                 'OCI revision 不符: ' + name)
        if name == 'eval-runner':
            need(environment(info['config']).get('ARGUS_BUILD_ID') == sha, 'Runner 内嵌构建身份不符')
    return manifest


def copy_tree(src, dst):
    files_under(src)  # 预先拒绝链接
    shutil.copytree(src, dst)


def make_tar(directory, output):
    directory = Path(directory)
    with tarfile.open(output, 'w:gz', compresslevel=1, format=tarfile.PAX_FORMAT) as tf:
        for p in files_under(directory):
            info = tf.gettarinfo(str(p), arcname=p.relative_to(directory).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.pax_headers = {}
            with open(p, 'rb') as f:
                tf.addfile(info, f)


@contextlib.contextmanager
def operation(root):
    root = deployment_root(root)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / '.operation.lock'
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise Stop('已有操作锁；先检查事务及进程，不自动移除锁') from exc
    write_json(lock / 'owner.json', {'pid': os.getpid(), 'started_at': stamp()})
    try:
        yield
    finally:
        shutil.rmtree(lock)


def compose(root, deployment, *args, **kwargs):
    site = read_json(Path(root) / 'site.json')
    # 配置全为字面量（美元符号已转义）；不允许调用者 COMPOSE_* 环境覆盖目标。
    env = {k: v for k, v in os.environ.items() if not k.startswith('COMPOSE_')}
    return docker('compose', '--env-file', '/dev/null', '--project-directory', str(Path(root).resolve()),
                  '-p', site['project'], '-f', str(Path(deployment) / 'compose.json'), *args, env=env, **kwargs)


def literal_config(value):
    if isinstance(value, dict):
        return {k: literal_config(v) for k, v in value.items()}
    if isinstance(value, list):
        return [literal_config(v) for v in value]
    if isinstance(value, str):
        return value.replace('$', '$$')
    return value


def write_compose(deployment, config):
    write_json(Path(deployment) / 'compose.json', literal_config(config))
    write_json(Path(deployment) / 'model.json', config)


def service_container(project, service, required=True):
    ids = docker('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project,
                 '--filter', 'label=com.docker.compose.service=' + service).decode().split()
    need(len(ids) <= 1, '服务存在多个容器，拒绝猜测: ' + service)
    if required:
        need(len(ids) == 1, '缺少服务容器: ' + service)
    return inspect_one('container', ids[0]) if ids else None


def fingerprint(container):
    return {'id': container['Id'], 'image': container['Image'],
            'mounts': sorted((m['Type'], m.get('Name', m.get('Source', '')), m['Destination'])
                             for m in container.get('Mounts', [])),
            'networks': {k: v['NetworkID'] for k, v in container['NetworkSettings']['Networks'].items()}}


def protected_state(site):
    if site.get('langfuse_project'):
        ids = docker('ps', '-aq', '--no-trunc', '--filter',
                     'label=com.docker.compose.project=' + site['langfuse_project']).decode().split()
        need(set(ids) == set(site['langfuse_containers']), 'Langfuse 项目组件集合变化；停止并重新核对')
    current = {}
    for ref in site['langfuse_containers']:
        c = inspect_one('container', ref)
        need(c['State']['Running'], '已有 Langfuse 组件未运行')
        current[ref] = fingerprint(c)
    n = inspect_one('network', site['langfuse_network'])
    need(n['Id'] == site['langfuse_network_id'], 'Langfuse 外部网络身份发生变化')
    need(json.loads(json.dumps(current)) == site['langfuse_fingerprints'], 'Langfuse 容器/镜像/挂载/网络变化；停止并重新核对')
    return current


def wait_health(project, service, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        c = service_container(project, service)
        if c['State']['Running'] and c['State'].get('Health', {}).get('Status') == 'healthy':
            return c
        if c['State'].get('ExitCode') == 132:
            raise Stop(service + ' 遇到 Illegal instruction；arm64 标签不能证明 ARMv8.0 兼容')
        time.sleep(2)
    raise Stop(service + ' 未在时限内 healthy；保留现场，不能标记验收通过')


def http_json(url, method='GET', body=None, headers=None, timeout=30, allowed=(200,)):
    encoded = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=encoded, method=method,
                                 headers={'Content-Type': 'application/json', **(headers or {})})
    # 不跟随带认证请求重定向；不使用宿主机公网代理。
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (OSError, TimeoutError) as exc:
        raise Stop('内网 HTTP 请求失败或超时；请求不会自动重放') from exc
    need(status in allowed, 'HTTP 状态不符: ' + str(status))
    try:
        return status, json.loads(raw)
    except ValueError as exc:
        raise Stop('HTTP 响应不是 JSON') from exc


def migration_map(path):
    return {p.name: hashlib.sha256(p.read_text(encoding='utf-8').encode()).hexdigest()
            for p in sorted(Path(path).glob('*.sql'))}


def validate_migrations(applied, candidate):
    for name, digest in applied.items():
        need(name in candidate and candidate[name] == digest, '已执行迁移被修改/缺失: ' + name)
    return sorted(set(candidate) - set(applied))


def record_error(root, message):
    p = Path(root) / 'transaction.json'
    if p.exists():
        tx = read_json(p)
        tx['error'] = message
        tx['failed_at'] = stamp()
        write_json(p, tx)


def finish_main(function):
    os.umask(0o077)
    try:
        function()
    except (Stop, ValueError, KeyError, OSError, tarfile.TarError, EOFError) as exc:
        # 异常的内容可能来自配置；只输出 Stop 中已受控的消息。
        message = str(exc) if isinstance(exc, Stop) else '输入/文件结构不符；已停止，请核对本机文件'
        print('STOP: ' + message, file=__import__('sys').stderr)
        raise SystemExit(1)
