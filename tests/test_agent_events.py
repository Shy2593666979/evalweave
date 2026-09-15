from evalweave.agents.events import redact_model_output


def test_model_output_is_preserved() -> None:
    content = (
        'Authorization: Bearer live-token '
        '"api_key":"sk-secret" '
        "password='open-sesame'"
    )

    assert redact_model_output(content) == content
