from contextlib import closing

import config
from storage import database
from delivery.jobs_discovery import discovery_report
from test_advertio_jobs import env as env, add_post


def test_historical_ai_processed_jobs_require_explicit_backfill(env, monkeypatch):
    service, client = env
    mid=add_post(age_days=1)
    with closing(database.get_connection()) as conn:
        conn.execute("UPDATE advertio_jobs_meta SET value=? WHERE key='deployment_watermark'",(str(mid),))
        conn.commit()
    normal=service.deliver_pending()
    assert normal['found']==0
    assert normal['discovery']['counts']['requires_explicit_backfill']==1
    assert not client.sent
    monkeypatch.setattr(config,'ADVERTIO_JOBS_BACKFILL_ENABLED',True)
    admission=service.enqueue(limit=1,backfill=True)
    assert admission['enqueued']==1
    assert admission['counts']['ready_for_queue']==1
    result=service.deliver_pending()
    assert result['sent']==1
    assert client.sent[0]['autoPublish'] is True
    assert service.enqueue(backfill=True)['enqueued']==0
    assert service.deliver_pending()['found']==0


def test_date_exclusions_use_post_date_not_recent_ai_date(env, monkeypatch):
    service,client=env
    add_post(message_id=101,age_days=30)
    mid=add_post(message_id=102)
    with closing(database.get_connection()) as conn:
        conn.execute("UPDATE messages SET date='bad date' WHERE id=?",(mid,))
        conn.commit()
    monkeypatch.setattr(config,'ADVERTIO_JOBS_BACKFILL_ENABLED',True)
    report=service.enqueue(backfill=True)
    assert report['counts']['outside_source_date_window']==1
    assert report['counts']['invalid_source_date']==1
    assert report['enqueued']==0
    assert not client.sent


def test_effective_backfill_window_is_minimum_and_preview_has_no_side_effects(env,monkeypatch):
    service,client=env
    add_post(age_days=10)
    monkeypatch.setattr(config,'ADVERTIO_JOBS_BACKFILL_DAYS',30)
    with closing(database.get_connection()) as conn:
        report=discovery_report(conn,backfill=True)
        assert report['effective_days']==7
        assert report['counts']['outside_source_date_window']==1
        assert conn.execute('SELECT COUNT(*) FROM advertio_jobs_deliveries').fetchone()[0]==0
    assert not client.sent


def test_diagnostics_distinguish_failed_missing_and_queued(env):
    service,client=env
    add_post(message_id=101)
    failed=add_post(message_id=102)
    missing=add_post(message_id=103)
    with closing(database.get_connection()) as conn:
        conn.execute("UPDATE messages SET ai_status='failed' WHERE id=?",(failed,))
        conn.execute('DELETE FROM joblist WHERE processed_message_id=?',(missing,))
        conn.commit()
    assert service.enqueue()['enqueued']==1
    with closing(database.get_connection()) as conn:
        counts=discovery_report(conn)['counts']
    assert counts['already_in_delivery_queue']==1
    assert counts['awaiting_jobs_extraction']==1
    assert counts['missing_extracted_record']==1
    assert not client.sent


def test_dashboard_explains_original_date_and_backfill_preview(env):
    from backoffice_jobs import jobs_report,jobs_section
    add_post(age_days=30)
    with closing(database.get_connection()) as conn:
        report=jobs_report(conn)
    html=jobs_section(report,lambda *a,**kw:'<form></form>','')
    assert 'Why are Jobs not being sent?' in html
    assert 'original Telegram post date' in html
    assert 'Older than the permitted source-date window' in html
    assert report['backfill_discovery']['counts']['outside_source_date_window']==1


def test_jobs_failure_diagnostics_category_city_and_english_stages(env):
    from backoffice_jobs import jobs_report, jobs_section

    service, client = env
    category_id = add_post(changes={"job_category": "unlisted_category"})
    city_id = add_post(message_id=102, changes={"city": "NotACanadianCity"})
    assert service.deliver_pending()["blocked"] == 2
    assert not client.sent

    with closing(database.get_connection()) as conn:
        report = jobs_report(conn)
    issues = {item["id"]: item for item in report["delivery_issues"]}
    assert issues[category_id]["stage"] == "Job validation"
    assert "unlisted_category" in issues[category_id]["reason"]
    assert "not a supported Advertio value" in issues[category_id]["reason"]
    assert issues[city_id]["stage"] == "Location validation"
    assert "NotACanadianCity" in issues[city_id]["reason"]
    assert "could not be matched uniquely" in issues[city_id]["reason"]
    assert issues[category_id]["http_status"] is None

    html = jobs_section(report, lambda *a, **kw: "<form></form>", "")
    assert "Jobs failure diagnostics" in html
    assert "Failed stage" in html
    assert "Explanation (English)" in html
    assert "unlisted_category" in html
    assert "NotACanadianCity" in html
    assert "not a supported Advertio value" in html


def test_jobs_failure_diagnostics_api_error_does_not_expose_response(env):
    from backoffice_jobs import jobs_report, jobs_section
    from delivery.advertio_client import AdvertioError

    service, client = env
    client.failure = AdvertioError(
        "SECRET response body containing personal information",
        status=400,
        retryable=False,
    )
    add_post()
    assert service.deliver_pending()["rejected"] == 1

    with closing(database.get_connection()) as conn:
        report = jobs_report(conn)
    assert report["delivery_issues"][0]["stage"] == "Advertio HTTP / catalog"
    assert report["delivery_issues"][0]["http_status"] == 400
    assert "exact endpoint and rejected field are not stored" in report["delivery_issues"][0]["reason"]
    html = jobs_section(report, lambda *a, **kw: "", "")
    assert "HTTP 400" in html
    assert "SECRET" not in html
    assert config.ADVERTIO_INGEST_KEY not in html


def test_jobs_failure_diagnostics_before_queue_and_pending_review(env, monkeypatch):
    from backoffice_jobs import jobs_report, jobs_section

    service, client = env
    failed_id = add_post(message_id=201)
    skipped_id = add_post(message_id=202)
    missing_id = add_post(message_id=203)
    historical_id = add_post(message_id=204, age_days=1)
    with closing(database.get_connection()) as conn:
        conn.execute(
            "UPDATE messages SET ai_status='failed',ai_error='invalid_provider_output' WHERE id=?",
            (failed_id,),
        )
        conn.execute(
            "UPDATE messages SET ai_status='skipped',ai_error='skipped:category_disabled:joblist' WHERE id=?",
            (skipped_id,),
        )
        conn.execute("DELETE FROM joblist WHERE processed_message_id=?", (missing_id,))
        conn.execute(
            "UPDATE advertio_jobs_meta SET value=? WHERE key='deployment_watermark'",
            (str(historical_id),),
        )
        conn.commit()

    with closing(database.get_connection()) as conn:
        report = jobs_report(conn)
    sample = {item["id"]: item for item in report["prequeue_issues"]}
    assert sample[failed_id]["stage"] == "AI extraction"
    assert "invalid or incomplete structured data" in sample[failed_id]["reason"]
    assert "Jobs AI extraction was disabled" in sample[skipped_id]["reason"]
    assert sample[missing_id]["stage"] == "AI persistence"
    assert "no structured Jobs record was saved" in sample[missing_id]["reason"]
    assert sample[historical_id]["stage"] == "Queue admission"
    assert "backfill" in sample[historical_id]["reason"]

    # Successful ingestion with remote PendingReview is not an HTTP rejection.
    monkeypatch.setattr(config, "ADVERTIO_JOBS_BACKFILL_ENABLED", True)
    assert service.enqueue(limit=1, backfill=True)["enqueued"] == 1
    client.remote_status = "PendingReview"
    assert service.deliver_pending()["sent"] == 1
    with closing(database.get_connection()) as conn:
        report = jobs_report(conn)
    review = next(item for item in report["delivery_issues"] if item["id"] == historical_id)
    assert review["stage"] == "Advertio moderation"
    assert "not rejected" in review["reason"]
    html = jobs_section(report, lambda *a, **kw: "", "")
    assert "Advertio moderation" in html


def test_jobs_failure_diagnostics_escapes_field_values(env):
    from backoffice_jobs import jobs_report, jobs_section

    service, client = env
    add_post(changes={"job_category": '<script>alert(1)</script>'})
    assert service.deliver_pending()["blocked"] == 1
    with closing(database.get_connection()) as conn:
        report = jobs_report(conn)
    html = jobs_section(report, lambda *a, **kw: "", "")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
