#!/usr/bin/env python3
"""站点配置、旧部署接管、离线升级、备份和恢复；仅操作 Argus 项目。"""
import argparse
import copy
import getpass
import io
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import tarfile
import time
from urllib.parse import urlsplit, urlunsplit
from common import (APPS, IMAGES, SERVICES, SHA_RE, TERMINAL_LAUNCH_STATUSES, Stop, archive_image_ids, checksums, compose, copy_tree, docker,
                    environment, files_under, fingerprint, finish_main, http_json, inspect_one,
                    literal_config, migration_map, need, operation, protected_state, read_json,
                    record_error, run, service_container, sha_file, stamp, validate_migrations,
                    verify_sums, wait_health, write_compose, write_json)
from receive import verify_source
import managed_secrets


def load_site(root):
    p = Path(root) / 'site.json'
    need(p.exists(), '站点尚未 init/adopt')
    need(p.stat().st_mode & 0o077 == 0, 'site.json 必须为 600 权限')
    return read_json(p)


def active(root):
    return Path(read_json(Path(root) / 'active.json')['deployment'])


def verify_toolchain():
    need(platform.system() == 'Linux' and platform.machine() in ('aarch64', 'arm64'), '部署仅允许 ARM64 Linux')
    target = docker('info', '--format', '{{.OSType}}/{{.Architecture}}').decode().strip()
    need(target in ('linux/arm64', 'linux/aarch64'), 'Docker 服务端必须为 ARM64 Linux；检查当前 Docker context')
    docker('compose', 'version')
    help_text = docker('compose', 'up', '--help').decode()
    need('--pull' in help_text and '--no-build' in help_text and '--no-deps' in help_text,
         'Docker Compose V2 缺少离线更新选项；先离线准备兼容版本')


def lf_identity(container, network):
    lf = inspect_one('container', container)
    need(lf['State']['Running'], 'Langfuse Web 未运行')
    project = lf['Config'].get('Labels', {}).get('com.docker.compose.project')
    need(project, 'Langfuse Web 无 Compose 项目身份；请人工核对后扩展站点适配')
    nets = lf['NetworkSettings']['Networks']
    if not network:
        need(len(nets) == 1, 'Langfuse Web 有多个网络；必须显式指定 --langfuse-network')
        network = next(iter(nets))
    need(network in nets, '所选网络未连接 Langfuse Web')
    ids = docker('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project).decode().split()
    fingerprints = {}
    for cid in ids:
        c = inspect_one('container', cid)
        need(c['State']['Running'], '已有 Langfuse 项目有未运行的组件')
        fingerprints[c['Id']] = fingerprint(c)
    selected = nets[network]
    hosts = {lf['Name'].lstrip('/'), selected.get('IPAddress', '')}
    hosts.update(selected.get('Aliases') or [])
    hosts.update(selected.get('DNSNames') or [])
    return {'langfuse_project': project, 'langfuse_network': network,
            'langfuse_web_hosts': sorted(h.lower() for h in hosts if h),
            'langfuse_network_id': inspect_one('network', network)['Id'],
            'langfuse_containers': list(fingerprints),
            'langfuse_fingerprints': json.loads(json.dumps(fingerprints))}


def validate_lf_url(url, identity):
    parsed = urlsplit(url)
    need(parsed.scheme in ('http', 'https') and parsed.hostname
         and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
         and parsed.path in ('', '/'), 'Langfuse 内部 URL 必须为无凭据/查询/路径的 HTTP(S) 地址')
    need(parsed.hostname.lower() in identity['langfuse_web_hosts'],
         'Langfuse URL 未指向所选网络中的 Web 容器；代理部署需单独适配')
    return url.rstrip('/')


def guard_lf_root(root, identity):
    for container in identity['langfuse_fingerprints'].values():
        for kind, source, _ in container['mounts']:
            if kind == 'bind':
                protected = Path(source).resolve()
                need(root != protected and protected not in root.parents,
                     'Argus 根目录位于已有 Langfuse 挂载目录中，拒绝写入')


def validate_topology(site, model):
    names = {key: value.get('name', site['project'] + '_' + key)
             for key, value in model.get('networks', {}).items()}
    need(set(names.values()) == {site['private_network'], site['langfuse_network']},
         'Compose 网络集合超出独立 Argus 私网和既有 Langfuse 网络')
    for service in SERVICES:
        config = model['services'][service]
        expected = {site['private_network']}
        if service == 'eval-runner':
            expected.add(site['langfuse_network'])
        need(not config.get('network_mode') and not config.get('external_links'), '不支持自定义网络模式/外部链接')
        need({names[key] for key in config.get('networks', {})} == expected, service + ' 配置网络不符合隔离要求')
        actual = service_container(site['project'], service)
        need(set(actual['NetworkSettings']['Networks']) == expected, service + ' 实际网络不符合隔离要求')


def validate_connections(site):
    # 只解析当前 Runner 环境和 DNS，不导入 app.main（导入会触发迁移）。
    helper = '''import json,os,socket
from urllib.parse import urlsplit
result={}
for name in ('DATABASE_URL','ARGUS_REDIS_URL'):
 u=urlsplit(os.environ[name]); addresses=sorted({x[4][0] for x in socket.getaddrinfo(u.hostname,u.port or (5432 if name=='DATABASE_URL' else 6379),socket.AF_INET,socket.SOCK_STREAM)})
 result[name]={'addresses':addresses,'database':u.path.lstrip('/'),'user':u.username}
print(json.dumps(result))'''
    runner = service_container(site['project'], 'eval-runner')
    resolved = json.loads(docker('exec', runner['Id'], 'python', '-c', helper))
    for service, key in (('postgres', 'DATABASE_URL'), ('redis', 'ARGUS_REDIS_URL')):
        container = service_container(site['project'], service)
        address = container['NetworkSettings']['Networks'][site['private_network']]['IPAddress']
        need(address and resolved[key]['addresses'] == [address],
             'Runner 的 ' + key + ' DNS 未唯一指向独立 Argus ' + service + '；先消除通用别名歧义')
    need(resolved['DATABASE_URL']['database'] == site['pg_database']
         and resolved['DATABASE_URL']['user'] == site['pg_user'], 'Runner 数据库/用户与备份目标不一致')


def pin_local(ref, role):
    info = inspect_one('image', ref)
    need(info['Architecture'] == 'arm64' and info['Os'] == 'linux', role + ' 镜像不是 ARM64 Linux')
    locked = 'argus-offline/' + role + ':locked-' + info['Id'].split(':')[1][:16]
    docker('tag', info['Id'], locked)
    return {'ref': locked, 'id': info['Id']}


def validate_project(project, lf_project):
    need(re.fullmatch('[a-z0-9][a-z0-9_-]{1,60}', project) and project != lf_project,
         'Argus 项目名无效或与 Langfuse 相同')


def validate_private_mounts(site):
    protected_names = set()
    for value in site['langfuse_fingerprints'].values():
        protected_names.update(m[1] for m in value['mounts'] if m[0] == 'volume')
    need(site['pg_volume'] != site['redis_volume'] and site['pg_volume'] not in protected_names
         and site['redis_volume'] not in protected_names, 'Argus 与 Langfuse 共享了数据卷')
    for name in (site['pg_volume'], site['redis_volume']):
        inspect_one('volume', name)
        ids = docker('ps', '-aq', '--filter', 'volume=' + name).decode().split()
        for cid in ids:
            c = inspect_one('container', cid)
            need(c['Config'].get('Labels', {}).get('com.docker.compose.project') == site['project'],
                 '数据卷被 Argus 项目以外的容器使用')


def initial_model(site, manifest, source):
    images = manifest['images']
    health_python = {'test': ['CMD', 'python', '-c',
        "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3)"],
        'interval': '5s', 'timeout': '5s', 'retries': 36, 'start_period': '20s'}
    private, external = 'argus_internal', 'langfuse_external'
    defaults = {'pull_policy': 'never', 'restart': 'unless-stopped', 'networks': {private: {}}}
    pg = {**copy.deepcopy(defaults), 'image': site['postgres']['ref'],
          'environment': {'POSTGRES_USER': 'argus', 'POSTGRES_DB': 'argus', 'POSTGRES_PASSWORD': site['pg_password']},
          'volumes': [{'type': 'volume', 'source': 'argus_pg_data', 'target': '/var/lib/postgresql/data'}],
          'healthcheck': {'test': ['CMD', 'pg_isready', '-U', 'argus', '-d', 'argus'], 'interval': '5s', 'timeout': '5s', 'retries': 36},
          'networks': {private: {'aliases': ['argus-postgres']}}}
    rd = {**copy.deepcopy(defaults), 'image': site['redis']['ref'],
          'environment': {'REDISCLI_AUTH': site['redis_password']},
          'command': ['redis-server', '--requirepass', site['redis_password'], '--appendonly', 'yes',
                      '--appendfsync', 'everysec', '--save', '900 1 300 10 60 10000', '--maxmemory-policy', 'noeviction'],
          'volumes': [{'type': 'volume', 'source': 'argus_redis_data', 'target': '/data'}],
          'healthcheck': {'test': ['CMD', 'redis-cli', 'ping'], 'interval': '5s', 'timeout': '5s', 'retries': 36},
          'networks': {private: {'aliases': ['argus-redis']}}}
    services = {'postgres': pg, 'redis': rd}
    for version, port in (('v1', '18081'), ('v2', '18082')):
        services['demo-agent-' + version] = {**copy.deepcopy(defaults), 'image': images['demo-agent']['ref'],
            'environment': {'AGENT_VERSION': version}, 'ports': ['127.0.0.1:' + port + ':8080'],
            'healthcheck': copy.deepcopy(health_python)}
    runner_env = {'DATABASE_URL': 'postgresql+psycopg://argus:' + site['pg_password'] + '@argus-postgres:5432/argus',
        'ARGUS_REDIS_URL': 'redis://:' + site['redis_password'] + '@argus-redis:6379/0', 'ARGUS_DB_MODE': 'prod',
        'ARGUS_AUTO_IMPORT_YAML': 'true', 'ARGUS_WORKER_ENABLED': 'true', 'ARGUS_RECONCILER_ENABLED': 'true',
        'ARGUS_WORKER_CONCURRENCY': '2', 'ARGUS_ENVIRONMENT': site['environment'], 'ARGUS_SECRET_MODE': 'production',
        'LANGFUSE_BASE_URL': site['langfuse_base_url'], 'LANGFUSE_HOST': site['langfuse_base_url'],
        'LANGFUSE_PUBLIC_KEY': site['lf_public_key'], 'LANGFUSE_SECRET_KEY': site['lf_secret_key'],
        'LANGFUSE_TRACING_ENVIRONMENT': site['environment'], 'OTEL_SDK_DISABLED': 'false',
        'ARGUS_LANGFUSE_DASHBOARD_URL': site['langfuse_dashboard_url'], 'ARGUS_CONSOLE_BASE_URL': site['console_url'],
        'AGENT_REGISTRY_PATH': '/app/config/agents.yaml', 'DATASET_SEED_PATH': '/app/data/dataset.json',
        'ARGUS_MIGRATIONS_PATH': '/app/migrations', 'ARGUS_DATASET_SOURCE': 'langfuse',
        'ARGUS_LANGFUSE_RUN_ITEM_STRATEGY': 'otel'}
    services['eval-runner'] = {**copy.deepcopy(defaults), 'image': images['eval-runner']['ref'],
        'environment': runner_env, 'ports': ['127.0.0.1:18080:8080'], 'stop_grace_period': '60s',
        'volumes': [{'type': 'bind', 'source': str(source / name), 'target': '/app/' + name,
                     'read_only': True} for name in ('config', 'data', 'migrations')],
        'healthcheck': copy.deepcopy(health_python), 'networks': {private: {}, external: {}}}
    services['argus-console'] = {**copy.deepcopy(defaults), 'image': images['console']['ref'],
        'environment': {'RUNNER_HOST': 'eval-runner', 'RUNNER_PORT': '8080'},
        'ports': [site['console_bind'] + ':18083:8080'],
        'healthcheck': {'test': ['CMD-SHELL', 'wget -qO- http://127.0.0.1:8080/health >/dev/null || exit 1'],
                        'interval': '5s', 'timeout': '5s', 'retries': 36}}
    return {'services': services, 'networks': {
        private: {'external': True, 'name': site['private_network']},
        external: {'external': True, 'name': site['langfuse_network']}},
        'volumes': {'argus_pg_data': {'external': True, 'name': site['pg_volume']},
                    'argus_redis_data': {'external': True, 'name': site['redis_volume']}}}


def init_site(a):
    root = Path(a.root).resolve()
    need(not (root / 'site.json').exists(), '站点已存在，禁止覆盖')
    need(not (root / 'compose.yaml').exists() and not (root / '.env').exists(), '检测到旧部署；请使用 adopt')
    lf = lf_identity(a.langfuse_container, a.langfuse_network)
    guard_lf_root(root, lf)
    validate_lf_url(a.langfuse_base_url, lf)
    validate_project(a.project, lf['langfuse_project'])
    need(not docker('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + a.project).strip(), '项目已有容器；请 adopt')
    pg, rd = pin_local(a.postgres_image, 'postgres'), pin_local(a.redis_image, 'redis')
    # 版本升级不在此 Skill 范围内；首次安装也限定已验证的大版本。
    pgv = docker('run', '--rm', '--pull', 'never', '--network', 'none', '--entrypoint', 'postgres', pg['ref'], '--version').decode()
    rdv = docker('run', '--rm', '--pull', 'never', '--network', 'none', '--entrypoint', 'redis-server', rd['ref'], '--version').decode()
    need(re.search(r'PostgreSQL\) 17\.', pgv) and re.search(r'v=7\.', rdv), '首次安装要求 PostgreSQL17 / Redis7')
    pk = input('Langfuse 测试项目 Public Key: ').strip()
    sk = getpass.getpass('Langfuse 测试项目 Secret Key: ').strip()
    need(all(re.fullmatch(r'[A-Za-z0-9_-]{8,256}', v) for v in (pk, sk)), 'API Key 格式异常')
    site = {**lf, 'site_id': secrets.token_hex(16), 'project': a.project, 'environment': a.environment,
            'pg_volume': a.project + '_argus_pg_data', 'redis_volume': a.project + '_argus_redis_data',
            'private_network': a.project + '_argus_internal', 'postgres': pg, 'redis': rd,
            'pg_user': 'argus', 'pg_database': 'argus', 'pg_password': secrets.token_hex(24),
            'redis_password': secrets.token_hex(24), 'lf_public_key': pk, 'lf_secret_key': sk,
            'langfuse_base_url': a.langfuse_base_url.rstrip('/'),
            'langfuse_dashboard_url': a.langfuse_dashboard_url.rstrip('/'),
            'console_bind': a.console_bind, 'console_url': a.console_url.rstrip('/'),
            'runner_url': 'http://127.0.0.1:18080', 'created_at': stamp()}
    need(a.console_bind == urlsplit(a.console_url).hostname, 'Console bind 与浏览器 URL 主机必须一致')
    # 禁止已有卷的意外接管；存在的站点必须走 inspect/adopt。
    volumes = docker('volume', 'ls', '-q').decode().split()
    networks = docker('network', 'ls', '--format', '{{.Name}}').decode().split()
    need(site['pg_volume'] not in volumes and site['redis_volume'] not in volumes and site['private_network'] not in networks,
         '站点卷或专用网络已存在；禁止把未知数据当新安装')
    protected_state(site)
    for name in (site['pg_volume'], site['redis_volume']):
        docker('volume', 'create', '--label', 'io.argus-offline.site=' + site['site_id'], name)
    docker('network', 'create', '--internal', '--label', 'io.argus-offline.site=' + site['site_id'], site['private_network'])
    seed_model = {'services': {'eval-runner': {'environment': {}, 'volumes': []}}}
    managed_secrets.prepare(root, seed_model, [])
    write_json(root / 'site.json', site)
    print('站点初始化完成；Langfuse 保持原容器/网络/卷。下一步 install。')


def snapshot_mounts(model, deployment):
    runner = model['services']['eval-runner']
    mounts = runner.get('volumes', [])
    standard = {'/app/config', '/app/data', '/app/migrations'}
    secret_targets = {managed_secrets.TARGET + name for name in managed_secrets.FILES.values()}
    need({x['target'] for x in mounts} == standard | (secret_targets if len(mounts) == 5 else set())
         and len(mounts) in (3, 5), '未知 Runner 挂载需先适配，禁止静默丢失')
    for v in mounts:
        need(v.get('type') == 'bind' and v.get('read_only'), 'Runner 文件必须只读挂载')
        if v['target'] in secret_targets:
            managed_secrets.private_bytes(v['source'])
            continue
        name = v['target'].split('/')[-1]
        destination = Path(deployment) / 'source' / name
        # 现场旧脚本将密钥置于 config/.secrets；原文件保留，普通备份不复制。
        source = Path(v['source'])
        need(not source.is_symlink(), '源码挂载不可为链接')
        files_under(source)  # 复制前拒绝链接/特殊文件，不跟随链接读取站点外内容。
        shutil.copytree(source, destination, ignore=shutil.ignore_patterns('.secrets'))
        need(not any(p.is_symlink() for p in destination.rglob('*')), '源码副本不得包含链接')
        v['source'] = str(destination)


def adopt(a):
    root = Path(a.root).resolve()
    need(not (root / 'site.json').exists() and not (root / 'active.json').exists(), '站点已接管')
    lf = lf_identity(a.langfuse_container, a.langfuse_network)
    guard_lf_root(root, lf)
    validate_lf_url(a.langfuse_base_url, lf)
    validate_project(a.project, lf['langfuse_project'])
    model = json.loads(docker('compose', '--env-file', str(Path(a.env_file).resolve()),
                   '--project-directory', str(Path(a.compose_file).resolve().parent), '-p', a.project,
                   '-f', str(Path(a.compose_file).resolve()), 'config', '--format', 'json'))
    need(set(model['services']) == set(SERVICES), '旧 Compose 必须仅包含六个 Argus 容器')
    containers = {s: service_container(a.project, s) for s in SERVICES}
    for c in containers.values():
        need(c['State']['Running'], '旧 Argus 有未运行容器；先恢复或单独诊断')
    need(not model.get('secrets') and not model.get('configs'), '额外 Compose Secret/Config 需先适配并纳入备份')
    for service, container in containers.items():
        configured = model['services'][service]
        actual_env = environment(container['Config'])
        need(all(value is not None and actual_env.get(key) == str(value)
                 for key, value in configured.get('environment', {}).items()),
             service + ' 的配置环境与实际容器不符；先核对旧 .env/Compose')
        if service not in ('postgres', 'redis', 'eval-runner'):
            need(not configured.get('volumes') and not container.get('Mounts'), service + ' 额外挂载需先适配备份')
        if service in ('postgres', 'redis'):
            need(len(configured.get('volumes', [])) == 1 and len(container.get('Mounts', [])) == 1,
                 service + ' 额外挂载需先适配备份')
    pg, rd = containers['postgres'], containers['redis']
    def volume(c, target):
        matches = [m for m in c['Mounts'] if m['Destination'] == target]
        need(len(matches) == 1 and matches[0]['Type'] == 'volume', '仅自动接管独立命名数据卷')
        return matches[0]['Name']
    need(not pg['HostConfig'].get('PortBindings') and not rd['HostConfig'].get('PortBindings'), 'Argus PG/Redis 不应发布宿主端口')
    private_nets = set(pg['NetworkSettings']['Networks']) & set(rd['NetworkSettings']['Networks'])
    need(len(private_nets) == 1 and lf['langfuse_network'] not in private_nets, 'PG/Redis 私网不明确或混入 Langfuse')
    runner = containers['eval-runner']
    need(lf['langfuse_network'] in runner['NetworkSettings']['Networks'], '旧 Runner 未连接指定 Langfuse 网络')
    baked = environment(inspect_one('image', runner['Image'])['Config'])
    runtime = environment(runner['Config'])
    sha = baked.get('ARGUS_BUILD_ID', '')
    need(SHA_RE.fullmatch(sha) and runtime.get('ARGUS_BUILD_ID') == sha, '旧 Runner 内嵌/运行时 SHA 不一致，不能可靠回滚')
    renv = model['services']['eval-runner'].setdefault('environment', {})
    need(renv.get('LANGFUSE_BASE_URL', renv.get('LANGFUSE_HOST')) == a.langfuse_base_url.rstrip('/'), '旧 Runner Langfuse URL 与指定实例不同')
    # 使用实际容器名消除双网络的 postgres/redis 通用 DNS 名称歧义；只更新下一次 Runner 配置。
    for key, c in (('DATABASE_URL', pg), ('ARGUS_REDIS_URL', rd)):
        u = urlsplit(renv[key])
        need(u.username is not None or key == 'ARGUS_REDIS_URL', '旧数据库连接字符串无法解析')
        host = c['Name'].lstrip('/')
        auth = u.netloc.rsplit('@', 1)[0] + '@' if '@' in u.netloc else ''
        renv[key] = urlunsplit((u.scheme, auth + host + (':' + str(u.port) if u.port else ''), u.path, u.query, u.fragment))
    site = {**lf, 'site_id': secrets.token_hex(16), 'project': a.project,
        'environment': renv.get('ARGUS_ENVIRONMENT', 'argus-kunpeng-test'),
        'pg_volume': volume(pg, '/var/lib/postgresql/data'), 'redis_volume': volume(rd, '/data'),
        'private_network': next(iter(private_nets)),
        'postgres': pin_local(pg['Image'], 'postgres'), 'redis': pin_local(rd['Image'], 'redis'),
        'pg_user': environment(pg['Config']).get('POSTGRES_USER', 'argus'),
        'pg_database': environment(pg['Config']).get('POSTGRES_DB', 'argus'),
        'runner_url': a.runner_url.rstrip('/'), 'console_url': a.console_url.rstrip('/'),
        'langfuse_base_url': a.langfuse_base_url.rstrip('/'), 'created_at': stamp(), 'adopted': True}
    validate_private_mounts(site)
    validate_topology(site, model)
    protected_state(site)
    deployment = root / 'deployments' / ('adopt-' + stamp())
    deployment.mkdir(parents=True)
    managed_secrets.prepare(root, model, credential_rows(site))
    probe_secrets(model, credential_rows(site))
    snapshot_mounts(model, deployment)
    for s in SERVICES:
        for forbidden in ('build', 'depends_on', 'container_name'):
            model['services'][s].pop(forbidden, None)
        model['services'][s]['pull_policy'] = 'never'
        model['services'][s]['image'] = pin_local(containers[s]['Image'], 'rollback-' + s)['ref']
    for key, n in model.get('networks', {}).items():
        name = n.get('name', a.project + '_' + key)
        model['networks'][key] = {'name': name, 'external': True}
    for key, v in model.get('volumes', {}).items():
        name = v.get('name', a.project + '_' + key)
        model['volumes'][key] = {'name': name, 'external': True}
    write_compose(deployment, model)
    write_json(deployment / 'deployment.json', {'git_sha': sha, 'deployment': str(deployment),
               'source': str(deployment / 'source'), 'adopted': True})
    write_json(root / 'site.json', site)
    # 验证数据库权限、Schema、Redis 持久化与实际 build_id；不会重启旧容器。
    try:
        preflight(root, deployment)
    except BaseException:
        (root / 'site.json').unlink()  # 仅撤销本次新建的接管描述，允许修正参数后重试。
        raise
    write_json(root / 'active.json', {'deployment': str(deployment), 'git_sha': sha, 'acceptance': 'NOT_RUN'})
    print('旧部署已接管；源目录/配置已留副本，运行中容器未重建。下一步 backup 或 upgrade。')


def sql(site, query, db=None, data=None):
    cid = service_container(site['project'], 'postgres')['Id']
    args = ['exec', '-i', cid, 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', site['pg_user'],
            '-d', db or site['pg_database'], '-At']
    if query is not None:
        args += ['-c', query]
    return docker(*args, data=data).decode().strip()


def credential_rows(site):
    if not sql(site, "SELECT to_regclass('public.credential_secrets');"):
        return []
    return json.loads(sql(site, "SELECT COALESCE(json_agg(s), '[]'::json) FROM "
                         "(SELECT credential_id, version, envelope FROM credential_secrets) s;"))


def probe_secrets(model, rows):
    runner = model['services']['eval-runner']
    args = ['run', '--rm', '-i', '--pull', 'never', '--network', 'none', '--entrypoint', 'python']
    for variable in managed_secrets.FILES:
        target = runner['environment'][variable]
        source = managed_secrets.host_file(runner, target)
        managed_secrets.private_bytes(source)
        args.extend(['--mount', 'type=bind,src=' + str(source) + ',dst=' + target + ',readonly', '-e', variable + '=' + target])
    docker(*args, runner['image'], '-c', managed_secrets.PROBE, data=json.dumps(rows).encode())


def redis_cli(site, *args):
    c = service_container(site['project'], 'redis')
    # 已有配置可能只有 ARGUS_REDIS_PASSWORD，无 REDISCLI_AUTH；从受限模型读取密码。
    pw = environment(c['Config']).get('REDISCLI_AUTH')
    if not pw:
        pw = environment(c['Config']).get('ARGUS_REDIS_PASSWORD')
    need(pw, '旧 Redis 容器缺少受限认证环境；请先适配，禁止在命令行回显密码')
    # stdin 中传递认证，不把密码放宿主机进程参数或打印到日志。
    return docker('exec', '-i', c['Id'], 'sh', '-c',
                  'IFS= read -r REDISCLI_AUTH; export REDISCLI_AUTH; exec redis-cli --raw "$@"',
                  'argus-redis-cli', *args, data=(pw + '\n').encode()).decode().strip()


def preflight(root, deployment, check_http=True):
    site = load_site(root)
    need(re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', site['pg_user']), '数据库用户名超出脚本支持范围')
    protected_state(site)
    validate_private_mounts(site)
    model = read_json(Path(deployment) / 'model.json')
    need(set(model['services']) == set(SERVICES), '部署出现范围外服务')
    validate_topology(site, model)
    compose(root, deployment, 'config', '-q')
    for s in ('postgres', 'redis'):
        c = service_container(site['project'], s)
        need(c['State']['Running'], s + ' 未运行')
        need(c['Image'] == site[s]['id'], s + ' 镜像 ID 发生变化；本 Skill 不升级数据库/缓存镜像')
        need(not c['HostConfig'].get('PortBindings'), s + ' 发布了宿主端口')
        expected_target = '/data' if s == 'redis' else '/var/lib/postgresql/data'
        mounts = [m for m in c['Mounts'] if m['Destination'] == expected_target]
        need(len(mounts) == 1 and mounts[0].get('Name') == site['redis_volume' if s == 'redis' else 'pg_volume'], s + ' 数据卷不符')
        need(site['langfuse_network'] not in c['NetworkSettings']['Networks'], s + ' 误连 Langfuse 网络')
    need(sql(site, 'SHOW server_version_num;').startswith('17'), '只支持已验证的 PostgreSQL17 站点')
    need(redis_cli(site, 'PING') == 'PONG', 'Redis 认证/连通性失败')
    need('redis_version:7.' in redis_cli(site, 'INFO', 'server'), '只支持已验证的 Redis7 站点')
    aof = redis_cli(site, 'CONFIG', 'GET', 'appendonly').splitlines()[-1]
    saves = redis_cli(site, 'CONFIG', 'GET', 'save').splitlines()
    need(aof == 'yes' or len(saves) > 1 and saves[-1].strip(), 'Redis 未启用 AOF 或 RDB 持久化')
    if check_http:
        validate_connections(site)
        verify_app_images(site, deployment)
        expected = read_json(Path(deployment) / 'deployment.json')['git_sha']
        source = Path(read_json(Path(deployment) / 'deployment.json')['source'])
        need(applied_migrations(site) == migration_map(source / 'migrations'), '实际 Schema 迁移与活动源码不一致')
        need(http_json(site['runner_url'] + '/health')[1].get('status') == 'ok', 'Runner health 不符')
        info = http_json(site['runner_url'] + '/api/v1/system/info')[1]
        need(info.get('build_id') == expected, '运行时 build_id 与活动部署不符')
        proxy_info = http_json(site['console_url'] + '/api/v1/system/info')[1]
        need(proxy_info.get('build_id') == expected, 'Console 代理未连接本次 Runner')
    return site


def recover_backup(a):
    root = Path(a.root).resolve()
    need(a.maintenance_window, '恢复原服务前需要确认仍处于停写窗口')
    tx = read_json(root / 'transaction.json')
    need(tx.get('operation') in ('backup', 'upgrade') and tx.get('phase') in
         ('PREPARED', 'BACKUP_IN_PROGRESS', 'BACKUP_COMPLETE'), '此阶段可能已迁移；必须走数据恢复回滚')
    previous = Path(tx['from_deployment'])
    site = load_site(root)
    protected_state(site)
    validate_private_mounts(site)
    expected = migration_map(Path(read_json(previous / 'deployment.json')['source']) / 'migrations')
    need(applied_migrations(site) == expected, '数据库不是原 Schema，禁止启动旧 Runner')
    rc = service_container(site['project'], 'redis')
    need(rc['Image'] == site['redis']['id'], 'Redis 镜像不符')
    if not rc['State']['Running']:
        docker('start', rc['Id'])
    wait_health(site['project'], 'redis')
    compose(root, previous, 'start', *APPS)
    preflight(root, previous)
    write_json(root / 'evidence' / ('recover-' + stamp()) / 'transaction.json', tx)
    (root / 'transaction.json').unlink()
    print('已确认 Schema 未改变并恢复原服务；失败/不完整备份不能用于回滚。')


def applied_migrations(site):
    value = sql(site, 'SELECT version, checksum FROM schema_migrations ORDER BY version;')
    return dict(row.split('|') for row in value.splitlines() if row)


def tx_update(root, **fields):
    p = Path(root) / 'transaction.json'
    value = read_json(p) if p.exists() else {'started_at': stamp()}
    value.update(fields)
    write_json(p, value)


def load_images(root, sha, smoke=True):
    need(SHA_RE.fullmatch(sha), '需要完整 Git SHA')
    release = Path(root) / 'releases' / sha
    manifest = verify_source(release)
    for name, row in manifest['images'].items():
        # 不允许同名 tag 已指向另一制品（相同源码重建也可能产生不同依赖/层）。
        allowed_ids = archive_image_ids(release / 'payload' / row['file'], row['ref'])
        ids = docker('image', 'ls', '--no-trunc', '--format', '{{.ID}}', row['ref']).decode().split()
        need(not ids or set(ids) <= allowed_ids, '本地同名 tag 指向其他 Image ID: ' + name)
        if not ids:
            docker('load', '-i', str(release / 'payload' / row['file']), timeout=1800)
        info = inspect_one('image', row['ref'])
        need(info['Id'] in allowed_ids and info['Architecture'] == 'arm64' and info.get('Os', 'linux') == 'linux', '加载后 Image ID/架构不符')
        if smoke:
            if name == 'eval-runner':
                cmd = ['--entrypoint', 'python', row['ref'], '-c',
                    'import sys,ssl,ctypes,uvicorn,psycopg,cryptography,redis; '
                    'from cryptography.hazmat.bindings._rust import openssl; print(sys.version)']
            elif name == 'demo-agent':
                cmd = ['--entrypoint', 'python', row['ref'], '-c', 'import sys,ssl,uvicorn,pydantic;print(sys.version)']
            elif name == 'console':
                # 保留 entrypoint 执行 envsubst；离线无网络时用回环地址，
                # 在目标内核上验证实际配置（包括共享内存页大小约束）。
                cmd = ['-e', 'RUNNER_HOST=127.0.0.1', '-e', 'ARGUS_DNS_RESOLVERS=127.0.0.1',
                       row['ref'], 'nginx', '-t']
            else:
                cmd = ['--entrypoint', 'postgres' if name == 'postgres' else 'redis-server', row['ref'], '--version']
            docker('run', '--rm', '--pull', 'never', '--network', 'none', *cmd, timeout=60)
    receipt = {'git_sha': sha, 'images': manifest['images'], 'target_native_imports': 'PASS',
               'machine': platform.machine(), 'at': stamp(), 'business_acceptance': 'NOT_RUN'}
    evidence = Path(root) / 'evidence' / ('load-' + stamp())
    write_json(evidence / 'image-receipt.json', receipt)
    print('离线镜像导入和当前 CPU 原生库启动检查通过；仍须完成业务验收。')
    return manifest


def candidate_deployment(root, sha, base=None):
    root = Path(root).resolve()
    site = load_site(root)
    manifest = verify_source(root / 'releases' / sha)
    source = root / 'releases' / sha / 'source'
    path = root / 'deployments' / (stamp() + '-' + sha[:12])
    path.mkdir(parents=True)
    if base:
        model = read_json(Path(base) / 'model.json')
        # 旧模型仅改变四个应用服务；基础组件、卷与网络保持同一份实际配置。
        managed_secrets.prepare(root, model, credential_rows(site))
        runner = model['services']['eval-runner']
        for v in runner['volumes']:
            if v['target'] in ('/app/config', '/app/data', '/app/migrations'):
                v['source'] = str(source / v['target'].split('/')[-1])
        env = runner.setdefault('environment', {})
        env.pop('ARGUS_BUILD_ID', None)  # 使用镜像内嵌 SHA，不靠运行时环境伪造身份。
        env.pop('RUNNER_VERSION', None)
        env['ARGUS_AUTO_IMPORT_YAML'] = 'false'  # Registry 是 DB SSOT，升级不重新导入演示 YAML。
        for s in APPS:
            model['services'][s]['image'] = manifest['images'][IMAGES[s]]['ref']
            model['services'][s]['pull_policy'] = 'never'
            model['services'][s].pop('build', None)
    else:
        model = initial_model(site, manifest, source)
        managed_secrets.prepare(root, model, [])
    probe_secrets(model, credential_rows(site) if base else [])
    write_compose(path, model)
    write_json(path / 'deployment.json', {'git_sha': sha, 'deployment': str(path), 'source': str(source),
                                        'images': manifest['images'], 'created_at': stamp()})
    compose(root, path, 'config', '-q')
    return path


def quiesce(root, deployment, site):
    compose(root, deployment, 'stop', '-t', '60', 'argus-console')
    terminal = ','.join("'" + value + "'" for value in TERMINAL_LAUNCH_STATUSES)
    query = 'SELECT count(*) FROM experiment_launches WHERE upper(status) NOT IN (' + terminal + ');'
    if int(sql(site, query)):
        compose(root, deployment, 'start', 'argus-console')
        raise Stop('存在未结束的 Launch；请按业务契约排空/取消后重试，未自动取消或重放 Agent')
    compose(root, deployment, 'stop', '-t', '60', 'eval-runner', 'demo-agent-v1', 'demo-agent-v2')
    need(int(sql(site, query)) == 0, '停止期间发现新任务；保持停写，先处理并发生产者')


def table_counts(site, database=None):
    tables = sql(site, "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;", db=database).splitlines()
    values = {}
    for name in tables:
        need(re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', name), '数据表名超出本脚本支持范围')
        values[name] = int(sql(site, 'SELECT count(*) FROM public."' + name + '";', db=database))
    return values


def restore_to_scratch(site, dump, expected_counts):
    name = 'argus_verify_' + secrets.token_hex(8)
    sql(site, 'CREATE DATABASE ' + name + ' OWNER "' + site['pg_user'] + '";', db='postgres')
    cid = service_container(site['project'], 'postgres')['Id']
    try:
        with open(dump, 'rb') as f:
            docker('exec', '-i', cid, 'pg_restore', '--exit-on-error',
                   '-U', site['pg_user'], '-d', name, stdin=f, timeout=3600)
        need(table_counts(site, name) == expected_counts, '备份恢复演练表计数不一致')
        return name
    except BaseException:
        sql(site, 'DROP DATABASE ' + name + ';', db='postgres')
        raise


def backup_data(root, deployment, site, label='backup'):
    root = Path(root)
    backup = root / 'backups' / (label + '-' + stamp())
    backup.mkdir(parents=True)
    tx_update(root, phase='BACKUP_IN_PROGRESS', backup=str(backup))
    pgcid = service_container(site['project'], 'postgres')['Id']
    db_bytes = int(sql(site, 'SELECT pg_database_size(current_database());'))
    refs = sorted({s['image'] for s in read_json(Path(deployment) / 'model.json')['services'].values()})
    images = {ref: inspect_one('image', ref)['Id'] for ref in refs}
    required = db_bytes * 2 + sum(inspect_one('image', ref).get('Size', 0) for ref in refs)
    need(shutil.disk_usage(root).free > required, '备份目录空间不足（需要 dump、源码和上一版镜像归档）')
    # PostgreSQL 数据目录可能不在 root 同一文件系统，单独核对恢复演练空间。
    df = docker('exec', pgcid, 'df', '-Pk', '/var/lib/postgresql/data').decode().splitlines()[-1].split()
    need(int(df[3]) * 1024 > db_bytes * 2, 'PostgreSQL 卷空间不足以做恢复演练')
    counts = table_counts(site)
    applied = applied_migrations(site)
    with open(backup / 'postgres.dump', 'xb') as f:
        docker('exec', pgcid, 'pg_dump', '-U', site['pg_user'], '-d', site['pg_database'],
               '--format=custom', stdout=f, timeout=3600)
    with open(backup / 'postgres.dump', 'rb') as f:
        need(docker('exec', '-i', pgcid, 'pg_restore', '--list', stdin=f).strip(), 'pg_restore --list 为空')
    scratch = restore_to_scratch(site, backup / 'postgres.dump', counts)
    sql(site, 'DROP DATABASE ' + scratch + ';', db='postgres')
    need(redis_cli(site, 'SAVE') == 'OK', 'Redis SAVE 失败，拒绝只有空卷备份')
    rcid = service_container(site['project'], 'redis')['Id']
    compose(root, deployment, 'stop', '-t', '60', 'redis')
    need(not inspect_one('container', rcid)['State']['Running'], 'Redis 未停止，拒绝不一致文件副本')
    (backup / 'redis-data').mkdir()
    docker('cp', rcid + ':/data/.', str(backup / 'redis-data'), timeout=1800)
    need(any(files_under(backup / 'redis-data')), 'Redis 数据备份为空')
    # 保留上一版制品，不依赖 registry、重建镜像或可被覆盖的 tag。
    docker('save', '-o', str(backup / 'rollback-images.tar'), *refs, timeout=3600)
    old_model = read_json(Path(deployment) / 'model.json')
    rows = credential_rows(site)
    managed_secrets.prepare(root, old_model, rows)
    probe_secrets(old_model, rows)
    recovery = managed_secrets.recovery_copy(root, old_model)
    write_json(backup / 'secret-dependencies.json', managed_secrets.receipt(old_model))
    print('密钥独立恢复副本（请另存受控介质，与数据库备份分离）: ' + str(recovery))
    snapshot_mounts(old_model, backup)
    write_json(backup / 'model.json', old_model)
    write_json(backup / 'site.json', site)
    old_deployment = read_json(Path(deployment) / 'deployment.json')
    write_json(backup / 'backup.json', {'format': 1, 'site_id': site['site_id'], 'project': site['project'],
        'git_sha': old_deployment['git_sha'], 'old_deployment': str(deployment), 'images': images,
        'pg_database': site['pg_database'], 'pg_volume': site['pg_volume'], 'redis_volume': site['redis_volume'],
        'table_counts': counts, 'migrations': applied, 'pg_restore_rehearsal': 'PASS', 'created_at': stamp(),
        'post_backup_writes': '必须排除；恢复将回到此备份时刻', 'langfuse_rollback': 'NOT_SUPPORTED_OR_ATTEMPTED'})
    checksums(backup)
    verify_sums(backup)
    # 完整校验清单与恢复演练均完成后才发布备份完成标记（标记不加入文件集合）。
    write_json(root / 'last-backup.json', {'path': str(backup), 'sha256sums': sha_file(backup / 'SHA256SUMS')})
    docker('start', rcid)
    wait_health(site['project'], 'redis')
    tx_update(root, phase='BACKUP_COMPLETE', backup=str(backup), backup_manifest_sha256=sha_file(backup / 'SHA256SUMS'))
    print('备份及实际恢复演练完成: ' + str(backup))
    print('请独立记录备份 SHA256SUMS 自身 SHA256: ' + sha_file(backup / 'SHA256SUMS'))
    return backup


def verify_app_images(site, deployment):
    model = read_json(Path(deployment) / 'model.json')
    for s in APPS:
        c = service_container(site['project'], s)
        need(c['Image'] == inspect_one('image', model['services'][s]['image'])['Id'], '应用容器镜像不符: ' + s)
        wait_health(site['project'], s)
    r = service_container(site['project'], 'eval-runner')
    expected = read_json(Path(deployment) / 'deployment.json')['git_sha']
    need(environment(inspect_one('image', r['Image'])['Config']).get('ARGUS_BUILD_ID') == expected,
         '运行中 Runner 镜像内嵌 SHA 不符')
    need(environment(r['Config']).get('ARGUS_BUILD_ID') == expected, '运行时构建身份被覆盖')


def activate_apps(root, deployment, site):
    compose(root, deployment, 'up', '-d', '--pull', 'never', '--no-build', '--no-deps', 'demo-agent-v1', 'demo-agent-v2')
    wait_health(site['project'], 'demo-agent-v1')
    wait_health(site['project'], 'demo-agent-v2')
    model = read_json(Path(deployment) / 'model.json')
    if model['services']['eval-runner'].get('environment', {}).get('ARGUS_MANAGED_SECRET_KEY_FILE'):
        probe_secrets(model, credential_rows(site))
    tx_update(root, phase='RUNNER_STARTING_MIGRATIONS_MAY_HAVE_RUN')
    compose(root, deployment, 'up', '-d', '--pull', 'never', '--no-build', '--no-deps', 'eval-runner')
    wait_health(site['project'], 'eval-runner')
    compose(root, deployment, 'up', '-d', '--pull', 'never', '--no-build', '--no-deps', 'argus-console')
    verify_app_images(site, deployment)
    preflight(root, deployment)


def finish_transaction(root, kind, deployment, backup=None):
    root = Path(root)
    descriptor = read_json(Path(deployment) / 'deployment.json')
    if (root / 'active.json').exists():
        write_json(root / 'previous.json', read_json(root / 'active.json'))
    write_json(root / 'active.json', {'deployment': str(deployment), 'git_sha': descriptor['git_sha'],
                                   'acceptance': 'PENDING', 'backup': str(backup) if backup else None})
    site = load_site(root)
    containers = {service: service_container(site['project'], service, required=False) for service in SERVICES}
    tx_update(root, phase='APPLIED_PENDING_ACCEPTANCE', operation=kind,
              argus_identity_after={service: fingerprint(container) for service, container in containers.items() if container},
              langfuse_identity_after=protected_state(site))
    evidence = root / 'evidence' / (kind + '-' + stamp())
    write_json(evidence / 'operation.json', read_json(root / 'transaction.json'))
    (root / 'transaction.json').unlink()
    print('服务检查通过；活动版本 ' + descriptor['git_sha'] + '，业务/页面验收仍为 PENDING。')


def install(a):
    root = Path(a.root).resolve()
    site = load_site(root)
    need(not (root / 'active.json').exists(), '已有活动版本，请 upgrade')
    if a.resume:
        tx = read_json(root / 'transaction.json')
        need(tx['operation'] == 'install' and tx['target_sha'] == a.sha, '仅允许恢复同一初始安装事务')
        deployment = Path(tx['target_deployment'])
        load_images(root, a.sha)
    else:
        need(not (root / 'transaction.json').exists(), '已有失败事务；status 后按故障表处理')
        load_images(root, a.sha)
        deployment = candidate_deployment(root, a.sha)
        tx_update(root, operation='install', target_sha=a.sha, target_deployment=str(deployment), phase='PREPARED')
    protected_state(site)
    validate_private_mounts(site)
    compose(root, deployment, 'up', '-d', '--pull', 'never', '--no-build', '--no-deps', 'postgres', 'redis')
    wait_health(site['project'], 'postgres')
    wait_health(site['project'], 'redis')
    activate_apps(root, deployment, site)
    finish_transaction(root, 'install', deployment)


def upgrade(a):
    root = Path(a.root).resolve()
    need(a.maintenance_window, '升级前必须关闭全部写入入口并提供 --maintenance-window')
    need(not (root / 'transaction.json').exists(), '已有未结束事务；先 status/rollback')
    previous = active(root)
    site = preflight(root, previous)
    need(read_json(previous / 'deployment.json')['git_sha'] != a.sha, '当前已是该 SHA；不要重复覆盖或重建')
    load_images(root, a.sha)
    deployment = candidate_deployment(root, a.sha, previous)
    pending = validate_migrations(applied_migrations(site), migration_map(read_json(deployment / 'deployment.json')['source'] + '/migrations'))
    baseline = {s: service_container(site['project'], s)['Id'] for s in ('postgres', 'redis')}
    tx_update(root, operation='upgrade', from_deployment=str(previous), target_deployment=str(deployment),
              target_sha=a.sha, pending_migrations=pending, phase='PREPARED',
              infrastructure_before={s: fingerprint(service_container(site['project'], s)) for s in ('postgres', 'redis')},
              langfuse_identity_before=protected_state(site))
    quiesce(root, previous, site)
    backup = backup_data(root, previous, site)
    activate_apps(root, deployment, site)
    need(all(service_container(site['project'], s)['Id'] == cid for s, cid in baseline.items()),
         '升级意外重建了 PostgreSQL/Redis，停止验收')
    expected = migration_map(read_json(deployment / 'deployment.json')['source'] + '/migrations')
    need(applied_migrations(site) == expected, '启动后的迁移记录与候选源码不一致')
    protected_state(site)
    finish_transaction(root, 'upgrade', deployment, backup)


def backup_command(a):
    root = Path(a.root).resolve()
    need(a.maintenance_window, '备份需要停写窗口 --maintenance-window')
    need(not (root / 'transaction.json').exists(), '已有未结束事务')
    deployment = active(root)
    site = preflight(root, deployment)
    tx_update(root, operation='backup', from_deployment=str(deployment), phase='PREPARED')
    quiesce(root, deployment, site)
    path = backup_data(root, deployment, site)
    compose(root, deployment, 'start', *APPS)
    verify_app_images(site, deployment)
    tx_update(root, phase='BACKUP_COMPLETE_SERVICES_RESUMED')
    write_json(root / 'evidence' / ('backup-' + stamp()) / 'operation.json', read_json(root / 'transaction.json'))
    (root / 'transaction.json').unlink()
    print('原版本已恢复运行。备份目录: ' + str(path))


def redis_restore_volume(site, backup, root):
    name = site['project'] + '_restore_redis_' + secrets.token_hex(8)
    docker('volume', 'create', '--label', 'io.argus-offline.site=' + site['site_id'], name)
    archive = Path(root) / ('.redis-restore-' + stamp() + '.tar')
    try:
        with tarfile.open(archive, 'w') as tf:
            for p in files_under(Path(backup) / 'redis-data'):
                tf.add(p, arcname=p.relative_to(Path(backup) / 'redis-data').as_posix(), recursive=False)
        with open(archive, 'rb') as f:
            docker('run', '--rm', '-i', '--pull', 'never', '--network', 'none', '-v', name + ':/data',
                   '--entrypoint', 'sh', site['redis']['ref'], '-c',
                   'cd /data && tar -xf - && chown -R redis:redis /data', stdin=f, timeout=1800)
    finally:
        if archive.exists():
            archive.unlink()
    return name


def rollback(a):
    root, backup = Path(a.root).resolve(), Path(a.backup).resolve()
    need(a.restore_data and a.maintenance_window, '恢复到备份时刻需要 --restore-data --maintenance-window')
    need(re.fullmatch('[0-9a-f]{64}', a.backup_sha256), '需要备份清单的独立 SHA256')
    need(sha_file(backup / 'SHA256SUMS') == a.backup_sha256, '备份清单 SHA256 不符')
    verify_sums(backup)
    info, saved_site = read_json(backup / 'backup.json'), read_json(backup / 'site.json')
    site = load_site(root)
    need(info['site_id'] == site['site_id'] and info['project'] == site['project'], '备份属于其他站点')
    need(info['pg_restore_rehearsal'] == 'PASS' and saved_site['postgres'] == site['postgres']
         and saved_site['redis'] == site['redis'] and info['pg_volume'] == site['pg_volume'],
         '备份未演练或数据库/缓存镜像、卷发生范围外变化')
    if (backup / 'secret-dependencies.json').exists():
        managed_secrets.verify_receipt(read_json(backup / 'secret-dependencies.json'))
    else:
        need(False, '旧备份无独立密钥依赖记录；请先人工核对密钥恢复方案，禁止自动恢复')
    protected_state(site)
    validate_private_mounts(site)
    # 失败升级时 Runner 可能已不可用；不要求旧 API healthy，但始终停止该项目的全部应用。
    old_active = active(root)
    prior_tx = read_json(root / 'transaction.json') if (root / 'transaction.json').exists() else {}
    need(prior_tx.get('operation') != 'rollback', '已有中断回滚；先按事务阶段核对数据库和卷，禁止盲目重复恢复')
    rescue_deployment = Path(prior_tx.get('target_deployment', old_active))
    need(rescue_deployment.is_dir(), '失败事务的候选配置缺失，请先保全现场')
    compose(root, old_active, 'stop', '-t', '60', *APPS)
    tx_update(root, operation='rollback', restore_backup=str(backup), phase='RESTORE_PREPARED')
    # 保全升级后数据，不覆盖；若保全失败则不继续恢复。
    rescue = backup_data(root, rescue_deployment, site, label='rescue-before-rollback')
    tx_update(root, operation='rollback', rescue_backup=str(rescue), phase='RESTORING')
    docker('load', '-i', str(backup / 'rollback-images.tar'), timeout=3600)
    for ref, expected in info['images'].items():
        need(inspect_one('image', ref)['Id'] == expected, '回滚镜像 ID 不符')
    docker('tag', site['postgres']['id'], site['postgres']['ref'])
    docker('tag', site['redis']['id'], site['redis']['ref'])
    scratch = restore_to_scratch(site, backup / 'postgres.dump', info['table_counts'])
    restored_volume = redis_restore_volume(site, backup, root)
    compose(root, old_active, 'stop', '-t', '60', 'redis')
    database = site['pg_database']
    need(re.fullmatch('[a-zA-Z_][a-zA-Z0-9_]*', database), '数据库名超出自动恢复支持范围')
    held = 'argus_hold_' + secrets.token_hex(8)
    tx_update(root, phase='DATABASE_SWAP_PREPARED', original_database=database,
              held_database=held, restored_database=scratch, restored_redis_volume=restored_volume,
              original_redis_volume=site['redis_volume'])
    sql(site, "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='" + database + "' AND pid<>pg_backend_pid();", db='postgres')
    sql(site, 'ALTER DATABASE "' + database + '" RENAME TO ' + held + ';', db='postgres')
    tx_update(root, phase='DATABASE_RENAMED')
    try:
        sql(site, 'ALTER DATABASE ' + scratch + ' RENAME TO "' + database + '";', db='postgres')
    except BaseException:
        sql(site, 'ALTER DATABASE ' + held + ' RENAME TO "' + database + '";', db='postgres')
        raise
    deployment = root / 'deployments' / ('rollback-' + stamp())
    deployment.mkdir(parents=True)
    model = read_json(backup / 'model.json')
    snapshot_mounts(model, deployment)
    for key, value in model['volumes'].items():
        if value['name'] == info['redis_volume']:
            model['volumes'][key] = {'name': restored_volume, 'external': True}
    model['services']['postgres']['image'] = site['postgres']['ref']
    model['services']['redis']['image'] = site['redis']['ref']
    write_compose(deployment, model)
    write_json(deployment / 'deployment.json', {'git_sha': info['git_sha'], 'source': str(deployment / 'source'),
               'deployment': str(deployment), 'restored_from': str(backup)})
    site['redis_volume'] = restored_volume
    write_json(root / 'site.json', site)
    tx_update(root, phase='DATA_RESTORED', target_deployment=str(deployment))
    compose(root, deployment, 'up', '-d', '--pull', 'never', '--no-build', '--no-deps', 'redis')
    wait_health(site['project'], 'redis')
    need(applied_migrations(site) == info['migrations'] and table_counts(site) == info['table_counts'], '恢复后数据/迁移不符')
    activate_apps(root, deployment, site)
    protected_state(site)
    finish_transaction(root, 'rollback', deployment, backup)
    print('已保留升级后数据库 ' + held + '、原 Redis 卷和 rescue 备份；待验收后另行决定保留期限。')


def status(a):
    root = Path(a.root)
    site = load_site(root)
    result = {'project': site['project'], 'site_id': site['site_id']}
    for name in ('active', 'previous', 'last-backup', 'transaction'):
        p = root / (name + '.json')
        if p.exists():
            result[name] = read_json(p)
    result['containers'] = {}
    for s in SERVICES:
        c = service_container(site['project'], s, required=False)
        result['containers'][s] = {'id': c['Id'], 'image': c['Image'], 'state': c['State']['Status'],
            'health': c['State'].get('Health', {}).get('Status')} if c else None
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='command', required=True)
    for name in ('init', 'adopt', 'load', 'install', 'upgrade', 'backup', 'rollback', 'recover-backup', 'status'):
        p = sub.add_parser(name)
        p.add_argument('--root', default='/data/argus-offline')
        if name in ('init', 'adopt'):
            p.add_argument('--project', default='argus-kunpeng-e2e')
            p.add_argument('--langfuse-container', required=True)
            p.add_argument('--langfuse-network')
            p.add_argument('--langfuse-base-url', default='http://langfuse-web:3000')
            p.add_argument('--console-url', default='http://10.102.0.70:18083')
        if name == 'init':
            p.add_argument('--postgres-image', default='postgres:17')
            p.add_argument('--redis-image', default='redis:7')
            p.add_argument('--environment', default='argus-kunpeng-test')
            p.add_argument('--console-bind', default='10.102.0.70')
            p.add_argument('--langfuse-dashboard-url', default='http://10.102.0.70:13000')
        if name == 'adopt':
            p.add_argument('--compose-file', required=True)
            p.add_argument('--env-file', required=True)
            p.add_argument('--runner-url', default='http://127.0.0.1:18080')
        if name in ('load', 'install', 'upgrade'):
            p.add_argument('--sha', required=True)
        if name == 'install':
            p.add_argument('--resume', action='store_true')
        if name in ('upgrade', 'backup', 'rollback', 'recover-backup'):
            p.add_argument('--maintenance-window', action='store_true')
        if name == 'rollback':
            p.add_argument('--backup', required=True)
            p.add_argument('--backup-sha256', required=True)
            p.add_argument('--restore-data', action='store_true')
    a = ap.parse_args()
    verify_toolchain()
    root = Path(a.root).resolve()
    with operation(root):
        try:
            if a.command == 'load':
                load_images(root, a.sha)
            else:
                {'init': init_site, 'adopt': adopt, 'install': install, 'upgrade': upgrade,
                 'backup': backup_command, 'rollback': rollback, 'recover-backup': recover_backup,
                 'status': status}[a.command](a)
        except (Stop, ValueError, KeyError, OSError) as exc:
            record_error(root, str(exc) if isinstance(exc, Stop) else '操作失败；保留现场，禁止盲目启动旧镜像')
            raise


if __name__ == '__main__':
    finish_main(main)
