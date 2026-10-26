"""Jobs contract, queue, scheduler and operator regressions. All HTTP is mocked."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
import requests

import config
from ai.category_schemas import CATEGORY_FIELDS, validate_result
from ai.extractor import _normalize_defaults
from ai.prompt_loader import render_prompt
from backoffice_jobs import jobs_report, jobs_section
from delivery.advertio_client import AdvertioClient, AdvertioError
from delivery.advertio_jobs import (
    AdvertioJobsService,
    JobsMappingError,
    source_name,
    now,
)
from storage import database

SCHEMA = json.loads(
    (Path(__file__).parent / "fixtures/advertio_jobs_schema.json").read_text()
)
TEXT = "Hiring a full-time warehouse associate in Toronto. CAD 20/hour. Apply to the original poster."


class FakeClient:
    def __init__(self):
        self.sent = []
        self.queries = []
        self.failure = None
        self.remote_status = "Active"
        self.existed = False
        self.cities = [
            dict(
                code="toronto",
                name="Toronto",
                nameFa="تورنتو",
                provinceCode="ON",
                countryCode="CA",
            ),
            dict(
                code="vancouver",
                name="Vancouver",
                nameFa="ونکوور",
                provinceCode="BC",
                countryCode="CA",
            ),
            dict(
                code="montreal",
                name="Montreal",
                nameFa="مونترال",
                provinceCode="QC",
                countryCode="CA",
            ),
        ]

    def catalog(self, path, **params):
        self.queries.append((path, params))
        if path == "categories/jobs/attributes":
            return SCHEMA
        if path == "locations/countries":
            return [{"code": "CA", "name": "Canada"}]
        if path == "locations/provinces":
            return [
                {"code": "ON", "name": "Ontario", "nameFa": "انتاریو"},
                {"code": "BC", "name": "British Columbia"},
                {"code": "QC", "name": "Quebec"},
            ]
        return self.cities

    def create_lead(self, payload):
        self.sent.append(payload)
        if self.failure:
            raise self.failure
        return dict(
            lead_id="lead-1",
            status=self.remote_status,
            already_existed=self.existed,
            http_status=200 if self.existed else 201,
        )


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "jobs.sqlite"))
    for key, value in dict(
        ADVERTIO_INGEST_ENABLED=True,
        ADVERTIO_JOBS_ENABLED=True,
        ADVERTIO_INGEST_KEY="test-secret-never-display",
        ADVERTIO_JOBS_AUTO_PUBLISH=True,
        ADVERTIO_JOBS_BACKFILL_ENABLED=False,
        ADVERTIO_JOBS_BATCH_SIZE=25,
        ADVERTIO_JOBS_DAILY_LIMIT=200,
        ADVERTIO_DAILY_REQUEST_LIMIT=1000,
        ADVERTIO_JOBS_FRESHNESS_DAYS=7,
        ADVERTIO_JOBS_MAX_ATTEMPTS=8,
    ).items():
        monkeypatch.setattr(config, key, value)
    database.initialize_db()
    client = FakeClient()
    return AdvertioJobsService(client), client


def record(**changes):
    r = dict(
        ai_status="processed",
        ai_category="joblist",
        classification_category="joblist",
        classification_status="processed",
        processing_status="processed",
        is_job_listing=1,
        role="employer",
        job_category="warehouse",
        job_type="full_time",
        job_title="Warehouse associate",
        country_code="CA",
        province="ON",
        city="Toronto",
        salary=20,
        salary_currency="CAD",
        salary_period="hour",
        message_id=101,
        channel_id=1001,
        channel_username="jobs_one",
        message_link="https://t.me/jobs_one/101",
        sender_type="user",
        sender_is_original=1,
        sender_id=123456,
        sender_username="real_author",
        cleaned_text=TEXT,
        raw_text=TEXT,
        description="AI summary MUST NOT be sent",
        company="Legacy company",
    )
    r.update(changes)
    return r


def add_post(*, message_id=101, channel="jobs_one", age_days=0, changes=None):
    r = record(**(changes or {}))
    r.update(
        message_id=message_id,
        channel_username=channel,
        message_link=f"https://t.me/{channel}/{message_id}",
    )
    date = (now() - timedelta(days=age_days)).isoformat()
    database.insert_message(
        channel,
        message_id,
        r["cleaned_text"],
        date,
        raw_text=r["raw_text"],
        cleaned_text=r["cleaned_text"],
        channel_id=1001 if channel == "jobs_one" else 1002,
        sender_type=r["sender_type"],
        sender_is_original=r["sender_is_original"],
        sender_username=r["sender_username"],
        sender_id=r["sender_id"],
        message_link=r["message_link"],
    )
    database.update_message(
        message_id,
        channel,
        processing_status="processed",
        ai_status=r["ai_status"],
        ai_category="joblist",
        classification_category="joblist",
        classification_status="processed",
    )
    with closing(database.get_connection()) as c:
        mid = c.execute(
            "SELECT id FROM messages WHERE message_id=? AND channel_username=?",
            (message_id, channel),
        ).fetchone()[0]
    database.save_category_record(
        mid, "joblist", {k: v for k, v in r.items() if k in CATEGORY_FIELDS["joblist"]}
    )
    return mid


def delivery(mid):
    with closing(database.get_connection()) as c:
        return dict(
            c.execute(
                "SELECT * FROM advertio_jobs_deliveries WHERE processed_message_id=?",
                (mid,),
            ).fetchone()
        )


@pytest.mark.parametrize(
    "role,prefix", [("employer", "Hiring"), ("seeker", "Looking for work")]
)
def test_complete_contract_and_original_description(env, role, prefix):
    s, c = env
    p, flags = s.build_payload(record(role=role))
    assert p["categorySlug"] == "jobs" and p["countryCode"] == "CA"
    assert p["mediaKeys"] == [] and p["autoPublish"] is True and flags == []
    assert p["title"] == prefix + " · Warehouse associate · Toronto"
    assert p["description"] == TEXT
    assert p["posterTelegramUserId"] == 123456 and p["contactHandle"] == "@real_author"
    assert p["sourceName"].startswith("tg-jobs-") and len(p["sourceName"]) == 24
    a = json.loads(p["attributesJson"])
    assert isinstance(a, dict)
    assert a["salary"] == 20 and a["salary_period"] == "hour"
    assert not set(a) & {
        "field",
        "salary_min",
        "salary_max",
        "salary_negotiable",
        "experience_years",
        "employment_type",
        "company_name",
        "application_method",
    }
    assert c.queries[-1][1] == dict(
        countryCode="CA", provinceCode="ON", q="Toronto", limit=200
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("role", None),
        ("job_category", None),
        ("job_type", None),
        ("job_category", "unknown"),
        ("job_type", "remote"),
        ("is_job_listing", None),
        ("is_job_listing", False),
        ("country_code", "US"),
        ("city", None),
        ("city", "Unknown"),
        ("city", "North York"),
        ("province", "AB"),
        ("cleaned_text", ""),
        ("cleaned_text", "Hiring"),
        ("cleaned_text", "a" * 2001),
        ("ai_status", "failed"),
        ("sender_type", "bot"),
    ],
)
def test_local_ineligibility_never_fabricates(env, key, value):
    with pytest.raises(JobsMappingError):
        env[0].build_payload(record(**{key: value}))
    assert not env[1].sent


@pytest.mark.parametrize(
    "text,job_type,arrangement",
    [
        (
            "Hiring full-time remote software developer in Toronto. Please contact the author.",
            "full_time",
            "remote",
        ),
        (
            "به نیروی نظافت پاره وقت در ونکوور نیاز داریم. برای اطلاعات تماس بگیرید.",
            "part_time",
            None,
        ),
        (
            "Looking for part-time cleaning work in Vancouver. Available evenings.",
            "part_time",
            None,
        ),
    ],
)
def test_remote_and_multilingual(env, text, job_type, arrangement):
    city = "Vancouver" if "Vancouver" in text or "ونکوور" in text else "Toronto"
    p, _ = env[0].build_payload(
        record(
            cleaned_text=text,
            job_type=job_type,
            work_arrangement=arrangement,
            city="ونکوور" if "ونکوور" in text else city,
            province="BC" if city == "Vancouver" else "ON",
        )
    )
    assert (
        p["description"] == text
        and json.loads(p["attributesJson"])["job_type"] == job_type
    )
    if arrangement:
        assert json.loads(p["attributesJson"])["work_arrangement"] == arrangement


def test_unspecified_hours_not_assumed_and_remote_no_city(env):
    for changes in [
        dict(
            cleaned_text="Hiring staff in Toronto. Please contact us.",
            job_type="full_time",
        ),
        dict(work_arrangement="remote", city=None),
    ]:
        with pytest.raises(JobsMappingError):
            env[0].build_payload(record(**changes))


def test_no_unknown_other_fallback(env):
    with pytest.raises(JobsMappingError, match="other_requires"):
        env[0].build_payload(record(job_category="other", job_title=None))


def test_canonical_label_and_schema_cache(env):
    s, c = env
    for _ in range(2):
        p, _ = s.build_payload(record(job_title=None))
        assert p["title"] == "Hiring · Warehouse · Toronto"
    assert len(c.queries) == 4


def test_ambiguous_city_and_truncated_catalog_are_blocked(env):
    s, c = env
    c.cities += [dict(code="toronto2", name="Toronto", provinceCode="BC")]
    with pytest.raises(JobsMappingError):
        s.build_payload(record(province=None))
    s.catalog.cache.clear()
    c.cities = [c.cities[0]] * 200
    with pytest.raises(JobsMappingError, match="truncated"):
        s.build_payload(record())


def test_jobs_from_any_collected_source_go_to_advertio_without_approval(env):
    service, client = env
    add_post(channel="new_job_ads_source")
    result = service.deliver_pending()
    assert result["sent"] == 1
    assert client.sent[0]["sourceUrl"] == "https://t.me/new_job_ads_source/101"
    assert client.sent[0]["categorySlug"] == "jobs"
    assert client.sent[0]["autoPublish"] is True


def test_city_must_appear_in_source(env):
    with pytest.raises(JobsMappingError, match="source_evidence"):
        env[0].build_payload(
            record(
                cleaned_text="Hiring a full-time warehouse worker. Remote from Canada."
            )
        )


@pytest.mark.parametrize(
    "text,salary,currency,period,expected",
    [
        ("CAD 20/hour", 20, "CAD", "hour", 20),
        ("20 CAD per hour", 20, "CAD", "hour", 20),
        ("CAD 4000/month", 4000, "CAD", "month", 4000),
        ("60000 CAD per year", 60000, "CAD", "year", 60000),
        (
            "ساعتی ۲۰ دلار کانادا",
            20,
            "CAD",
            "hour",
            20,
        ),  # AI understands the Persian word order
        ("۲۰ دلار کانادا ساعتی", 20, "CAD", "hour", 20),
        ("CAD 18-22/hour", 18, "CAD", "hour", 18),
        ("18–22 CAD/hour", 18, "CAD", "hour", 18),
        ("18–22 CAD/hour", 22, "CAD", "hour", 22),
        ("CAD 22-18/hour", 18, "CAD", "hour", 18),
        ("No pay specified", None, None, None, None),
        ("Salary negotiable", None, None, None, None),
        ("USD 20/hour", 20, "USD", "hour", None),
        ("$20/hour", 20, "CAD", "hour", 20),
        ("CAD 20", 20, "CAD", None, 20),
        ("CAD 20/hour", 200, "CAD", "hour", 200),
        ("CAD 0/hour", 0, "CAD", "hour", None),
        ("CAD 1000001/year", 1000001, "CAD", "year", None),
        ("CAD 20/hour", float("nan"), "CAD", "hour", None),
        ("CAD 20/hour", True, "CAD", "hour", None),
    ],
)
def test_compensation_supported_by_original_text(
    env, text, salary, currency, period, expected
):
    p, _ = env[0].build_payload(
        record(
            cleaned_text="Hiring full-time warehouse staff in Toronto. " + text,
            salary=salary,
            salary_currency=currency,
            salary_period=period,
        )
    )
    a = json.loads(p["attributesJson"])
    assert a.get("salary") == expected
    assert ("salary_period" in a) == (expected is not None and period in {"hour", "month", "year"})


@pytest.mark.parametrize(
    "risk",
    [
        "Pay application fee before hiring.",
        "Send online banking password.",
        "Send your passport and social insurance number.",
        "Transfer bitcoin to start work.",
    ],
)
def test_scam_is_submitted_for_moderation_only(env, risk):
    p, flags = env[0].build_payload(record(cleaned_text=TEXT + " " + risk))
    assert flags and p["autoPublish"] is False


def test_contact_integrity_and_provenance(env):
    s, c = env
    for changes in [
        dict(sender_is_original=None),
        dict(sender_is_original=0),
        dict(sender_type="channel"),
        dict(sender_username="jobs_one"),
    ]:
        p, _ = s.build_payload(record(**changes))
        assert p["contactHandle"] is None
        assert p["sourceUrl"] == "https://t.me/jobs_one/101"
    p, _ = s.build_payload(record(sender_id=-100))
    assert "posterTelegramUserId" not in p
    with pytest.raises(JobsMappingError):
        s.build_payload(
            record(
                sender_is_original=0,
                message_link="https://t.me/unrelated/101",
                contact="@random_name",
            )
        )
    with pytest.raises(JobsMappingError):
        s.build_payload(
            record(sender_is_original=0, message_link="https://t.me/c/999/101")
        )


@pytest.mark.parametrize(
    "status,existed,outcome",
    [
        ("Active", False, "sent"),
        ("PendingReview", False, "sent"),
        ("Active", True, "already_existed"),
    ],
)
def test_queue_success_terminal_actual_remote_status(env, status, existed, outcome):
    s, c = env
    c.remote_status = status
    c.existed = existed
    mid = add_post()
    assert s.deliver_pending()[outcome] == 1
    row = delivery(mid)
    assert row["remote_status"] == status and row["lead_id"] == "lead-1"
    assert s.deliver_pending()["found"] == 0 and len(c.sent) == 1
    with closing(database.get_connection()) as conn:
        assert conn.execute(
            "SELECT advertio_status,ai_status FROM messages WHERE id=?", (mid,)
        ).fetchone()[:] == ("waiting", "processed")
    assert json.loads(c.sent[0]["attributesJson"])["role"] == "employer"


@pytest.mark.parametrize(
    "http,retryable,outcome",
    [
        (400, False, "rejected"),
        (401, False, "retry"),
        (404, False, "retry"),
        (429, True, "retry"),
        (500, True, "retry"),
        (None, True, "retry"),
    ],
)
def test_http_policy_and_no_tight_retry(env, http, retryable, outcome, caplog):
    s, c = env
    c.failure = AdvertioError(
        "SECRET applicant private bank details",
        status=http,
        retryable=retryable,
        retry_after=1000 if http == 429 else None,
    )
    mid = add_post()
    add_post(message_id=102)
    result = s.deliver_pending()
    assert result[outcome] >= 1
    assert "SECRET" not in caplog.text
    with closing(database.get_connection()) as conn:
        assert "SECRET" not in str(
            [dict(x) for x in conn.execute("SELECT * FROM advertio_jobs_attempts")]
        )
    if outcome == "retry":
        assert len(c.sent) == 1 and s.deliver_pending()["found"] == 0
        row = delivery(mid if c.sent[0]["externalId"] == "101" else mid + 1)
        assert row["next_attempt_at"] > now().isoformat()
        if http == 429:
            assert datetime.fromisoformat(row["next_attempt_at"]) >= now() + timedelta(
                seconds=990
            )
        if http in (401, 404):
            with closing(database.get_connection()) as conn:
                assert (
                    conn.execute(
                        "SELECT COUNT(*) FROM system_activity WHERE kind='advertio_jobs_fault'"
                    ).fetchone()[0]
                    == 1
                )


def test_missing_fields_blocked_controlled_requeue(env):
    s, c = env
    mid = add_post(changes={"job_type": None})
    assert s.deliver_pending()["blocked"] == 1 and not c.sent
    assert delivery(mid)["error_code"] == "invalid_or_missing_job_type"
    corrected = database.get_category_record(mid, "joblist")
    corrected["job_type"] = "full_time"
    database.save_category_record(mid, "joblist", corrected)
    assert s.deliver_pending()["found"] == 0
    s.requeue(mid)
    assert s.deliver_pending()["sent"] == 1
    with pytest.raises(ValueError):
        s.requeue(mid)


def test_jobs_switches_and_auto_publish_independent(env, monkeypatch):
    s, c = env
    add_post()
    housing = config.ADVERTIO_AUTO_PUBLISH
    cargo = config.ADVERTIO_CARGO_AUTO_PUBLISH
    monkeypatch.setattr(config, "ADVERTIO_JOBS_ENABLED", False)
    assert s.deliver_pending()["disabled"] and not c.sent
    monkeypatch.setattr(config, "ADVERTIO_JOBS_ENABLED", True)
    monkeypatch.setattr(config, "ADVERTIO_INGEST_ENABLED", False)
    assert s.deliver_pending()["disabled"] and not c.sent
    monkeypatch.setattr(config, "ADVERTIO_INGEST_ENABLED", True)
    monkeypatch.setattr(config, "ADVERTIO_JOBS_AUTO_PUBLISH", False)
    s.deliver_pending()
    assert c.sent[0]["autoPublish"] is False
    assert (
        config.ADVERTIO_AUTO_PUBLISH == housing
        and config.ADVERTIO_CARGO_AUTO_PUBLISH == cargo
    )


def test_source_identity_fixed_across_corrections_and_same_id_two_channels(env):
    s, c = env
    first = add_post()
    add_post(channel="jobs_two")
    s.enqueue()
    fixed = delivery(first)["source_name"]
    database.update_message(101, "jobs_one", channel_id=99999)
    assert s.deliver_pending()["sent"] == 2
    assert len({p["sourceName"] for p in c.sent}) == 2
    assert fixed in {p["sourceName"] for p in c.sent}
    assert source_name(
        record(channel_id=None, channel_username="@Jobs_One")
    ) == source_name(record(channel_id=None, channel_username="jobs_one"))


def test_duplicate_workers_atomic_lease_and_stop_after_inflight(env):
    s, c = env
    add_post()
    add_post(message_id=102)
    entered = Event()
    release = Event()
    original = c.create_lead

    def blocking(p):
        entered.set()
        assert release.wait(5)
        return original(p)

    c.create_lead = blocking
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(s.deliver_pending)
        assert entered.wait(5)
        assert AdvertioJobsService(c).deliver_pending()["busy"]
        s.stop()
        release.set()
        result = first.result(5)
    assert result["sent"] == 1 and result["stopped"] and len(c.sent) == 1


def test_restart_recovery_and_cutoff(env):
    s, c = env
    mid = add_post()
    s.enqueue()
    with closing(database.get_connection()) as conn:
        conn.execute(
            "UPDATE advertio_jobs_deliveries SET status='sending',updated_at=? WHERE processed_message_id=?",
            ((now() - timedelta(hours=2)).isoformat(), mid),
        )
        conn.commit()
    cutoff = now().isoformat()
    assert s.deliver_pending(before_datetime=cutoff)["found"] == 0
    assert delivery(mid)["status"] == "retry"
    assert AdvertioJobsService(c).deliver_pending()["sent"] == 1


def test_retry_identity_unchanged_and_bounded(env, monkeypatch):
    s, c = env
    mid = add_post()
    c.failure = AdvertioError("timeout", retryable=True)
    s.deliver_pending()
    first = c.sent[0]
    s.resume()
    with closing(database.get_connection()) as conn:
        conn.execute(
            "UPDATE advertio_jobs_deliveries SET next_attempt_at=NULL,updated_at='2000',attempts=8 WHERE processed_message_id=?",
            (mid,),
        )
        conn.commit()
    assert s.deliver_pending()["blocked"] == 1 and len(c.sent) == 1
    s.requeue(mid)
    c.failure = None
    assert s.deliver_pending()["sent"] == 1
    assert (
        c.sent[-1]["sourceName"] == first["sourceName"]
        and c.sent[-1]["externalId"] == first["externalId"]
    )


def test_no_accidental_backfill_and_freshness(env, monkeypatch):
    s, c = env
    add_post()
    stale = add_post(message_id=102, age_days=30)
    with closing(database.get_connection()) as conn:
        conn.execute(
            "UPDATE advertio_jobs_meta SET value=? WHERE key='deployment_watermark'",
            (str(stale),),
        )
        conn.commit()
    assert s.deliver_pending()["found"] == 0
    with pytest.raises(ValueError):
        s.enqueue(backfill=True)
    monkeypatch.setattr(config, "ADVERTIO_JOBS_BACKFILL_ENABLED", True)
    assert s.deliver_pending()["found"] == 0  # switch alone doesn't scan history
    s.enqueue(backfill=True, limit=1)
    assert s.deliver_pending()["sent"] == 1
    assert c.sent[0]["externalId"] == "101"


def test_jobs_budget_and_shared_budget(env, monkeypatch):
    s, c = env
    add_post()
    add_post(message_id=102)
    monkeypatch.setattr(config, "ADVERTIO_JOBS_DAILY_LIMIT", 1)
    assert s.deliver_pending()["sent"] == 1 and len(c.sent) == 1
    assert s.deliver_pending()["budget_exhausted"]
    monkeypatch.setattr(config, "ADVERTIO_JOBS_DAILY_LIMIT", 200)
    monkeypatch.setattr(config, "ADVERTIO_DAILY_REQUEST_LIMIT", 1)
    with closing(database.get_connection()) as conn:
        conn.execute(
            "INSERT INTO advertio_request_log(category,attempted_at) VALUES('housing',?)",
            (now().isoformat(),),
        )
        conn.commit()
    assert s.deliver_pending()["budget_exhausted"]


def test_live_schema_drift_pauses_jobs(env, monkeypatch):
    s, c = env
    add_post()
    original = c.catalog
    c.catalog = lambda path, **kw: (
        [] if path == "categories/jobs/attributes" else original(path, **kw)
    )
    assert s.deliver_pending()["retry"] == 1
    assert s.deliver_pending()["pause_reason"] == "jobs_schema_changed" and not c.sent


def test_ai_schema_keeps_legacy_and_no_fabricated_currency():
    result = {
        "category": "joblist",
        "data": {
            "joblist": {
                "salary": 20,
                "company": "Example",
                "role": None,
                "job_type": None,
                "city": None,
            }
        },
    }
    _normalize_defaults(result)
    _, data = validate_result(result)
    assert (
        data["company"] == "Example"
        and data["job_type"] is None
        and data["city"] is None
    )
    assert data["salary_currency"] == "CAD"
    assert {
        "role",
        "job_category",
        "country_code",
        "province",
        "city",
        "work_arrangement",
        "is_job_listing",
    } <= set(CATEGORY_FIELDS["joblist"])
    prompt = render_prompt("joblist")
    assert "Remote is NEVER job_type" in prompt and "other" in prompt


def test_additive_migration_preserves_existing_records_and_watermark(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "DB_NAME", str(tmp_path / "legacy.sqlite"))
    with closing(database.get_connection()) as conn:
        conn.executescript("""CREATE TABLE messages(id INTEGER PRIMARY KEY,channel_username TEXT NOT NULL,message_id INTEGER,text TEXT,date TEXT NOT NULL,ai_status TEXT,ai_category TEXT);
            CREATE TABLE joblist(id INTEGER PRIMARY KEY,processed_message_id INTEGER UNIQUE,job_title TEXT,job_type TEXT,company TEXT);
            INSERT INTO messages VALUES(1,'old',101,'old original','2026-10-20','processed','joblist');
            INSERT INTO joblist VALUES(1,1,'Legacy','remote','Legacy company');""")
        conn.commit()
    database.initialize_db()
    with closing(database.get_connection()) as conn:
        row = dict(conn.execute("SELECT * FROM joblist").fetchone())
        assert (
            row["company"] == "Legacy company"
            and row["job_type"] == "remote"
            and row["role"] is None
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM advertio_jobs_deliveries").fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT value FROM advertio_jobs_meta WHERE key='deployment_watermark'"
            ).fetchone()[0]
            == "1"
        )
    database.initialize_db()
    assert database.get_category_record(1, "joblist")["job_type"] == "remote"


def test_backoffice_reports_remote_status_history_and_no_secret(env):
    s, c = env
    add_post()
    s.deliver_pending()
    add_post(message_id=102, changes={"job_type": None})
    s.deliver_pending()
    with closing(database.get_connection()) as conn:
        report = jobs_report(conn)
    assert (
        report["sent"] == 1
        and report["active"] == 1
        and report["blocked"] == 1
        and len(report["attempts"]) == 1
    )
    markup = jobs_section(report, lambda *args, **kw: str(args), "")
    for text in (
        "Send next eligible Jobs",
        "Stop Jobs Delivery",
        "Requeue after correction",
        "Actual remote Active",
        "View delivery history",
    ):
        assert text in markup
    assert config.ADVERTIO_INGEST_KEY not in markup and TEXT not in markup


@pytest.mark.parametrize(
    "http,body",
    [
        (201, {"leadId": "new", "status": "Active", "alreadyExisted": False}),
        (201, {"leadId": "new", "status": "PendingReview", "alreadyExisted": False}),
        (200, {"leadId": "other", "status": "Active", "alreadyExisted": True}),
    ],
)
def test_real_transport_response_contract(env, monkeypatch, http, body):
    captured = {}

    def post(url, **kw):
        captured.update(kw)
        return SimpleNamespace(status_code=http, json=lambda: body)

    monkeypatch.setattr(requests, "post", post)
    client = AdvertioClient("https://example.test", "testkey", category="jobs")
    payload, _ = env[0].build_payload(record())
    out = client.create_lead(payload)
    assert (
        out["status"] == body["status"]
        and out["already_existed"] == body["alreadyExisted"]
    )
    assert captured["headers"]["X-Ingest-Key"] == "testkey"
    assert isinstance(captured["json"]["attributesJson"], str)


@pytest.mark.parametrize("retry_after", ["600", "Wed, 21 Oct 2099 07:28:00 GMT"])
def test_retry_after_and_error_sanitization(env, monkeypatch, retry_after):
    monkeypatch.setattr(
        requests,
        "post",
        lambda *a, **kw: SimpleNamespace(
            status_code=429,
            text="test-secret-never-display applicant PII",
            headers={"Retry-After": retry_after},
        ),
    )
    with pytest.raises(AdvertioError) as error:
        AdvertioClient("https://example.test", "key", category="jobs").create_lead({})
    assert error.value.retry_after >= 600 and "PII" not in str(error.value)


def test_network_timeout_stays_retryable(env, monkeypatch):
    def post(*args, **kwargs):
        raise requests.Timeout("sensitive response")

    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(AdvertioError) as error:
        AdvertioClient("https://example.test", "key", category="jobs").create_lead({})
    assert error.value.retryable and "sensitive" not in str(error.value)


def test_original_url_available_for_text_only_channel_posts(env):
    payload, _ = env[0].build_payload(
        record(message_link=None, sender_type="channel", sender_is_original=None)
    )
    assert (
        payload["sourceUrl"] == "https://t.me/jobs_one/101"
        and payload["contactHandle"] is None
    )


def test_actual_retry_after_timeout_keeps_identity(env):
    s, c = env
    mid = add_post()
    c.failure = AdvertioError("timeout", retryable=True)
    s.deliver_pending()
    original = c.sent[0]
    s.resume()
    with closing(database.get_connection()) as conn:
        conn.execute(
            "UPDATE advertio_jobs_deliveries SET next_attempt_at=NULL,updated_at='2000' WHERE processed_message_id=?",
            (mid,),
        )
        conn.commit()
    c.failure = None
    c.existed = True
    assert s.deliver_pending()["already_existed"] == 1
    assert c.sent[-1] == original


@pytest.mark.asyncio
async def test_scheduler_automatically_delivers_jobs_and_isolates_failure(
    env, monkeypatch
):
    from services.scheduler_service import SchedulerService
    from test_advertio_automatic_retry import (
        FakeProcessing,
        DisabledClassification,
        FakeAIProcessing,
        FakeAdvertioService,
        FakeMonitor,
        FakeStageControl,
    )

    monkeypatch.setattr(config, "ADVERTIO_CARGO_ENABLED", False)
    s, c = env
    mid = add_post()
    scheduler = SchedulerService(
        crawl_job_service=object(),
        processing_service=FakeProcessing(),
        classification_service=DisabledClassification(),
        ai_processing_service=FakeAIProcessing(FakeAdvertioService()),
    )
    scheduler._advertio_jobs_service = s
    scheduler.monitor = FakeMonitor()
    scheduler.stage_control = FakeStageControl()
    await scheduler._run_post_crawl_pipeline(
        object(), "jobs_one", {"status": "completed"}
    )
    assert (
        delivery(mid)["remote_status"] == "Active" and c.sent[0]["autoPublish"] is True
    )
    assert any(stage == "advertio_jobs" for stage, _ in scheduler.monitor.reports)

    def fail(**kwargs):
        raise RuntimeError("test independent failure")

    scheduler._advertio_jobs_service = SimpleNamespace(deliver_pending=fail)
    result = await scheduler._run_post_crawl_pipeline(
        object(), "jobs_one", {"status": "completed"}
    )
    assert len(result) == 3
    assert len(scheduler.ai_processing.advertio_service.calls) == 2


@pytest.mark.asyncio
async def test_scheduler_skip_never_calls_jobs(env, monkeypatch):
    from services.scheduler_service import SchedulerService
    from test_advertio_automatic_retry import (
        FakeProcessing,
        DisabledClassification,
        FakeAIProcessing,
        FakeMonitor,
        FakeStageControl,
    )

    monkeypatch.setattr(config, "ADVERTIO_CARGO_ENABLED", False)
    s, c = env
    add_post()
    scheduler = SchedulerService(
        crawl_job_service=object(),
        processing_service=FakeProcessing(),
        classification_service=DisabledClassification(),
        ai_processing_service=FakeAIProcessing(None),
    )
    scheduler._advertio_jobs_service = s
    scheduler.monitor = FakeMonitor()
    scheduler.stage_control = FakeStageControl()
    scheduler.stage_control.is_skip_requested = lambda stage: stage == "advertio"
    await scheduler._run_post_crawl_pipeline(object(), "jobs_one", {})
    assert not c.sent


@pytest.mark.asyncio
async def test_manual_jobs_worker_same_rules_and_stop(env, monkeypatch):
    from backoffice_operations import BackofficeOperations
    from test_backoffice_operations import FakeConsole

    s, c = env
    add_post()
    operations = BackofficeOperations(console_ui=FakeConsole())
    operations._advertio_jobs = s
    operations.start_advertio_jobs(limit=1, requested_by=123)
    await operations._tasks["advertio_jobs"]
    assert c.sent[0]["autoPublish"] is True
    assert operations.state("advertio_jobs")["status"] == "completed"
    operations.stop_advertio_jobs()
    with closing(database.get_connection()) as conn:
        assert s._meta(conn, "stop") == "1"


def test_transport_coordinates_shared_request_budget(env, monkeypatch):
    calls = []
    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: (
            calls.append(1)
            or SimpleNamespace(
                status_code=201,
                json=lambda: dict(leadId="x", status="Active", alreadyExisted=False),
            )
        ),
    )
    monkeypatch.setattr(config, "ADVERTIO_DAILY_REQUEST_LIMIT", 1)
    AdvertioClient("https://example.test", "key").create_lead(
        {}
    )  # Housing/legacy reserves shared budget
    with pytest.raises(AdvertioError) as exc:
        AdvertioClient("https://example.test", "key", category="jobs").create_lead({})
    assert exc.value.status == 429 and len(calls) == 1


def test_jobs_attempt_failure_does_not_change_ai_state(env):
    s, c = env
    mid = add_post()
    c.failure = AdvertioError("failed", status=500, retryable=True)
    s.deliver_pending()
    with closing(database.get_connection()) as conn:
        row = conn.execute(
            "SELECT ai_status,advertio_status FROM messages WHERE id=?", (mid,)
        ).fetchone()
        assert tuple(row) == ("processed", "waiting")
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='advertio_cargo_deliveries'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "body",
    [
        dict(leadId="x"),
        dict(leadId="x", status="Active", alreadyExisted="false"),
        dict(leadId="x", status="Active", alreadyExisted=False),
    ],
)
def test_malformed_jobs_success_retries_same_identity(env, monkeypatch, body):
    monkeypatch.setattr(
        requests,
        "post",
        lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: body),
    )
    with pytest.raises(AdvertioError) as error:
        AdvertioClient("https://example.test", "key", category="jobs").create_lead({})
    assert error.value.retryable


def test_resume_refreshes_corrected_catalog_immediately(env):
    service, client = env
    add_post()
    original = client.catalog
    client.catalog = lambda path, **kw: (
        [] if path == "categories/jobs/attributes" else original(path, **kw)
    )
    assert service.deliver_pending()["retry"] == 1
    client.catalog = original
    service.resume()
    with closing(database.get_connection()) as conn:
        conn.execute(
            "UPDATE advertio_jobs_deliveries SET next_attempt_at=NULL,updated_at='2000'"
        )
        conn.commit()
    assert service.deliver_pending()["sent"] == 1


def test_changed_classification_is_blocked(env):
    service, client = env
    mid = add_post()
    service.enqueue()
    database.update_message(101, "jobs_one", classification_category="none")
    assert service.deliver_pending()["blocked"] == 1 and not client.sent
    assert delivery(mid)["error_code"] == "ai_not_processed_joblist"


@pytest.mark.parametrize(
    "role,job_type,expected",
    [
        ("employer", "full_time", "sent"),
        ("seeker", "full_time", "sent"),
        (None, "full_time", "blocked"),
        ("employer", None, "blocked"),
    ],
)
def test_real_ai_persistence_to_jobs_queue(env, monkeypatch, role, job_type, expected):
    from ai.ai_service import AIProcessingService
    from storage.message_repository import MessageRepository

    service, client = env
    mid = add_post()
    database.update_message(101, "jobs_one", ai_status="pending")
    monkeypatch.setattr(config, "AI_EXTRACTION_ENABLED", True)
    monkeypatch.setitem(config.AI_EXTRACTION_CATEGORY_ENABLED, "joblist", True)
    data = {
        k: v
        for k, v in record(role=role, job_type=job_type).items()
        if k in CATEGORY_FIELDS["joblist"]
    }

    class Provider:
        provider = "fixture"

        def extract(self, text, category):
            assert text == TEXT and category == "joblist"
            return {"category": "joblist", "data": {"joblist": data}}

    ai = AIProcessingService(
        repository=MessageRepository(),
        provider_manager=Provider(),
        advertio_service=object(),
    )
    result = ai.process_pending()
    assert result["processed"] == 1 and result["failed"] == 0
    stored = database.get_category_record(mid, "joblist")
    assert stored["role"] == role and stored["job_type"] == job_type
    assert service.deliver_pending()[expected] == 1
    if expected == "sent":
        assert client.sent[0]["description"] == TEXT


def test_extraction_failure_never_enters_jobs_delivery(env, monkeypatch):
    from ai.ai_service import AIProcessingService
    from ai.extractor import AIExtractionError
    from storage.message_repository import MessageRepository

    service, client = env
    add_post()
    database.update_message(101, "jobs_one", ai_status="pending")
    monkeypatch.setattr(config, "AI_EXTRACTION_ENABLED", True)
    monkeypatch.setattr(config, "GROQ_INVALID_JSON_MAX_RETRIES", 0)
    monkeypatch.setitem(config.AI_EXTRACTION_CATEGORY_ENABLED, "joblist", True)

    class Provider:
        provider = "fixture"

        def extract(self, text, category):
            raise AIExtractionError("bad JSON", reason="invalid_provider_output")

    ai = AIProcessingService(
        repository=MessageRepository(),
        provider_manager=Provider(),
        advertio_service=object(),
    )
    result = ai.process_pending()
    assert result["failed"] == 1
    assert service.deliver_pending()["found"] == 0 and not client.sent
