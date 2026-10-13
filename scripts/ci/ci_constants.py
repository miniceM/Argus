"""Stable names shared by trusted Argus GitHub Actions controllers."""

REPOSITORY = "miniceM/Argus"
MAIN_REF = "refs/heads/main"
CANDIDATE_ENVIRONMENT = "langfuse-e2e-candidate"
CANDIDATE_WORKFLOW = ".github/workflows/cloud-e2e-trusted.yml"
POLICY_CONTEXT = "Cloud Integration Policy"
REQUIRED_CI_CHECKS = (
    "Code Quality",
    "Full Python Tests",
    "Docker / Compose Validation",
    "Console Quality & E2E",
)
REQUIRED_SUITES = ("managed-cloud", "async-link")
REQUIRED_SECRETS = ("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
LOW_RISK_DEV_DEPENDENCIES = frozenset(
    {
        "@axe-core/playwright",
        "@playwright/test",
        "@testing-library/dom",
        "@testing-library/jest-dom",
        "@testing-library/react",
        "@types/node",
        "@types/react",
        "@types/react-dom",
        "jsdom",
        "playwright",
        "playwright-core",
        "typescript",
        "vitest",
    }
)
