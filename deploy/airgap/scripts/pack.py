#!/usr/bin/env python3
"""公网 Mac：从 git archive 的固定快照构建，生成 SHA256 + 280 MB 分卷。"""
import argparse
import copy
import gzip
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
from common import (CHUNK, SHA_RE, checksums, docker, environment, files_under, finish_main,
                    image_archive, make_tar, need, read_json, run, safe_extract, sha_file,
                    stamp, verify_payload, verify_sums, write_json)


def split_archive(archive, transfer, chunk_size=CHUNK):
    transfer = Path(transfer)
    transfer.mkdir(exist_ok=False)
    count = 0
    with open(archive, 'rb') as src:
        while True:
            block = src.read(min(chunk_size, 1024 * 1024))
            if not block:
                break
            need(count < 26 ** 3, '分卷数量超过三位后缀容量')
            suffix = ''.join(chr(97 + count // n % 26) for n in (676, 26, 1))
            with open(transfer / ('argus-arm64.tar.gz.part-' + suffix), 'xb') as out:
                written = 0
                while block:
                    out.write(block)
                    written += len(block)
                    if written == chunk_size:
                        break
                    block = src.read(min(chunk_size - written, 1024 * 1024))
            count += 1
    need(count > 0, '压缩包为空')
    checksums(transfer, 'SHA256SUMS.parts')
    verify_sums(transfer, 'SHA256SUMS.parts')
    return sha_file(transfer / 'SHA256SUMS.parts')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--repo', required=True)
    ap.add_argument('--ref', required=True, help='完整 Git SHA 或已获取的 ref；开始时解析并固定')
    ap.add_argument('--out', required=True, help='全新输出目录；可为 ExFAT 外置硬盘路径')
    ap.add_argument('--work-dir', help='Mac 本机 APFS 临时构建目录的父目录')
    ap.add_argument('--with-infra', action='store_true', help='仅首次安装可选：附带 PG17/Redis7 镜像')
    ap.add_argument('--postgres-image', default='postgres:17')
    ap.add_argument('--redis-image', default='redis:7')
    ap.add_argument('--source-receipt', help='可选：已审阅的同 SHA 源码质量 JSON；不会由打包脚本伪造测试通过')
    args = ap.parse_args()
    repo, out = Path(args.repo).resolve(), Path(args.out).resolve()
    need(not out.exists(), '输出目录已存在；禁止混入旧分卷或覆盖旧版本')
    sha = run(['git', '-C', repo, 'rev-parse', '--verify', args.ref + '^{commit}']).decode().strip()
    need(SHA_RE.fullmatch(sha), 'Git SHA 不合法')
    need('miniceM/Argus' in run(['git', '-C', repo, 'remote', 'get-url', 'origin']).decode(),
         'origin 不是 miniceM/Argus；请核对仓库')
    docker('info', '--format', '{{.OSType}}/{{.Architecture}}')
    docker('buildx', 'version')
    out.mkdir(parents=True)
    stage = out / 'staging'
    (stage / 'images').mkdir(parents=True)
    (stage / 'ARGUS_BUILD_ID').write_text(sha + '\n')
    source_name = 'argus-source-' + sha[:12] + '.tar.gz'
    # git archive 严格忽略工作树未提交内容；不调用 checkout/reset。
    raw = out / 'source.tar'
    with open(raw, 'wb') as f:
        run(['git', '-C', repo, 'archive', '--format=tar', sha], stdout=f)
    with open(raw, 'rb') as src, gzip.open(stage / source_name, 'wb', compresslevel=1) as dst:
        shutil.copyfileobj(src, dst)
    manifest = {'format': 1, 'repository': 'miniceM/Argus', 'git_sha': sha,
                'created_at': stamp(), 'source': source_name, 'part_bytes': CHUNK,
                'images': {}, 'validation': {'target_armv8_runtime': 'NOT_RUN',
                'repository_tests': 'NOT_RUN_BY_PACKER', 'intranet_acceptance': 'NOT_RUN'}}
    if args.source_receipt:
        receipt = read_json(args.source_receipt)
        need(receipt.get('git_sha') == sha and receipt.get('overall') == 'PASS', '源码验收回执 SHA/结果不符')
        write_json(stage / 'source-validation.json', receipt)
        manifest['validation']['repository_tests'] = 'EXTERNAL_RECEIPT_REQUIRES_REVIEW'
    with tempfile.TemporaryDirectory(prefix='argus-build-', dir=args.work_dir) as td:
        snap = Path(td) / 'source'
        safe_extract(raw, snap)
        # 构建上下文须保持标准可读权限 (0755/0644)，防止 nginx-unprivileged 等非 root 镜像 COPY 后报 Permission denied。
        for p in snap.rglob('*'):
            os.chmod(p, 0o755 if (p.is_dir() or (p.stat().st_mode & 0o111)) else 0o644)
        for required in ('config/agents.yaml', 'data/dataset.json', 'docs/openapi.json', 'migrations/001_initial_schema.sql'):
            need((snap / required).is_file(), '仓库契约文件缺失: ' + required)
        version_line = next((x for x in (snap / 'services/eval-runner/Dockerfile').read_text().splitlines()
                             if x.startswith('ARG RUNNER_VERSION=')), None)
        need(version_line is not None, 'Runner 版本构建参数发生变化；先更新 Skill')
        manifest['runner_version'] = version_line.split('=', 1)[1]
        manifest['console_version'] = read_json(snap / 'services/console/package.json')['version']
        manifest['dockerfiles'] = {}
        for name in ('eval-runner', 'demo-agent', 'console'):
            context = snap / 'services' / name
            df = context / 'Dockerfile'
            need(df.is_file(), 'Dockerfile 缺失')
            ref = 'argus/' + name + ':git-' + sha[:12]
            target = stage / 'images' / (name + '.tar')
            need(',' not in str(target), 'Buildx 输出路径不能含逗号')
            command = ['buildx', 'build', '--platform', 'linux/arm64', '--pull', '--progress=plain',
                       '--label', 'org.opencontainers.image.revision=' + sha,
                       '--label', 'org.opencontainers.image.source=https://github.com/miniceM/Argus',
                       '-t', ref, '--output', 'type=docker,dest=' + str(target), '-f', str(df)]
            if name == 'eval-runner':
                command += ['--build-arg', 'ARGUS_BUILD_ID=' + sha,
                            '--build-arg', 'RUNNER_VERSION=' + manifest['runner_version']]
            command += [str(context)]
            print('构建 ' + name + ' @ ' + sha, flush=True)
            # 构建日志不含本 Skill 的密码；保存在 Mac，失败时可诊断。
            with open(out / ('build-' + name + '.log'), 'wb') as log:
                docker(*command, stdout=log, timeout=7200, log_stderr=True)
            info = image_archive(target, ref)
            manifest['images'][name] = {'ref': ref, 'id': info['id'], 'file': 'images/' + name + '.tar'}
            manifest['dockerfiles']['services/' + name + '/Dockerfile'] = sha_file(df)
        if args.with_infra:
            for name, remote in (('postgres', args.postgres_image), ('redis', args.redis_image)):
                docker('pull', '--platform', 'linux/arm64', remote, timeout=1800)
                cfg = json.loads(docker('image', 'inspect', remote))[0]
                ref = 'argus-offline/' + name + ':locked-' + cfg['Id'].split(':')[1][:16]
                docker('tag', cfg['Id'], ref)
                target = stage / 'images' / (name + '.tar')
                docker('save', '-o', str(target), ref, timeout=1800)
                info = image_archive(target, ref)
                manifest['images'][name] = {'ref': ref, 'id': info['id'], 'file': 'images/' + name + '.tar'}
        manifest['migrations'] = {p.name: sha_file(p) for p in sorted((snap / 'migrations').glob('*.sql'))}
        # 将操作工具随离线载荷交付；不复制缓存、构建日志或任何本机配置。
        toolkit = snap / 'deploy' / 'airgap'
        need((toolkit / 'scripts/managed_secrets.py').is_file(), '目标 SHA 缺少版本化离线工具，请使用包含根因修复的提交')
        for folder in ('scripts', 'assets', 'references', 'agents', 'tests'):
            shutil.copytree(toolkit / folder, stage / 'toolkit' / folder,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('SKILL.md', 'README.md'):
            shutil.copyfile(toolkit / name, stage / 'toolkit' / name)
    raw.unlink()
    write_json(stage / 'release.json', manifest)
    checksums(stage)
    verify_payload(stage)
    archive = out / 'argus-arm64.tar.gz'
    make_tar(stage, archive)  # Python 归档不携带 macOS xattrs / ACL / AppleDouble。
    with gzip.open(archive, 'rb') as f:
        for _ in iter(lambda: f.read(1024 * 1024), b''):
            pass
    digest = split_archive(archive, out / 'transfer')
    write_json(out / 'PACK-RECEIPT.json', {'git_sha': sha, 'parts_manifest_sha256': digest,
               'part_bytes': CHUNK, 'images': manifest['images'], 'target_acceptance': 'NOT_RUN'})
    print('打包完成。请独立记录 SHA256SUMS.parts 的 SHA256: ' + digest)
    print('传输目录: ' + str(out / 'transfer'))


if __name__ == '__main__':
    finish_main(main)
