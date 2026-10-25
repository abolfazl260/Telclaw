"""AI-led salary extraction policy and contract-compatible negotiable display."""

import json
import pytest
from ai.extractor import _normalize_defaults, _normalize_currencies
from ai.prompt_loader import render_prompt
from test_advertio_jobs import env as env, record


def test_unspecified_currency_defaults_to_cad(env):
    p, _ = env[0].build_payload(
        record(
            cleaned_text="Hiring full-time warehouse staff in Toronto. ساعتی ۲۰ دلار",
            salary=20,
            salary_currency=None,
            salary_period="hour",
        )
    )
    assert json.loads(p["attributesJson"])["salary"] == 20
    assert "Negotiable" not in p["description"]


def test_pay_without_period_keeps_amount(env):
    p, _ = env[0].build_payload(
        record(salary=4000, salary_currency=None, salary_period=None)
    )
    attrs = json.loads(p["attributesJson"])
    assert attrs["salary"] == 4000 and "salary_period" not in attrs


@pytest.mark.parametrize(
    "text",
    [
        "Hiring full-time warehouse staff in Toronto. Please apply.",
        "استخدام تمام وقت انباردار در تورنتو. لطفا پیام بدهید.",
    ],
)
def test_missing_salary_visible_as_negotiable(env, text):
    p, _ = env[0].build_payload(
        record(cleaned_text=text, salary=None, salary_currency=None, salary_period=None)
    )
    assert p["description"] == text + "\n\nحقوق: توافقی (Negotiable)"
    attrs = json.loads(p["attributesJson"])
    assert not {"salary", "salary_period", "salary_negotiable"} & attrs.keys()
    assert p["autoPublish"] is True


def test_stated_amount_can_also_be_negotiable(env):
    text = "Hiring full-time warehouse staff in Toronto. $25/hour, negotiable."
    p, _ = env[0].build_payload(
        record(
            cleaned_text=text, salary=25, salary_currency="CAD", salary_period="hour"
        )
    )
    assert json.loads(p["attributesJson"])["salary"] == 25 and p["description"] == text


def test_foreign_amount_preserved_without_false_negotiable(env):
    result = {
        "category": "joblist",
        "data": {
            "joblist": {"salary": 50, "salary_currency": "USD", "salary_period": "hour"}
        },
    }
    _normalize_defaults(result)
    _normalize_currencies(result)
    assert result["data"]["joblist"]["salary"] == 50
    p, _ = env[0].build_payload(
        record(
            cleaned_text="Hiring full-time warehouse staff in Toronto. USD 50/hour.",
            **result["data"]["joblist"],
        )
    )
    assert (
        "salary" not in json.loads(p["attributesJson"])
        and "Negotiable" not in p["description"]
    )


def test_existing_negotiable_text_not_duplicated(env):
    text = "Hiring full-time warehouse staff in Toronto. Salary negotiable."
    p, _ = env[0].build_payload(record(cleaned_text=text, salary=None))
    assert p["description"] == text


def test_prompt_short_policy():
    prompt = render_prompt("joblist")
    assert "Default salary_currency to CAD" in prompt
    assert "use your understanding" in prompt
    assert "listing should display Negotiable" in prompt
    assert "actual explicit currency only" not in prompt


def test_negotiable_footer_does_not_truncate_source(env):
    from delivery.advertio_jobs import JobsMappingError

    text = "Hiring full-time warehouse staff in Toronto. " + ("x" * 1945)
    with pytest.raises(JobsMappingError, match="with_compensation_label"):
        env[0].build_payload(record(cleaned_text=text, salary=None))


def test_delivery_annotation_never_overwrites_original(env):
    from contextlib import closing
    from storage.database import get_connection
    from test_advertio_jobs import add_post

    service, client = env
    mid = add_post(
        changes={"salary": None, "salary_currency": None, "salary_period": None}
    )
    assert service.deliver_pending()["sent"] == 1
    with closing(get_connection()) as conn:
        original = conn.execute(
            "SELECT cleaned_text FROM messages WHERE id=?", (mid,)
        ).fetchone()[0]
    assert client.sent[0]["description"] == original + "\n\nحقوق: توافقی (Negotiable)"
