import pytest

from harness.runtime.mistyai_config import MistyAIConfigurationError, MistyAIConnectionSettings


def test_accounting_token_is_optional_separate_and_excluded_from_repr(tmp_path):
    body = "generated-body-secret-unique"
    accounting = "generated-accounting-secret-unique"
    settings = MistyAIConnectionSettings.from_environment({"NC_MISTYAI_TOKEN": body})
    assert settings.accounting_token is None
    path = tmp_path / "accounting.token"
    path.write_text(accounting)
    settings = MistyAIConnectionSettings.from_environment(
        {
            "NC_MISTYAI_TOKEN": body,
            "NC_MISTYAI_ACCOUNTING_TOKEN": "generated-lower-precedence-secret",
            "NC_MISTYAI_ACCOUNTING_TOKEN_FILE": str(path),
        }
    )
    assert settings.accounting_token == accounting and settings.accounting_token_source == "file"
    assert body not in repr(settings) and accounting not in repr(settings)
    with pytest.raises(MistyAIConfigurationError, match="differ"):
        MistyAIConnectionSettings.from_environment({"NC_MISTYAI_TOKEN": body, "NC_MISTYAI_ACCOUNTING_TOKEN": body})


def test_bad_accounting_file_never_falls_back(tmp_path):
    with pytest.raises(MistyAIConfigurationError, match="NC_MISTYAI_ACCOUNTING_TOKEN_FILE"):
        MistyAIConnectionSettings.from_environment(
            {
                "NC_MISTYAI_TOKEN": "generated-body-secret-unique",
                "NC_MISTYAI_ACCOUNTING_TOKEN": "generated-accounting-secret-unique",
                "NC_MISTYAI_ACCOUNTING_TOKEN_FILE": str(tmp_path / "missing"),
            }
        )
