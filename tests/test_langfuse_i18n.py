import hashlib
import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
I18N_ROOT = ROOT / "deploy" / "langfuse"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")


def _flatten(value: object, prefix: str = "") -> dict[str, str]:
    if isinstance(value, dict):
        flattened: dict[str, str] = {}
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else key
            flattened.update(_flatten(child, child_prefix))
        return flattened
    assert isinstance(value, str), f"{prefix} must be a string"
    return {prefix: value}


def _placeholders(message: str) -> set[str]:
    return set(re.findall(r"\{\s*([A-Za-z][A-Za-z0-9_]*)\s*(?:[,}])", message))


def test_release_manifest_locks_reproducible_upstream_and_artifacts() -> None:
    manifest = json.loads((I18N_ROOT / "upstream" / "manifest.json").read_text())

    assert manifest["repository"] == "https://github.com/langfuse/langfuse.git"
    assert manifest["tag"] == "v4.38.0"
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["commit"])
    assert manifest["runtime"] == {"node": "24", "pnpm": "12.4.1"}
    assert IMAGE_RE.fullmatch(manifest["images"]["upstreamWeb"])
    assert IMAGE_RE.fullmatch(manifest["images"]["worker"])

    patch_paths = [item["path"] for item in manifest["patches"]]
    assert patch_paths == sorted(patch_paths)
    assert len(patch_paths) >= 3
    for item in manifest["patches"]:
        path = I18N_ROOT / item["path"]
        assert path.is_file()
        assert SHA256_RE.fullmatch(item["sha256"])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]

    for locale, item in manifest["locales"].items():
        path = I18N_ROOT / item["path"]
        assert path.is_file(), locale
        assert SHA256_RE.fullmatch(item["sha256"])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]


def test_locale_resources_cover_core_ui_and_have_matching_contracts() -> None:
    en = _flatten(json.loads((I18N_ROOT / "locales" / "en.json").read_text()))
    zh = _flatten(json.loads((I18N_ROOT / "locales" / "zh-CN.json").read_text()))

    assert set(en) == set(zh)
    assert all(value.strip() for value in en.values())
    assert all(value.strip() for value in zh.values())
    assert {key: _placeholders(value) for key, value in en.items()} == {
        key: _placeholders(value) for key, value in zh.items()
    }

    required = {
        "common.language",
        "common.english",
        "common.simplifiedChinese",
        "navigation.projects",
        "navigation.tracing",
        "navigation.sessions",
        "navigation.users",
        "navigation.prompts",
        "navigation.scores",
        "navigation.datasets",
        "navigation.experiments",
        "navigation.settings",
        "pages.dataset.title",
        "pages.experiment.title",
        "pages.trace.title",
        "pages.score.title",
        "pages.settings.title",
        "states.empty",
        "states.error",
        "validation.required",
    }
    assert required <= set(en)


def test_compose_uses_patched_web_and_matching_immutable_worker() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    web = compose["services"]["langfuse-web"]
    worker = compose["services"]["langfuse-worker"]

    assert web["build"]["context"] == "./deploy/langfuse"
    assert web["image"] == "argus/langfuse-i18n:4.38.0"
    assert web["environment"]["LANGFUSE_UI_DEFAULT_LOCALE"] == (
        "${LANGFUSE_UI_DEFAULT_LOCALE:-zh-CN}"
    )
    assert IMAGE_RE.fullmatch(worker["image"])


def test_documented_i18n_entrypoints_and_fail_fast_scripts_exist() -> None:
    makefile = (ROOT / "Makefile").read_text()
    assert "validate-i18n:" in makefile
    assert "build-langfuse-i18n:" in makefile
    assert "validate-langfuse-integration:" in makefile

    for name in (
        "apply-patches.sh",
        "build-image.sh",
        "check-i18n-coverage.py",
        "prepare-upstream.sh",
        "verify-upstream.sh",
    ):
        path = I18N_ROOT / "scripts" / name
        assert path.is_file(), name
        assert path.stat().st_mode & 0o111, f"{name} must be executable"

    assert (I18N_ROOT / "README.md").is_file()
    assert (ROOT / ".github" / "workflows" / "langfuse-i18n.yml").is_file()


def test_storybook_gate_supplies_required_build_environment() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "langfuse-i18n.yml").read_text()
    )
    steps = workflow["jobs"]["patch-and-ui"]["steps"]
    storybook = next(
        step
        for step in steps
        if step.get("name") == "Build Storybook and capture bilingual smoke screenshots"
    )

    assert {
        "DATABASE_URL",
        "NEXTAUTH_SECRET",
        "NEXTAUTH_URL",
        "SALT",
        "CLICKHOUSE_URL",
        "CLICKHOUSE_USER",
        "CLICKHOUSE_PASSWORD",
    } <= set(storybook["env"])
    assert "playwright install --with-deps chromium" in storybook["run"]

    image_steps = workflow["jobs"]["image"]["steps"]
    swap = next(
        step
        for step in image_steps
        if step.get("name") == "Add swap for the upstream Next.js production build"
    )
    assert "swapon" in swap["run"]
