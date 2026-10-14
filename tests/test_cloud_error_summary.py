from __future__ import annotations

from scripts.ci.cloud_error_summary import summarize_http_error


def test_summarize_http_error_redacts_credentials_and_urls() -> None:
    public_key = "pk-test-secret-value"
    secret_key = "sk-test-secret-value"
    base_url = "https://langfuse.example.test"
    response = (
        '{"detail":"bootstrap failed: HTTP 401 Unauthorized at '
        f'{base_url}/api/public/datasets using {public_key} and Bearer {secret_key}'
        '"}'
    )

    summary = summarize_http_error(
        500,
        response,
        secret_values=(base_url, public_key, secret_key),
    )

    assert summary.startswith("Cloud bootstrap returned HTTP 500:")
    assert "401 Unauthorized" in summary
    assert "[redacted URL]" in summary
    assert public_key not in summary
    assert secret_key not in summary
    assert base_url not in summary


def test_summarize_http_error_does_not_echo_non_json_body() -> None:
    summary = summarize_http_error(
        502,
        "upstream failure body that must not be copied to logs",
        secret_values=(),
    )

    assert summary == "Cloud bootstrap returned HTTP 502 with a non-JSON response body"


def test_summarize_http_error_handles_empty_response_body() -> None:
    summary = summarize_http_error(500, "", secret_values=())

    assert summary == "Cloud bootstrap returned HTTP 500 with an empty response body"
