"""Housing API waits apply across rows, clients, restarts and inline AI sends."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from threading import Event
from types import SimpleNamespace

import pytest
import config
from ai.ai_service import AIProcessingService
from delivery import housing_retry
from delivery.advertio_client import AdvertioClient, AdvertioError
from delivery.advertio_service import AdvertioDeliveryService
from storage import database
from storage.message_repository import MessageRepository


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DB_NAME', str(tmp_path / 'housing-retry.sqlite'))
    monkeypatch.setattr(config, 'ADVERTIO_INGEST_ENABLED', False)
    database.initialize_db()
    clock = [datetime(2026, 10, 27, 12, tzinfo=timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(housing_retry, 'datetime', Clock)
    monkeypatch.setattr('delivery.advertio_client.datetime', Clock)
    rows = []
    for message_id in [1, 2]:
        database.insert_message('housing', message_id, 'Apartment for rent', '2026-10-27',
                                message_link=f'https://t.me/housing/{message_id}')
        database.update_message(message_id, 'housing', processing_status='processed',
                                ai_status='processed', ai_category='housinglist', ai_processed_at='2026-10-27T10:00:00+00:00')
        data = dict(title='Apartment for rent', description='Original listing', property_type='apartment',
                    listing_type='rent', bedrooms='1', price=2100, currency='CAD', country_code='CA',
                    province='Ontario', city='Toronto')
        database.save_category_record(message_id, 'housinglist', data)
    return clock


class Client:
    def __init__(self, error=None):
        self.error = error
        self.payloads = []
        self.uploads = []
    def create_lead(self, payload):
        self.payloads.append(payload)
        if self.error:
            raise self.error
        return {'lead_id': 'lead-' + payload['externalId'], 'already_existed': False}
    def upload_media(self, *args):
        self.uploads.append(args)
        if self.error:
            raise self.error
        return 'media-key'


def statuses():
    conn = database.get_connection()
    try:
        return [r[0] for r in conn.execute('SELECT advertio_status FROM messages ORDER BY id')]
    finally:
        conn.close()


@pytest.mark.parametrize('scheduled', [False, True])
@pytest.mark.parametrize('header', ['numeric', 'http-date', 'missing', 'invalid'])
def test_wait_survives_restart_blocks_all_rows_and_resumes(env, scheduled, header):
    clock = env
    delay = 3600 if header in ('numeric', 'http-date') else 60
    headers = {} if header == 'missing' else {'Retry-After':
        '3600' if header == 'numeric' else format_datetime(clock[0] + timedelta(hours=1), usegmt=True)
        if header == 'http-date' else 'invalid'}
    transport = AdvertioClient('https://invalid.example', 'fake')
    with pytest.raises(AdvertioError) as raised:
        transport._raise_for_response(SimpleNamespace(status_code=429, headers=headers))
    client = Client(raised.value)
    service = AdvertioDeliveryService(client=client)
    kwargs = dict(before_datetime='2099-01-01T00:00:00+00:00') if scheduled else {}
    result = service.deliver_pending(progress=False, **kwargs)
    assert len(client.payloads) == 1 and result['failed'] == 1
    assert result['paused_until'] == (clock[0] + timedelta(seconds=delay)).isoformat()
    assert statuses() == ['retry', 'waiting']
    restarted_client = Client()
    restarted = AdvertioDeliveryService(client=restarted_client)
    assert restarted.deliver_pending(progress=False, **kwargs)['found'] == 0
    clock[0] += timedelta(seconds=delay - 1)
    assert restarted.deliver_pending(progress=False, **kwargs)['found'] == 0
    assert not restarted_client.payloads
    clock[0] += timedelta(seconds=1)
    assert restarted.deliver_pending(progress=False, **kwargs)['sent'] == 2
    assert statuses() == ['sent', 'sent']
    assert restarted.deliver_pending(progress=False, **kwargs)['found'] == 0
    assert restarted_client.payloads[0] == client.payloads[0]


def test_inline_ai_housing_send_and_media_wait_for_shared_cooldown(env, monkeypatch):
    client = Client(AdvertioError('limited', status=429, retryable=True, retry_after=3600))
    service = AdvertioDeliveryService(client=client)
    ai = AIProcessingService(repository=MessageRepository(),
        provider_manager=SimpleNamespace(provider='fake'), advertio_service=service)
    records = service.repository.get_advertio_pending()
    assert ai._deliver_to_advertio(records[0], 'housinglist', records[0]['housing_data'])['failed'] == 1
    def forbidden(*args, **kwargs):
        raise AssertionError('No media download or upload during cooldown')
    monkeypatch.setattr(service, 'prepare_media_for_delivery', forbidden)
    result = ai._deliver_to_advertio(records[1], 'housinglist', records[1]['housing_data'])
    assert result['attempted'] == 0 and result['skipped'] == 1
    assert len(client.payloads) == 1 and statuses() == ['retry', 'waiting']
    with pytest.raises(housing_retry.HousingDeferred):
        service.deliver(records[1], records[1]['housing_data'])
    assert len(client.payloads) == 1


def test_rate_limit_during_media_upload_pauses_without_creating_lead(env, tmp_path):
    path = tmp_path / 'photo.jpg'; path.write_bytes(b'photo')
    client = Client(AdvertioError('limited', status=429, retryable=True, retry_after=3600))
    service = AdvertioDeliveryService(client=client)
    record = service.repository.get_advertio_pending()[0]
    record['media_path'] = str(path)
    with pytest.raises(AdvertioError):
        service.deliver(record, record['housing_data'])
    assert len(client.uploads) == 1 and not client.payloads
    with pytest.raises(housing_retry.HousingDeferred):
        service.deliver(record, record['housing_data'])
    assert len(client.uploads) == 1 and path.exists()


def test_pause_never_shortens_and_other_errors_keep_existing_behavior(env):
    housing_retry.defer(AdvertioError('busy', status=503, retryable=True, retry_after=7200))
    until = housing_retry.paused_until()
    housing_retry.defer(AdvertioError('limited', status=429, retryable=True, retry_after=1))
    assert housing_retry.paused_until() == until


@pytest.mark.parametrize('error', [AdvertioError('bad', status=400), AdvertioError('timeout', retryable=True)])
def test_missing_wait_on_other_errors_does_not_pause(env, error):
    client = Client(error)
    service = AdvertioDeliveryService(client=client)
    assert service.deliver_pending(progress=False)['failed'] == 2
    assert housing_retry.paused_until() is None
    expected = 'retry' if error.retryable else 'rejected'
    assert statuses() == [expected, expected]


def test_simultaneous_housing_services_observe_first_limit(env):
    entered, release = Event(), Event()
    class BlockingClient(Client):
        def create_lead(self, payload):
            entered.set(); assert release.wait(5)
            return super().create_lead(payload)
    first = BlockingClient(AdvertioError('limited', status=429, retryable=True, retry_after=3600))
    second = Client()
    a, b = AdvertioDeliveryService(client=first), AdvertioDeliveryService(client=second)
    records = a.repository.get_advertio_pending()
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(a.deliver, records[0], records[0]['housing_data'])
        assert entered.wait(5)
        two = pool.submit(b.deliver, records[1], records[1]['housing_data'])
        release.set()
        with pytest.raises(AdvertioError): one.result(timeout=5)
        with pytest.raises(housing_retry.HousingDeferred): two.result(timeout=5)
    assert len(first.payloads) == 1 and not second.payloads
