"""站点级受限文件；永不把主密钥写入 release 或普通数据库备份。"""
import base64
import json
import os
from pathlib import Path
import secrets
import stat

from common import checksums, need, sha_file, stamp, verify_sums

FILES = {'ARGUS_MANAGED_SECRET_KEY_FILE': 'keyring.json',
         'ARGUS_CREDENTIAL_ADMIN_TOKEN_FILE': 'admin-token'}
TARGET = '/run/argus-secrets/'


def private_bytes(path):
    path = Path(path)
    need(not any(p.is_symlink() for p in [path, *path.parents]) and path.is_file(), '密钥文件缺失或为链接；请恢复原文件，禁止重新生成')
    need(stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, '密钥文件必须为 600 或更严格权限')
    return path.read_bytes()


def host_file(runner, target):
    matches = []
    for mount in runner.get('volumes', []):
        destination = Path(mount['target'])
        requested = Path(target)
        if requested == destination or destination in requested.parents:
            need(mount.get('type') == 'bind' and mount.get('read_only'), '密钥来源必须为只读 bind 挂载')
            matches.append((len(destination.parts), Path(mount['source']) / requested.relative_to(destination)))
    need(matches, '配置的密钥文件未挂载；请恢复原文件，不会生成替代密钥')
    return max(matches, key=lambda pair: pair[0])[1]


def validate_ring(data, rows):
    try:
        ring = json.loads(data)
        keys = ring['keys']
        need(isinstance(keys, dict) and ring['active_key_id'] in keys, '主密钥格式无效')
        need(all(isinstance(key, str) and len(base64.b64decode(value, validate=True)) == 32
                 for key, value in keys.items()), '主密钥格式无效')
        need(all(row['envelope']['key_id'] in keys for row in rows), '缺少历史密文所需密钥')
    except (ValueError, KeyError, TypeError, AssertionError):
        need(False, '主密钥格式无效或缺少历史密文所需密钥；请恢复原 Keyring')


def prepare(root, model, rows):
    root = Path(root).resolve()
    runner = model['services']['eval-runner']
    env = runner.setdefault('environment', {})
    directory = root / 'secrets'
    need(not directory.is_symlink(), '站点 secrets 目录不得为链接')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    need(directory.stat().st_mode & 0o077 == 0, '站点 secrets 目录必须为 700')
    sources = {}
    # 先读取并验证全部旧文件，禁止缺文件时部分创建一套不匹配的新密钥。
    for variable, name in FILES.items():
        destination = directory / name
        configured = env.get(variable)
        if configured:
            original = host_file(runner, configured)
            data = private_bytes(original)
            if destination.exists():
                need(private_bytes(destination) == data, '站点与原挂载密钥冲突；保留两份文件后人工核对')
        elif destination.exists():
            data = private_bytes(destination)
        else:
            need(not rows or name != 'keyring.json', '存在密文但原主密钥缺失；禁止生成新密钥')
            if name == 'keyring.json':
                data = (json.dumps({'active_key_id': 'site-1', 'keys': {
                    'site-1': base64.b64encode(secrets.token_bytes(32)).decode()}}) + '\n').encode()
            else:
                data = (secrets.token_urlsafe(48) + '\n').encode()
        if name == 'keyring.json':
            validate_ring(data, rows)
        else:
            need(data.strip() and not any(c.isspace() for c in data.strip().decode()), '管理 Token 格式无效')
        sources[variable] = data
    for variable, data in sources.items():
        name = FILES[variable]
        destination = directory / name
        if not destination.exists():
            with open(destination, 'xb') as handle:
                os.chmod(destination, 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        env[variable] = TARGET + name
    mounts = runner.setdefault('volumes', [])
    # 只替换本工具标准文件挂载；其他未知挂载仍由 snapshot_mounts 拒绝。
    mounts[:] = [v for v in mounts if v['target'] not in [TARGET + name for name in FILES.values()]]
    mounts.extend({'type': 'bind', 'source': str(directory / name), 'target': TARGET + name,
                   'read_only': True, 'bind': {'create_host_path': False}} for name in FILES.values())
    return {name: sha_file(directory / name) for name in FILES.values()}


def receipt(model):
    runner = model['services']['eval-runner']
    env = runner.get('environment', {})
    return {variable: {'path': str(host_file(runner, env[variable])),
                       'sha256': sha_file(host_file(runner, env[variable]))}
            for variable in FILES if env.get(variable)}


def verify_receipt(record):
    for item in record.values():
        private_bytes(item['path'])
        need(sha_file(item['path']) == item['sha256'], '恢复依赖的密钥已变化；先恢复独立保管的原密钥文件')


# 镜像内使用同一 Provider 真实解密全部历史修订；不导入 app.main，不触发迁移。
PROBE = '''import json,sys
from app.credentials import ManagedSecretProvider, protected_file
from app.secret_providers import _token
provider = ManagedSecretProvider()
_token(protected_file('ARGUS_CREDENTIAL_ADMIN_TOKEN_FILE').strip())
for row in json.load(sys.stdin):
    provider.decrypt(row['credential_id'], row['version'], row['envelope'])
'''


def recovery_copy(root, model):
    """独立于普通数据库备份的受限恢复集合；操作者另存受控介质。"""
    directory = Path(root) / 'secret-recovery' / stamp()
    directory.mkdir(parents=True, mode=0o700)
    os.chmod(directory.parent, 0o700)
    runner = model['services']['eval-runner']
    for variable, name in FILES.items():
        data = private_bytes(host_file(runner, runner['environment'][variable]))
        with open(directory / name, 'xb') as handle:
            os.chmod(directory / name, 0o600)
            handle.write(data)
    checksums(directory)
    os.chmod(directory / 'SHA256SUMS', 0o600)
    verify_sums(directory)
    return directory
