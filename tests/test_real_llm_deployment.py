"""Issue #198 部署/架构检查：real-llm-agent 可选接入、隔离与门禁。"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_MODEL_ENV_KEYS = ("MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME")


def test_base_compose_stays_default_without_model_variables():
    base = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert "real-llm-agent" not in base["services"]
    for service in base["services"].values():
        env = service.get("environment") or {}
        if isinstance(env, dict):
            for key in env:
                assert not key.startswith("MODEL_"), "基础 Compose 决不能依赖 MODEL_*"
                assert key in env


def test_override_defines_isolated_real_llm_agent_service():
    override = yaml.safe_load((ROOT / "docker-compose.real-llm.yml").read_text())
    assert set(override) >= {"services"}
    assert set(override["services"]) == {"real-llm-agent"}
    service = override["services"]["real-llm-agent"]
    assert service["build"] == {
        "context": "./services/real-llm-agent",
        "dockerfile": "Dockerfile",
    }
    for key in REQUIRED_MODEL_ENV_KEYS:
        assert service["environment"][key] == "${" + key + ":?required}"
    assert service["ports"] == ["127.0.0.1:18084:8080"]
    assert "/health" in " ".join(service["healthcheck"]["test"])
    assert "networks" not in service, "必须继承基础 Compose 默认网络"
    assert "env_file" not in service
    assert "name" not in override
    assert "volumes" not in override


def test_env_example_lists_three_required_without_secret():
    example_text = (ROOT / ".env.real-llm.example").read_text()
    values: dict[str, str] = {}
    for line in example_text.splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    assert list(values) == list(REQUIRED_MODEL_ENV_KEYS)
    assert values["MODEL_BASE_URL"].startswith("https://")
    assert values["MODEL_API_KEY"] == ""
    assert values["MODEL_NAME"] == ""
    assert "sk-" not in example_text
    assert "langfuse" not in example_text.lower()


def test_real_llm_dockerfile_isolated_without_eval_platform():
    text = (ROOT / "services/real-llm-agent/Dockerfile").read_text()
    assert "python:3.12" in text
    copy_targets = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("COPY"):
            copy_targets.append(line.split()[1])
    assert set(copy_targets) <= {"requirements.txt", "app.py"}
    assert "langfuse" not in text.lower()
    assert "/var/run/docker.sock" not in text
    assert ".env" not in text
    assert ".env" not in text


def test_agent_source_and_requirements_contain_no_eval_platform():
    python_text = (ROOT / "services/real-llm-agent/app.py").read_text()
    requirements = (ROOT / "services/real-llm-agent/requirements.txt").read_text().lower()
    for banned in ("from langfuse", "import langfuse", "Evaluation(", "run_experiment"):
        assert banned not in python_text
    assert "langfuse" not in requirements
    assert "openai" in requirements


def test_aggregate_dev_and_ci_manifest_wiring():
    dev_text = (ROOT / "requirements-dev.txt").read_text()
    assert "-r services/real-llm-agent/requirements.txt" in dev_text
    ci_text = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "services/real-llm-agent/requirements.txt" in ci_text
    assert "context: services/real-llm-agent" in ci_text
    assert "MODEL_API_KEY" not in ci_text
    assert "MODEL_BASE_URL" not in ci_text
    assert "MODEL_NAME" not in ci_text


def test_dependabot_registers_new_docker_directory():
    dependabot = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    docker_dirs = [
        update["directory"]
        for update in dependabot["updates"]
        if update["package-ecosystem"] == "docker"
    ]
    assert "/services/real-llm-agent" in docker_dirs
    pip_dir_count = sum(1 for u in dependabot["updates"] if u["package-ecosystem"] == "pip")
    assert pip_dir_count == 1, "避免 Root aggregate 之外的重复 pip 更新组"


def test_validate_script_extends_guard_to_new_agent():
    validate_script = (ROOT / "scripts/validate.sh").read_text()
    assert "services/real-llm-agent" in validate_script
    assert "services/real-llm-agent/requirements.txt" in validate_script


def test_cloud_compose_runner_dataset_source_defaults_to_langfuse():
    """Issue #198 S6.1：云栈版本化 Launch 必须默认走 Langfuse dataset（run-item 可回填）。"""
    yaml_text = (ROOT / "docker-compose.cloud.yml").read_text()
    assert (
        "ARGUS_DATASET_SOURCE: ${ARGUS_DATASET_SOURCE:-langfuse}" in yaml_text
    ), "云栈 runner 必须默认 langfuse dataset source，防止 seed 残留导致 run-item 永久缺失"

def test_cloud_compose_runner_run_item_strategy_defaults_to_otel():
    """Issue 198 S6.2 方向 A：云栈 run-item 写入默认走 v4 OTel 投影（不再隐式依赖 legacy REST）。"""
    yaml_text = (ROOT / "docker-compose.cloud.yml").read_text()
    assert (
        "ARGUS_LANGFUSE_RUN_ITEM_STRATEGY: ${ARGUS_LANGFUSE_RUN_ITEM_STRATEGY:-otel}" in yaml_text
    ), "云栈 runner 必须默认 OTel 投影，避免 v4 组织 dataset_run_items 返回 200 但不投影"
