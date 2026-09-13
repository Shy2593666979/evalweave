from evalweave.agents.events import redact_model_output


def test_redact_model_output_hides_common_credentials() -> None:
    content = (
        'Authorization: Bearer live-token '
        '"api_key":"sk-secret" '
        "password='open-sesame'"
    )

    redacted = redact_model_output(content)

    assert "live-token" not in redacted
    assert "sk-secret" not in redacted
    assert "open-sesame" not in redacted
    assert redacted.count("[REDACTED]") == 3
