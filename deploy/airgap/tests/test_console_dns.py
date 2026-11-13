"""真实 Nginx DNS 恢复；仅创建隔离临时容器，不访问业务或 Langfuse。"""
import json
import os
from pathlib import Path
import subprocess
import time
import unittest
import uuid


@unittest.skipUnless(os.environ.get('RUN_DOCKER_TESTS') == '1', '需显式启用隔离 Docker 测试')
class ConsoleDNSTests(unittest.TestCase):
    def test_runner_ip_change_recovers_without_console_restart(self):
        console_image = os.environ['ARGUS_TEST_CONSOLE_IMAGE']
        backend_image = os.environ['ARGUS_TEST_DEMO_IMAGE']
        network = 'argus-dns-test-' + uuid.uuid4().hex[:10]
        containers = []
        def run(*args):
            return subprocess.check_output(['docker', *args], stderr=subprocess.PIPE, text=True).strip()
        def spawn(*args):
            name = network + '-' + str(len(containers))
            containers.append(name)
            return run('run', '--name', name, '-d', '--rm', '--pull', 'never', *args)
        def server(marker):
            code = "from http.server import BaseHTTPRequestHandler,HTTPServer\nclass H(BaseHTTPRequestHandler):\n def do_GET(self):\n  self.send_response(200);self.end_headers();self.wfile.write((%r+self.path).encode())\nHTTPServer(('0.0.0.0',8080),H).serve_forever()" % (marker + ' ')
            return spawn('--network', network, '--network-alias', 'eval-runner', '--entrypoint', 'python', backend_image, '-u', '-c', code)
        def get(cid):
            result = subprocess.run(['docker', 'exec', cid, 'wget', '-T', '2', '-qO-', 'http://127.0.0.1:8080/api/probe?value=1'], capture_output=True, text=True)
            return result.stdout.strip() if result.returncode == 0 else None
        run('network', 'create', '--internal', '--subnet', '172.29.' + str(int(network[-2:], 16)) + '.0/24', network)
        try:
            first = server('before')
            old_ip = json.loads(run('inspect', first))[0]['NetworkSettings']['Networks'][network]['IPAddress']
            console = spawn('--network', network, console_image)
            def eventually(expected):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if get(console) == expected:
                        return
                    time.sleep(0.5)
                self.fail('Console 代理未在 20 秒内自动恢复')
            eventually('before /api/probe?value=1')
            identity = json.loads(run('inspect', console))[0]['State']['StartedAt']
            run('network', 'disconnect', network, first)
            run('stop', '-t', '1', first)
            spawn('--network', network, '--ip', old_ip, '--entrypoint', 'python', backend_image, '-c', 'import time;time.sleep(120)')
            second = server('after')
            new_ip = json.loads(run('inspect', second))[0]['NetworkSettings']['Networks'][network]['IPAddress']
            self.assertNotEqual(old_ip, new_ip)
            eventually('after /api/probe?value=1')
            self.assertEqual(identity, json.loads(run('inspect', console))[0]['State']['StartedAt'])
            record = {'result': 'PASS', 'old_ip': old_ip, 'new_ip': new_ip, 'console_restarted': False,
                      'uri_and_query_preserved': True, 'recovery_bound_seconds': 20}
            if os.environ.get('ARGUS_DNS_RECEIPT'):
                Path(os.environ['ARGUS_DNS_RECEIPT']).write_text(json.dumps(record, indent=2) + '\n')
        finally:
            for cid in reversed(containers):
                subprocess.run(['docker', 'rm', '-f', cid], capture_output=True)
            subprocess.run(['docker', 'network', 'rm', network], capture_output=True)
