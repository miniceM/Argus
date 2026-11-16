#!/usr/bin/env python3
"""麒麟：严格校验、重组、解包；此步骤不访问 Docker 或网络。"""
import argparse
import gzip
import hashlib
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
from common import (CHUNK, checksums, files_under, finish_main, need, operation, read_json,
                    read_sums, safe_extract, sha_file, verify_payload, write_json)


def verify_source(release):
    release = Path(release)
    manifest = verify_payload(release / 'payload')
    expected = {}
    with tarfile.open(release / 'payload' / manifest['source'], 'r:gz') as tf:
        for m in tf:
            if m.isfile():
                with tf.extractfile(m) as f:
                    h = hashlib.sha256()
                    for block in iter(lambda: f.read(1024 * 1024), b''):
                        h.update(block)
                    expected[m.name] = h.hexdigest()
    actual = {p.relative_to(release / 'source').as_posix(): sha_file(p)
              for p in files_under(release / 'source')}
    need(expected == actual, '解出的源码与固定 SHA 源码归档不一致')
    need(manifest['migrations'] == {p.name: sha_file(p) for p in sorted((release / 'source/migrations').glob('*.sql'))},
         '源码迁移清单不符')
    return manifest


def receive(root, transfer, expected_hash, max_bytes):
    root, transfer = Path(root).resolve(), Path(transfer).resolve()
    need(re.fullmatch('[0-9a-f]{64}', expected_hash), '必须提供独立记录的分卷清单 SHA256')
    need(sha_file(transfer / 'SHA256SUMS.parts') == expected_hash, '分卷清单自身 SHA256 不符')
    sums = read_sums(transfer / 'SHA256SUMS.parts')
    names = sorted(sums)
    need(all(re.fullmatch(r'argus-arm64.tar.gz.part-[a-z]{3}', n) for n in names), '分卷名不符')
    for i, name in enumerate(names):
        suffix = ''.join(chr(97 + i // n % 26) for n in (676, 26, 1))
        need(name.endswith(suffix), '分卷缺失或顺序不连续')
        p = transfer / name
        need(not p.is_symlink() and p.is_file() and 0 < p.stat().st_size <= CHUNK, '分卷不是普通文件或超过 280 MB')
        need(sha_file(p) == sums[name], '分卷 SHA256 不符: ' + name)
    need({p.name for p in transfer.glob('argus-arm64.tar.gz.part-*')} == set(names), '发现清单外分卷，禁止 glob 混包')
    releases = root / 'releases'
    releases.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.receive-', dir=str(releases)) as td:
        temp = Path(td)
        archive = temp / 'joined.tar.gz'
        compressed_size = sum((transfer / n).stat().st_size for n in names)
        need(shutil.disk_usage(releases).free > compressed_size * 2, '接收目录空间不足')
        with open(archive, 'xb') as out:
            for name in names:  # 清单排序，不使用未验证 glob 进行重组
                with open(transfer / name, 'rb') as src:
                    shutil.copyfileobj(src, out, 1024 * 1024)
        # 完整读到 gzip EOF，明确验证CRC/尾部，不能只依赖tar到结束标记即停止读取。
        expanded_stream = 0
        with gzip.open(archive, 'rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                expanded_stream += len(block)
                need(expanded_stream <= max_bytes, 'gzip 展开字节数超过限制')
        with tarfile.open(archive, 'r:gz') as tf:
            expanded = sum(m.size for m in tf)
        need(expanded <= max_bytes, '解包大小超过限制；核对交付包再调整 --max-expanded-bytes')
        need(shutil.disk_usage(releases).free > expanded * 2 + compressed_size, '解包与保留源码的空间不足')
        safe_extract(archive, temp / 'candidate/payload')
        manifest = verify_payload(temp / 'candidate/payload')
        safe_extract(temp / 'candidate/payload' / manifest['source'], temp / 'candidate/source')
        verify_source(temp / 'candidate')
        destination = releases / manifest['git_sha']
        need(not destination.exists(), '该 SHA 已接收；先核对已有版本，禁止覆盖不可变交付')
        os.rename(temp / 'candidate', destination)
    print('接收与双层校验完成: ' + manifest['git_sha'])
    print('源码与工具目录: ' + str(destination))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', default='/data/argus-offline')
    ap.add_argument('--transfer', required=True)
    ap.add_argument('--parts-sha256', required=True)
    ap.add_argument('--max-expanded-bytes', type=int, default=20000000000)
    a = ap.parse_args()
    with operation(a.root):
        receive(a.root, a.transfer, a.parts_sha256, a.max_expanded_bytes)


if __name__ == '__main__':
    finish_main(main)
