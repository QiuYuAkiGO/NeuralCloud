from __future__ import annotations

import pytest

from harness.runtime.mistyai_config import (
    MistyAIConfigurationError,
    MistyAIConnectionSettings,
)


def test_mistyai_settings_require_an_explicit_token() -> None:
    with pytest.raises(MistyAIConfigurationError, match="requires NC_MISTYAI_TOKEN_FILE"):
        MistyAIConnectionSettings.from_environment({})


@pytest.mark.parametrize(
    "token",
    [
        "",
        "too-short",
        "development-misty-token",
        "replace-with-body-service-token",
        "CHANGE-ME",
        "${MISTYAI_SERVICE_TOKEN}",
        "<body-service-token>",
    ],
)
def test_mistyai_settings_reject_empty_development_and_placeholder_tokens(token: str) -> None:
    with pytest.raises(MistyAIConfigurationError, match="empty|at least 16|development or placeholder"):
        MistyAIConnectionSettings.from_environment({"NC_MISTYAI_TOKEN": token})


def test_mistyai_settings_accept_environment_token_for_compatibility() -> None:
    settings = MistyAIConnectionSettings.from_environment(
        {
            "NC_MISTYAI_TOKEN": "generated-production-secret",
            "NC_MISTYAI_BASE_URL": "https://body.internal.example/",
            "NC_MISTYAI_TIMEOUT_SECONDS": "4.5",
            "NC_MISTYAI_POLL_INTERVAL_SECONDS": "0.75",
        }
    )

    assert settings.base_url == "https://body.internal.example"
    assert settings.token == "generated-production-secret"
    assert settings.token_source == "environment"
    assert settings.timeout_seconds == 4.5
    assert settings.poll_interval_seconds == 0.75


def test_mistyai_token_file_takes_precedence_over_environment(tmp_path) -> None:
    token_path = tmp_path / "body-service.token"
    token_path.write_text("generated-file-secret\n", encoding="utf-8")

    settings = MistyAIConnectionSettings.from_environment(
        {
            "NC_MISTYAI_TOKEN_FILE": str(token_path),
            "NC_MISTYAI_TOKEN": "generated-environment-secret",
        }
    )

    assert settings.token == "generated-file-secret"
    assert settings.token_source == "file"


def test_invalid_token_file_does_not_fall_back_to_environment(tmp_path) -> None:
    token_path = tmp_path / "body-service.token"
    token_path.write_text("development-misty-token\n", encoding="utf-8")

    with pytest.raises(MistyAIConfigurationError, match="development or placeholder"):
        MistyAIConnectionSettings.from_environment(
            {
                "NC_MISTYAI_TOKEN_FILE": str(token_path),
                "NC_MISTYAI_TOKEN": "generated-environment-secret",
            }
        )


def test_mistyai_token_is_redacted_from_repr() -> None:
    settings = MistyAIConnectionSettings.from_environment({"NC_MISTYAI_TOKEN": "generated-production-secret"})

    assert "generated-production-secret" not in repr(settings)


@pytest.mark.parametrize(
    "environment, message",
    [
        ({"NC_MISTYAI_TOKEN": "generated-secret", "NC_MISTYAI_BASE_URL": "ftp://body"}, "HTTP"),
        (
            {
                "NC_MISTYAI_TOKEN": "generated-secret",
                "NC_MISTYAI_BASE_URL": "https://user:password@body.example",
            },
            "credentials",
        ),
        (
            {
                "NC_MISTYAI_TOKEN": "generated-secret",
                "NC_MISTYAI_BASE_URL": "https://body.example/v1",
            },
            "without a path",
        ),
        (
            {
                "NC_MISTYAI_TOKEN": "generated-secret",
                "NC_MISTYAI_BASE_URL": "http://body.internal.example",
            },
            "HTTPS unless the host is loopback",
        ),
        (
            {"NC_MISTYAI_TOKEN": "generated-secret", "NC_MISTYAI_TIMEOUT_SECONDS": "0"},
            "positive number",
        ),
        (
            {"NC_MISTYAI_TOKEN": "generated-secret", "NC_MISTYAI_TIMEOUT_SECONDS": "nan"},
            "positive number",
        ),
    ],
)
def test_mistyai_settings_reject_unsafe_connection_values(
    environment: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(MistyAIConfigurationError, match=message):
        MistyAIConnectionSettings.from_environment(environment)


@pytest.mark.parametrize(
    "base_url",
    ["http://localhost:8787", "http://127.0.0.1:8787", "http://[::1]:8787"],
)
def test_mistyai_settings_allow_loopback_http(base_url: str) -> None:
    settings = MistyAIConnectionSettings.from_environment(
        {
            "NC_MISTYAI_TOKEN": "generated-production-secret",
            "NC_MISTYAI_BASE_URL": base_url,
        }
    )

    assert settings.base_url == base_url
