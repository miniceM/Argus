from pathlib import Path

import yaml


def test_cloud_compose_uses_managed_langfuse_dns():
    """Cloud 托管服务会轮换地址，Compose 不得覆盖其 DNS。"""
    compose = yaml.safe_load(Path("docker-compose.cloud.yml").read_text())
    hosts = compose["services"]["eval-runner"].get("extra_hosts", [])
    names = hosts if isinstance(hosts, dict) else [host.split(":", 1)[0] for host in hosts]
    assert not any(name == "cloud.langfuse.com" or name.endswith(".cloud.langfuse.com") for name in names)
