import asyncio

import pytest

import backoffice_web
from backoffice_operations import BackofficeOperations


class FakeProcessing:
    def process_pending_with_stats(self, should_stop=None):
        return {"found": 3, "processed": 3, "failed": 0, "stopped": False}


class FakeClassificationRepository:
    def get_classification_queue_status(self):
        return {"pending": 2, "processing": 0, "classified": 5, "failed": 0}

    def get_classification_pending(self, limit=100000):
        return [{"id": 1}, {"id": 2}]

    def retry_failed_classifications(self):
        return 4


class FakeClassification:
    def __init__(self):
        self.repository = FakeClassificationRepository()
        self.limits = []

    def process_pending_with_stats(self, limit=None, should_stop=None):
        self.limits.append(limit)
        return {
            "found": 2,
            "processed": 2,
            "failed": 0,
            "skipped": 0,
            "stopped": False,
            "disabled": False,
            "remaining": 0,
        }


class FakeAI:
    def __init__(self):
        self.media_downloader = None

    def set_media_downloader(self, downloader):
        self.media_downloader = downloader

    def process_pending_with_stats(self, should_stop=None):
        return {
            "found": 1,
            "processed": 1,
            "failed": 0,
            "skipped": 0,
            "stopped": False,
            "disabled": False,
            "remaining": 0,
            "advertio": None,
        }


class FakeScheduler:
    def __init__(self):
        self._pipeline_lock = asyncio.Lock()


class FakeCrawler:
    def __init__(self):
        self.scheduler = FakeScheduler()
        self.jobs = {}

    def active_jobs(self):
        return self.jobs

    def stop_all(self):
        self.jobs.clear()


class FakeAccounts:
    async def list_accounts(self):
        return [{"session": "primary", "meta": {"username": "tester"}}]

    async def disconnect(self, client):
        return None


class FakeChannels:
    def load(self):
        return {"transport": [{"username": "source", "description": "Source"}]}


class FakeClient:
    pass


class FakeConsole:
    def __init__(self):
        self.processing_service = FakeProcessing()
        self.classification_service = FakeClassification()
        self.ai_service = FakeAI()
        self.advertio_service = None
        self.crawler = FakeCrawler()
        self.accounts = FakeAccounts()
        self.channels = FakeChannels()
        self.client = None
        self.client_account = None

    async def connect_client(self, account_name=None):
        self.client = FakeClient()
        self.client_account = account_name
        return self.client


@pytest.mark.asyncio
async def test_backoffice_operations_runs_same_processing_and_classification_services(monkeypatch):
    console = FakeConsole()
    operations = BackofficeOperations(console_ui=console)
    monkeypatch.setattr("backoffice_operations.database.record_system_activity", lambda *args, **kwargs: None)

    operations.start_processing(requested_by=7)
    await operations._tasks["processing"]
    processing = operations.state("processing")
    assert processing["status"] == "completed"
    assert processing["result"]["processed"] == 3

    operations.start_classification(batch_size=17, requested_by=7)
    await operations._tasks["classification"]
    classification = operations.state("classification")
    assert classification["status"] == "completed"
    assert classification["result"]["processed"] == 2
    assert console.classification_service.limits == [17]

    retried = await operations.retry_failed_classifications(requested_by=7)
    assert retried == 4


@pytest.mark.asyncio
async def test_backoffice_operations_ai_reuses_selected_terminal_account(monkeypatch):
    console = FakeConsole()
    operations = BackofficeOperations(console_ui=console)
    monkeypatch.setattr("backoffice_operations.database.record_system_activity", lambda *args, **kwargs: None)

    operations.start_ai("primary", requested_by=8)
    await operations._tasks["ai"]

    assert operations.state("ai")["status"] == "completed"
    assert console.client_account == "primary"
    assert callable(console.ai_service.media_downloader)


def test_backoffice_app_exposes_terminal_equivalent_operations_routes():
    app = backoffice_web.create_app()
    paths = {route.resource.canonical for route in app.router.routes()}
    assert "/operations" in paths
    assert "/operations/action" in paths


class FakePageOperations:
    async def list_accounts(self):
        return [{"session": "primary", "meta": {"username": "tester"}}]

    def connected_account(self):
        return "primary"

    def states(self):
        return {
            name: {
                "name": name,
                "status": "idle",
                "started_at": None,
                "finished_at": None,
                "result": None,
                "error": None,
                "requested_by": None,
                "params": {},
            }
            for name in ("processing", "classification", "ai", "advertio", "groq")
        }

    def classification_status(self):
        return {"pending": 2, "processing": 0, "classified": 5, "failed": 1, "eligible_pending": 2}

    def crawler_status(self):
        return {"active_jobs": 0, "job_keys": []}

    def channel_data(self):
        return {"transport": [{"username": "source", "name": "Source", "description": "Transport source"}]}

    def transfer_status(self):
        return {"total": 4, "sent": 1, "waiting": 2, "failed": 1}

    def transfer_ads(self, limit=20, offset=0):
        return {
            "total": 1,
            "limit": limit,
            "offset": offset,
            "records": [{
                "processed_message_id": 44,
                "channel_username": "source",
                "origin_city": "Berlin",
                "destination_city": "Toronto",
                "departure_date": "2099-01-02",
                "price": 100,
                "currency": "CAD",
                "contact": "@alice",
                "delivery_status": "waiting",
            }],
        }

    def account_registration_state(self):
        return None

    def any_running(self):
        return False


@pytest.mark.asyncio
async def test_operations_page_renders_terminal_controls(monkeypatch):
    monkeypatch.setattr(
        backoffice_web.backoffice_health,
        "snapshot",
        lambda: {
            "pipeline": {
                "processing_pending": 3,
                "processing_failed": 0,
                "classification_pending": 2,
                "classification_failed": 1,
                "ai_pending": 1,
                "ai_failed": 0,
                "advertio_pending": 1,
                "advertio_failed": 0,
            },
            "activity": [],
        },
    )

    class Request(dict):
        query = {}
        app = {"operations": FakePageOperations()}

    request = Request({
        "session": {"csrf": "csrf", "admin_id": 7},
        "csp_nonce": "nonce",
    })
    response = await backoffice_web.operations_page(request)

    assert response.status == 200
    assert "Run Processing" in response.text
    assert "Run Classification" in response.text
    assert "Run AI Extraction" in response.text
    assert "Start scheduled crawler" in response.text
    assert "Send eligible ads to Advertio" in response.text
    assert "Test Groq connection" in response.text
    assert "Add a new Telegram account" in response.text
    assert "Crawler Channels" in response.text
    assert "Unsent Transfer Ads" in response.text
    assert "Berlin" in response.text
    assert "Toronto" in response.text
    assert "Send eligible existing housing listings to Advertio now?" in response.text
    assert "Log out of Back Office" in response.text



def test_manual_pipeline_jobs_are_serialized():
    operations = BackofficeOperations(console_ui=FakeConsole())
    operations._states["processing"] = {
        "name": "processing",
        "status": "running",
        "started_at": "now",
        "finished_at": None,
        "result": None,
        "error": None,
        "requested_by": 1,
        "params": {},
    }
    with pytest.raises(RuntimeError, match="manual pipeline operation"):
        operations.start_classification(batch_size=10, requested_by=1)


@pytest.mark.asyncio
async def test_account_switch_is_blocked_while_crawler_job_is_active():
    console = FakeConsole()
    console.client = FakeClient()
    console.client_account = "primary"
    console.crawler.jobs["job"] = object()
    operations = BackofficeOperations(console_ui=console)

    with pytest.raises(RuntimeError, match="Stop active crawler jobs"):
        await operations.connect_account("secondary", requested_by=1)
